FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

COPY ../config /app/config
COPY . /app/exec-lane

RUN adduser --disabled-password --gecos "" bot \
    && mkdir -p /app/logs \
    && chown -R bot:bot /app

USER bot
WORKDIR /app/exec-lane
ENV PYTHONPATH=/app/exec-lane:/app

CMD ["gunicorn", "-c", "/app/config/gunicorn_conf.py", "app:app"]
