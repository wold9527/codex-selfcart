FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
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
    fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt

RUN pip install --upgrade pip \
    && pip install -r /app/requirements.txt \
    && python -m playwright install --with-deps chromium

COPY . /app

RUN mkdir -p /app/runtime /app/test_outputs /app/logs /app/outputs \
    && chmod +x /app/docker/entrypoint.sh

EXPOSE 8503

ENTRYPOINT ["dumb-init", "--", "/app/docker/entrypoint.sh"]
