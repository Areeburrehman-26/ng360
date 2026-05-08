import logging
from pathlib import Path
from logging.handlers import RotatingFileHandler

from core.logging_setup import configure_logging


def test_configure_logging_creates_log_file():
    logger = logging.getLogger()
    old_handlers = logger.handlers[:]
    logger.handlers = []

    try:
        configure_logging("unit", logging.INFO)
        logs_dir = Path(__file__).resolve().parents[1] / "logs"
        assert logs_dir.exists()
        assert any(isinstance(h, RotatingFileHandler) for h in logger.handlers)
    finally:
        logger.handlers = old_handlers
