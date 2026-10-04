"""Rotating logging helper."""

from __future__ import annotations

import contextlib
import logging
import os
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

from .config import LoggingConfig

MAX_LOG_DIR_BYTES = 2 * 1024 * 1024 * 1024
MAX_LINE_LENGTH = 3000


def _get_dir_size(path: Path) -> int:
    total = 0
    for root, _, files in os.walk(path):
        for file_name in files:
            file_path = Path(root) / file_name
            try:
                total += file_path.stat().st_size
            except OSError:
                continue
    return total


def _enforce_size_limit(log_dir: Path) -> None:
    while log_dir.exists() and _get_dir_size(log_dir) > MAX_LOG_DIR_BYTES:
        files = sorted(
            (file_path for file_path in log_dir.iterdir() if file_path.is_file()),
            key=lambda path: path.stat().st_mtime,
        )
        if not files:
            break
        files[0].unlink(missing_ok=True)


class _BoundedHandler(TimedRotatingFileHandler):
    """Daily rotation, a cap on the total size of the log folder and on line length."""

    def doRollover(self) -> None:  # noqa: N802 - overrides the logging API
        super().doRollover()
        with contextlib.suppress(OSError):
            _enforce_size_limit(Path(self.baseFilename).parent)

    def emit(self, record: logging.LogRecord) -> None:
        message = record.getMessage()
        if len(message) > MAX_LINE_LENGTH:
            record.msg = f"{message[:MAX_LINE_LENGTH]}... [TRUNCATED]"
            record.args = None
        super().emit(record)


def get_logger(component_name: str, config: LoggingConfig) -> logging.Logger:
    logger = logging.getLogger(f"soar_sync.{component_name}")
    if logger.handlers:
        return logger

    logger.setLevel(getattr(logging, config.level, logging.INFO))
    logger.propagate = False
    log_dir = config.directory / component_name
    log_file = log_dir / f"{component_name}.log"

    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        _enforce_size_limit(log_dir)
        handler = _BoundedHandler(
            str(log_file),
            when="midnight",
            interval=1,
            backupCount=7,
            encoding="utf-8",
        )
    except OSError:
        handler = logging.StreamHandler()

    handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    logger.addHandler(handler)
    return logger
