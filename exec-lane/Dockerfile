FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN adduser --disabled-password --gecos '' appuser

COPY exec-lane/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

COPY config ./config
COPY exec-lane ./exec-lane
COPY logs ./logs

RUN chown -R appuser:appuser /app

WORKDIR /app/exec-lane
ENV PYTHONPATH=/app/exec-lane

USER appuser

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080"]
