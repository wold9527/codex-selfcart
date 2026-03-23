# syntax=docker/dockerfile:1.7
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DEBIAN_FRONTEND=noninteractive \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    DISPLAY=:99 \
    STREAMLIT_PORT=8503 \
    ABC_DB_PATH=/app/runtime/data.db

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    xvfb \
    ca-certificates \
    curl \
    dumb-init \
    libasound2 \
    libatk-bridge2.0-0 \
    libatk1.0-0 \
    libatspi2.0-0 \
    libcairo2 \
    libcups2 \
    libdbus-1-3 \
    libdrm2 \
    libgbm1 \
    libgtk-3-0 \
    libnspr4 \
    libnss3 \
    libpango-1.0-0 \
    libx11-6 \
    libx11-xcb1 \
    libxcb1 \
    libxcomposite1 \
    libxdamage1 \
    libxext6 \
    libxfixes3 \
    libxkbcommon0 \
    libxrandr2 \
    fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt

RUN --mount=type=cache,target=/root/.cache/pip \
    pip install -r /app/requirements.txt \
    && python -m playwright install chromium

COPY . /app

RUN mkdir -p /app/runtime /app/test_outputs /app/logs /app/outputs \
    && chmod +x /app/docker/entrypoint.sh

EXPOSE 8503

ENTRYPOINT ["dumb-init", "--", "/app/docker/entrypoint.sh"]
