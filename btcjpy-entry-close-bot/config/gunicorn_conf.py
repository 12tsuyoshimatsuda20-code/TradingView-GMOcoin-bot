import multiprocessing
import os

bind = "0.0.0.0:8080"
worker_class = "uvicorn.workers.UvicornWorker"
workers = int(os.getenv("WEB_CONCURRENCY", multiprocessing.cpu_count()))
loglevel = os.getenv("LOG_LEVEL", "info")
accesslog = "-"
errorlog = "-"
preload_app = False
timeout = 60
