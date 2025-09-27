FROM public.ecr.aws/docker/library/python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential curl \
    && rm -rf /var/lib/apt/lists/*

COPY exec-lane/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt \
    && python -c "import sys; import platform;\nif platform.system() == 'Linux':\n import subprocess; subprocess.check_call([sys.executable, '-m', 'pip', 'install', '--no-cache-dir', 'uvloop==0.19.0'])"

COPY exec-lane /app/exec-lane
COPY config/gunicorn_conf.py /app/config/gunicorn_conf.py

ENV PYTHONPATH=/app

CMD ["gunicorn", "exec-lane.app:app", "-c", "config/gunicorn_conf.py"]
