# syntax=docker/dockerfile:1
FROM ghcr.io/astral-sh/uv:0.12.5 AS uv

FROM python:3.13-slim-bookworm AS dependencies
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_PYTHON_DOWNLOADS=never \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /app
# Native extensions may need a compiler; build tools stay out of the runtime.
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY backend/pyproject.toml backend/uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project --python /usr/local/bin/python3.13

FROM python:3.13-slim-bookworm AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH" \
    DATABASE_URL=sqlite:////app/data/decisionguard.db \
    REPOS_STORAGE_DIR=/app/data/repos \
    ADRS_STORAGE_DIR=/app/data/adrs
WORKDIR /app
# Git is used by repository and PR operations; certificates support HTTPS clients.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 app \
    && useradd --uid 10001 --gid app --no-create-home --home-dir /app --shell /usr/sbin/nologin app \
    && mkdir -p /app/data/repos /app/data/adrs /app/.state \
    && chown -R app:app /app/data /app/.state
COPY --from=dependencies /app/.venv /app/.venv
COPY backend/api_services ./api_services
COPY backend/ai_services ./ai_services
COPY backend/shared ./shared
USER app
EXPOSE 8080
CMD ["sh", "-c", "exec uvicorn api_services.app.main:app --host 0.0.0.0 --port \"${PORT:-8080}\" --workers 1"]
