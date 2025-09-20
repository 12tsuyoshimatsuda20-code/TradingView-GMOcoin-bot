# syntax=docker/dockerfile:1.7
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Tokyo

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates tzdata && \
    rm -rf /var/lib/apt/lists/*

RUN useradd -m -u 10001 appuser
WORKDIR /app

COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY . /app
RUN chown -R appuser:appuser /app
USER appuser

EXPOSE 8080
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
