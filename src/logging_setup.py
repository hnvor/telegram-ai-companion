"""Структурированные логи через structlog → консоль (dev) и JSON файл (prod)."""

import logging
import sys
from pathlib import Path

import structlog

from src.config import settings


def setup_logging() -> None:
    log_dir = Path("logs")
    log_dir.mkdir(exist_ok=True)

    level = getattr(logging, settings.log_level.upper(), logging.INFO)

    timestamper = structlog.processors.TimeStamper(fmt="iso", utc=True)
    shared_processors = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        timestamper,
    ]

    if settings.env == "dev":
        renderer = structlog.dev.ConsoleRenderer(colors=False)
    else:
        renderer = structlog.processors.JSONRenderer()

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )

    # Понизим логи aiogram/httpx
    logging.basicConfig(level=level, stream=sys.stdout, format="%(message)s")
    for noisy in ("httpx", "httpcore", "aiogram.event", "apscheduler"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
