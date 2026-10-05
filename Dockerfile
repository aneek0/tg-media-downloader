# syntax=docker/dockerfile:1
FROM ghcr.io/astral-sh/uv:python311-bookworm-slim AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY . .
RUN uv sync --frozen --no-dev


FROM python:3.11-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ffmpeg aria2 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --shell /usr/sbin/nologin app

USER app
WORKDIR /app

COPY --from=builder --chown=app:app /app /app

ENV PATH="/app/.venv/bin:${PATH}" \
    DOWNLOAD_LOCATION=/app/DOWNLOADS \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

VOLUME /app/DOWNLOADS

CMD ["python", "bot.py"]
