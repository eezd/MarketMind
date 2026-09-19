import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from typing import Annotated

from argon2 import PasswordHasher, Type
from argon2.exceptions import InvalidHashError, VerificationError
from fastapi import APIRouter, Depends, Request, Response
from redis import Redis
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from marketmind.db import get_db
from marketmind.errors import ApiError
from marketmind.models import AdminSession, AdminUser, AuditEvent
from marketmind.schemas import IdentityResponse, LoginInput

router = APIRouter(prefix="/auth", tags=["Authentication"])
password_hasher = PasswordHasher(type=Type.ID)
COOKIE_NAME = "mm_session"
RATE_SCRIPT = """
local ip_count = redis.call('INCR', KEYS[1])
if ip_count == 1 then redis.call('EXPIRE', KEYS[1], 60) end
local global_count = redis.call('INCR', KEYS[2])
if global_count == 1 then redis.call('EXPIRE', KEYS[2], 60) end
if ip_count > 5 or global_count > 30 then return 0 end
return 1
"""
Db = Annotated[Session, Depends(get_db)]


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def check_origin(request: Request) -> None:
    if request.headers.get("origin") not in request.app.state.settings.origins:
        raise ApiError(403, "invalid_origin", "A permitted same-origin request is required")


def current_session(request: Request, db: Db) -> tuple[AdminUser, AdminSession]:
    token = request.cookies.get(COOKIE_NAME)
    if not token or len(token) > 128:
        raise ApiError(401, "unauthenticated", "Please sign in")
    row = db.execute(
        select(AdminUser, AdminSession)
        .join(AdminSession, AdminSession.admin_id == AdminUser.id)
        .where(
            AdminSession.token_hash == token_digest(token),
            AdminSession.revoked_at.is_(None),
            AdminSession.expires_at > func.now(),
        )
    ).first()
    if row is None:
        raise ApiError(401, "unauthenticated", "Session is expired or revoked")
    user, session = row
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        check_origin(request)
        csrf = request.headers.get("x-csrf-token", "")
        if not secrets.compare_digest(csrf.encode(), session.csrf_token.encode()):
            raise ApiError(403, "invalid_csrf", "A valid CSRF token is required")
    return user, session


Authenticated = Annotated[tuple[AdminUser, AdminSession], Depends(current_session)]


def identity(user: AdminUser, session: AdminSession) -> IdentityResponse:
    return IdentityResponse(id=user.id, username=user.username, csrf_token=session.csrf_token)


@router.post("/login", response_model=IdentityResponse)
def login(payload: LoginInput, request: Request, response: Response, db: Db) -> IdentityResponse:
    check_origin(request)
    redis: Redis = request.app.state.redis
    # Only the socket peer is trusted. Proxy forwarding headers never select this key.
    peer = request.client.host if request.client else "unknown"
    namespace = request.app.state.rate_namespace
    if not redis.eval(RATE_SCRIPT, 2, f"{namespace}:ip:{token_digest(peer)}", f"{namespace}:global"):
        raise ApiError(429, "login_rate_limited", "Too many login attempts; retry in one minute")
    user = db.scalar(select(AdminUser).where(AdminUser.username == payload.username).with_for_update())
    stored_hash = user.password_hash if user else request.app.state.dummy_password_hash
    try:
        valid = password_hasher.verify(stored_hash, payload.password)
    except (VerificationError, InvalidHashError):
        valid = False
    if not valid or user is None:
        db.add(
            AuditEvent(
                action="auth.login",
                target_type="admin",
                outcome="denied",
                request_id=request.state.request_id,
            )
        )
        db.commit()
        raise ApiError(401, "invalid_credentials", "Invalid username or password")
    if password_hasher.check_needs_rehash(user.password_hash):
        user.password_hash = password_hasher.hash(payload.password)
    previous_token = request.cookies.get(COOKIE_NAME)
    if previous_token and len(previous_token) <= 128:
        previous = db.scalar(
            select(AdminSession).where(
                AdminSession.token_hash == token_digest(previous_token),
                AdminSession.revoked_at.is_(None),
            )
        )
        if previous:
            previous.revoked_at = datetime.now(UTC)
    token = secrets.token_urlsafe(32)
    now = datetime.now(UTC)
    session = AdminSession(
        admin_id=user.id,
        token_hash=token_digest(token),
        csrf_token=secrets.token_urlsafe(32),
        created_at=now,
        expires_at=now + timedelta(hours=request.app.state.settings.session_hours),
    )
    db.add(session)
    db.add(
        AuditEvent(
            admin_id=user.id,
            action="auth.login",
            target_type="admin",
            target_id=user.id,
            outcome="succeeded",
            request_id=request.state.request_id,
        )
    )
    db.commit()
    response.set_cookie(
        COOKIE_NAME,
        token,
        httponly=True,
        secure=request.app.state.settings.cookie_secure,
        samesite="strict",
        max_age=request.app.state.settings.session_hours * 3600,
        path="/api",
    )
    return identity(user, session)


@router.get("/me", response_model=IdentityResponse)
def me(auth: Authenticated) -> IdentityResponse:
    return identity(*auth)


@router.post("/logout", status_code=204)
def logout(request: Request, response: Response, db: Db, auth: Authenticated) -> None:
    user, session = auth
    session.revoked_at = datetime.now(UTC)
    db.add(
        AuditEvent(
            admin_id=user.id,
            action="auth.logout",
            target_type="admin_session",
            target_id=session.id,
            outcome="succeeded",
            request_id=request.state.request_id,
        )
    )
    db.commit()
    response.delete_cookie(
        COOKIE_NAME,
        path="/api",
        httponly=True,
        secure=request.app.state.settings.cookie_secure,
        samesite="strict",
    )
