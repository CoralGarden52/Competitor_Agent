from __future__ import annotations

import logging
from pathlib import Path
from threading import Lock

from app.core.config import get_config


_LOGGER_CACHE: dict[str, logging.Logger] = {}
_LOGGER_LOCK = Lock()


def run_log_path(run_id: str) -> Path:
    config = get_config()
    return config.backend_log_dir_obj / f"{run_id}.log"


def _build_run_logger(run_id: str) -> logging.Logger:
    config = get_config()
    log_path = run_log_path(run_id)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger(f"run.{run_id}")
    logger.setLevel(getattr(logging, config.backend_log_level.upper(), logging.INFO))
    logger.propagate = False

    if not logger.handlers:
        handler = logging.FileHandler(log_path, encoding="utf-8")
        handler.setLevel(getattr(logging, config.backend_log_level.upper(), logging.INFO))
        handler.setFormatter(
            logging.Formatter(
                fmt="%(asctime)s [%(levelname)s] %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
        logger.addHandler(handler)
    return logger


def ensure_run_logger(run_id: str) -> Path:
    normalized = str(run_id or "").strip()
    if not normalized:
        raise ValueError("run_id is required")
    with _LOGGER_LOCK:
        if normalized not in _LOGGER_CACHE:
            _LOGGER_CACHE[normalized] = _build_run_logger(normalized)
    return run_log_path(normalized)


def log_run_output(run_id: str, message: str, *, level: int = logging.INFO) -> None:
    normalized = str(run_id or "").strip()
    cleaned = str(message or "").strip()
    if not normalized or not cleaned:
        return
    with _LOGGER_LOCK:
        logger = _LOGGER_CACHE.get(normalized)
        if logger is None:
            logger = _build_run_logger(normalized)
            _LOGGER_CACHE[normalized] = logger
    logger.log(level, cleaned)
