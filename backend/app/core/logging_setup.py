from __future__ import annotations

import io
import logging
import logging.handlers
import sys
from pathlib import Path

from app.core.config import get_config


_LOGGING_CONFIGURED = False


class _LoggerWriter(io.TextIOBase):
    def __init__(self, logger: logging.Logger, level: int) -> None:
        self._logger = logger
        self._level = level
        self._buffer = ""

    def write(self, message: str) -> int:
        if not message:
            return 0
        self._buffer += message
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            cleaned = line.strip()
            if cleaned:
                self._logger.log(self._level, cleaned)
        return len(message)

    def flush(self) -> None:
        cleaned = self._buffer.strip()
        if cleaned:
            self._logger.log(self._level, cleaned)
        self._buffer = ""


def configure_logging() -> Path:
    global _LOGGING_CONFIGURED
    config = get_config()
    log_dir = config.backend_log_dir_obj
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / config.backend_log_filename

    if _LOGGING_CONFIGURED:
        return log_path

    formatter = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    file_handler = logging.handlers.RotatingFileHandler(
        log_path,
        maxBytes=config.backend_log_max_bytes,
        backupCount=config.backend_log_backup_count,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    file_handler.setLevel(getattr(logging, config.backend_log_level.upper(), logging.INFO))

    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.setLevel(getattr(logging, config.backend_log_level.upper(), logging.INFO))
    root_logger.addHandler(file_handler)

    for logger_name in ("uvicorn", "uvicorn.error", "uvicorn.access", "fastapi"):
        named_logger = logging.getLogger(logger_name)
        named_logger.handlers.clear()
        named_logger.setLevel(getattr(logging, config.backend_log_level.upper(), logging.INFO))
        named_logger.propagate = True

    stdout_logger = logging.getLogger("stdout")
    stderr_logger = logging.getLogger("stderr")
    sys.stdout = _LoggerWriter(stdout_logger, logging.INFO)
    sys.stderr = _LoggerWriter(stderr_logger, logging.ERROR)

    _LOGGING_CONFIGURED = True
    logging.getLogger(__name__).info("Backend logging configured at %s", log_path)
    return log_path
