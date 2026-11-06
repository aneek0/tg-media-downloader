# syntax=docker/dockerfile:1
FROM ghcr.io/astral-sh/uv:python3.11-bookworm-slim AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY . .
RUN uv sync --frozen --no-dev --no-install-project


FROM python:3.11-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        aria2 \
        ca-certificates \
        chromium \
        curl \
        ffmpeg \
        fonts-liberation \
        nodejs \
        procps \
        tini \
        xvfb \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --shell /usr/sbin/nologin app

USER app
WORKDIR /app

COPY --from=builder --chown=app:app /app /app

ENV PATH="/app/.venv/bin:${PATH}" \
    CHROME_PATH=/usr/bin/chromium \
    DOWNLOAD_LOCATION=/app/DOWNLOADS \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

VOLUME /app/DOWNLOADS

ENTRYPOINT ["/usr/bin/tini", "--"]

CMD ["python", "bot.py"]
