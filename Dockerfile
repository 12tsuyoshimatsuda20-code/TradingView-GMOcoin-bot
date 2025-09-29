FROM python:3.11-slim

ENV TZ=Asia/Tokyo

RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata ca-certificates \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime \
    && echo $TZ > /etc/timezone \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt /tmp/requirements.txt

RUN python -m pip install --upgrade pip \
    && pip install --no-cache-dir -r /tmp/requirements.txt \
    && pip check \
    && python - <<'PY'
import sys
mods = ["fastapi", "pydantic", "aiohttp", "httpx", "pybotters", "loguru", "aiosqlite"]
bad = []
for mod in mods:
    try:
        __import__(mod)
    except Exception as exc:
        bad.append((mod, repr(exc)))
if bad:
    print("IMPORT_SMOKE_FAILED:", bad, file=sys.stderr)
    sys.exit(1)
print("IMPORT_SMOKE_OK")
PY

RUN useradd -m -u 1000 -s /bin/bash appuser \
    && mkdir -p /app/data /app/logs \
    && chown -R appuser:appuser /app

COPY --chown=appuser:appuser . /app

USER appuser

CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8080"]
