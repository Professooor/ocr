"""Application logging: console output plus a rotating log file."""

import logging
from logging.handlers import RotatingFileHandler

from app import config

LOG_FORMAT = "%(asctime)s %(levelname)s [%(name)s] %(message)s"


def setup_logging():
    """Send log messages to the console and to logs/app.log. Safe to call more than once."""
    config.LOG_DIR.mkdir(parents=True, exist_ok=True)
    root_logger = logging.getLogger()
    if getattr(root_logger, "_document_automation_configured", False):
        return

    level = getattr(logging, config.LOG_LEVEL, logging.INFO)
    root_logger.setLevel(level)
    formatter = logging.Formatter(LOG_FORMAT)

    file_handler = RotatingFileHandler(
        config.LOG_DIR / "app.log",
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    root_logger.addHandler(file_handler)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)

    root_logger._document_automation_configured = True
