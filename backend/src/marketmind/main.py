import logging
import secrets
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from redis import Redis
from redis.exceptions import RedisError
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from starlette.exceptions import HTTPException

from marketmind import alerts, analysis_api, auth, changes, controls, market_api, proxies, queries, sessions
from marketmind.config import get_settings
from marketmind.db import make_engine
from marketmind.errors import ApiError, error_response
from marketmind.schemas import ErrorResponse, HealthResponse

logger = logging.getLogger("marketmind")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    app.state.settings = settings
    app.state.engine = make_engine(settings)
    app.state.redis = Redis.from_url(
        settings.redis_url.get_secret_value(),
        socket_connect_timeout=3,
        socket_timeout=3,
        decode_responses=True,
    )
    app.state.rate_namespace = "marketmind:auth"
    app.state.dummy_password_hash = auth.password_hasher.hash(secrets.token_urlsafe(32))
    try:
        yield
    finally:
        app.state.redis.close()
        app.state.engine.dispose()


app = FastAPI(
    title="MarketMind API",
    version="2026.09.18.1",
    description="Authenticated collection control, immutable news data, analysis and traceable summaries.",
    lifespan=lifespan,
    responses={status: {"model": ErrorResponse} for status in (400, 401, 403, 404, 422, 429, 500, 503)},
)


@app.middleware("http")
async def request_context(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    request.state.request_id = str(uuid4())
    try:
        response = await call_next(request)
    except Exception as exc:
        # Exception strings and SQL parameters can contain credentials or raw documents.
        logger.error(
            "request_failed request_id=%s exception=%s",
            request.state.request_id,
            type(exc).__name__,
        )
        response = error_response(request, 500, "internal_error", "An internal error occurred")
    response.headers["X-Request-ID"] = request.state.request_id
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@app.exception_handler(ApiError)
async def api_error(request: Request, exc: ApiError) -> Response:
    return error_response(request, exc.status, exc.code, exc.message)


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError) -> Response:
    # Pydantic's input and ctx fields can include submitted passwords and are never returned.
    details = [{"location": list(error["loc"]), "type": error["type"]} for error in exc.errors()]
    return error_response(request, 422, "validation_error", "Invalid request parameters", details)


@app.exception_handler(HTTPException)
async def http_error(request: Request, exc: HTTPException) -> Response:
    return error_response(request, exc.status_code, "http_error", "Request could not be handled")


@app.exception_handler(SQLAlchemyError)
@app.exception_handler(RedisError)
async def dependency_error(request: Request, exc: Exception) -> Response:
    logger.warning(
        "dependency_unavailable request_id=%s exception=%s",
        request.state.request_id,
        type(exc).__name__,
    )
    return error_response(request, 503, "dependency_unavailable", "A required service is unavailable")


app.include_router(auth.router, prefix="/api/v1")
app.include_router(controls.router, prefix="/api/v1")
app.include_router(changes.router, prefix="/api/v1")
app.include_router(proxies.router, prefix="/api/v1")
app.include_router(sessions.router, prefix="/api/v1")
app.include_router(alerts.router, prefix="/api/v1")
app.include_router(queries.router, prefix="/api/v1")
app.include_router(analysis_api.router, prefix="/api/v1")
app.include_router(market_api.router, prefix="/api/v1")


@app.get("/api/v1/health/live", response_model=HealthResponse, tags=["Health"])
def live() -> HealthResponse:
    return HealthResponse(status="ok")


@app.get("/api/v1/health/ready", response_model=HealthResponse, tags=["Health"])
def ready(request: Request) -> HealthResponse:
    with request.app.state.engine.connect() as connection:
        connection.execute(text("SELECT 1"))
        # A reachable database without the deployed schema is not ready to serve the API.
        revision = connection.scalar(text("SELECT version_num FROM alembic_version"))
        if revision != "20260919_0004":
            raise ApiError(503, "schema_not_ready", "The database schema is not ready")
    if not request.app.state.redis.ping():
        raise ApiError(503, "dependency_unavailable", "A required service is unavailable")
    return HealthResponse(status="ok")
