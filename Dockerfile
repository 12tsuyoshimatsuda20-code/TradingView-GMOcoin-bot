# syntax=docker/dockerfile:1.5
FROM python:3.11-slim AS builder

WORKDIR /app

COPY requirements.txt ./

RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --no-cache-dir -r requirements.txt

FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1

RUN useradd --create-home --shell /bin/bash appuser

WORKDIR /app

COPY --from=builder /usr/local /usr/local
COPY . .

RUN mkdir -p data && chown -R appuser:appuser /app

USER appuser

# PORT 環境変数に対応（未設定なら8000）
CMD ["sh", "-lc", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
