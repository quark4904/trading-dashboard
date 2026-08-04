from __future__ import annotations

import logging
import logging.handlers
import os
from pathlib import Path

from app.config import load_env


_CONFIGURED = False


def configure_logging() -> logging.Logger:
    global _CONFIGURED
    load_env()
    logger = logging.getLogger("trading_dashboard")
    if _CONFIGURED:
        return logger

    level_name = os.getenv("TRADING_DASHBOARD_LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    logger.setLevel(level)
    logger.propagate = False
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)

    log_path = os.getenv("TRADING_DASHBOARD_LOG_PATH", "").strip()
    if log_path:
        try:
            path = Path(log_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            file_handler = logging.handlers.RotatingFileHandler(
                path,
                maxBytes=_env_int("TRADING_DASHBOARD_LOG_MAX_BYTES", 5 * 1024 * 1024, minimum=1024, maximum=100 * 1024 * 1024),
                backupCount=_env_int("TRADING_DASHBOARD_LOG_BACKUP_COUNT", 5, minimum=1, maximum=30),
                encoding="utf-8",
            )
            file_handler.setFormatter(formatter)
            logger.addHandler(file_handler)
        except OSError:
            logger.exception("로그 파일을 열 수 없어 표준 출력만 사용합니다.")

    _CONFIGURED = True
    return logger


def _env_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        value = default
    return max(minimum, min(value, maximum))
