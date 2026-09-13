FROM python:3.12.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH=/app/.venv/bin:$PATH

WORKDIR /app

RUN apt-get update \
    && apt-get install --no-install-recommends -y ca-certificates curl ffmpeg \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir uv==0.12.5

COPY pyproject.toml uv.lock .python-version ./
COPY apps/control-api/ apps/control-api/
COPY packages/contracts/ packages/contracts/

RUN uv sync --frozen --package control-api --no-dev

WORKDIR /app/apps/control-api
EXPOSE 8000
