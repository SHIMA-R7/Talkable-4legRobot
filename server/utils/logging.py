"""
server/utils/logging.py
========================
構造化ログ設定。
uvicorn のログとアプリログを統一フォーマットで出力する。
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path


_FMT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
_DATE_FMT = "%Y-%m-%d %H:%M:%S"


def setup_logging(level: str = "INFO", log_file: str | None = None) -> None:
    numeric = getattr(logging, level.upper(), logging.INFO)

    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]

    if log_file:
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(
            log_file, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
        )
        handlers.append(fh)

    logging.basicConfig(level=numeric, format=_FMT, datefmt=_DATE_FMT, handlers=handlers)

    # uvicorn の冗長ログを抑制
    for noisy in ("uvicorn.access", "websockets.server"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
