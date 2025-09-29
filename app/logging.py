import sys
from loguru import logger

from .config import settings


def setup_logging() -> None:
    logger.remove()
    logger.add(
        sys.stdout,
        format="{message}",
        level=settings.log_level.upper(),
        serialize=True,
        backtrace=False,
        diagnose=False,
    )


def get_logger():
    return logger
