import logging
import os
from pathlib import Path
from logging.handlers import RotatingFileHandler


def configure_logging(name: str = "app", level: int = logging.INFO):
    """Configure root logging: console + rotating file handler in ./logs/.

    - Creates a workspace-level `logs/` directory if missing.
    - Writes component logs to `logs/<name>.log` with rotation.
    """
    # Determine workspace root (parent of core/)
    root = Path(__file__).resolve().parents[1]
    logs_dir = root / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger()
    logger.setLevel(level)

    # Avoid adding handlers multiple times in long-running processes
    existing_files = [h for h in logger.handlers if isinstance(h, RotatingFileHandler)]
    if not any(getattr(h, "baseFilename", None) == str(logs_dir / f"{name}.log") for h in existing_files):
        file_path = logs_dir / f"{name}.log"
        fh = RotatingFileHandler(
            filename=str(file_path),
            maxBytes=5 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s — %(message)s")
        fh.setFormatter(fmt)
        fh.setLevel(level)
        logger.addHandler(fh)

    # Ensure a console handler exists
    if not any(isinstance(h, logging.StreamHandler) for h in logger.handlers):
        ch = logging.StreamHandler()
        ch.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s — %(message)s"))
        ch.setLevel(level)
        logger.addHandler(ch)

    logging.getLogger(__name__).debug("Logging configured: %s -> %s", name, str(logs_dir))