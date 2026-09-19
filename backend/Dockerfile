FROM python:3.12-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.11.8 /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev

FROM python:3.12-slim
ARG APP_VERSION=development
ARG COMMIT_SHA=unknown
LABEL org.opencontainers.image.title="MarketMind API" \
      org.opencontainers.image.version=$APP_VERSION \
      org.opencontainers.image.revision=$COMMIT_SHA
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/playwright MM_VERSION=$APP_VERSION MM_COMMIT_SHA=$COMMIT_SHA
WORKDIR /app
RUN groupadd --system marketmind && useradd --system --gid marketmind marketmind
COPY --from=build --chown=marketmind:marketmind /app /app
RUN /app/.venv/bin/playwright install --with-deps chromium \
    && chmod -R a+rX /opt/playwright \
    && mkdir -p /app/private /app/logs \
    && chown -R marketmind:marketmind /app/private /app/logs
COPY --chown=marketmind:marketmind alembic.ini ./
COPY --chown=marketmind:marketmind migrations ./migrations
USER marketmind
EXPOSE 8000
CMD ["uvicorn", "marketmind.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-proxy-headers"]
