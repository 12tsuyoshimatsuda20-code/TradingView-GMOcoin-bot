FROM python:3.11-slim

ENV POETRY_VIRTUALENVS_CREATE=false \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY . /app

RUN useradd -ms /bin/bash appuser
USER appuser

EXPOSE 8080

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080"]
