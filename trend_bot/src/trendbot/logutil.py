"""Structured JSON-lines logging.

Every log record is one JSON object with ``ts`` (UTC ISO), ``level``, ``event`` and
arbitrary fields. Secrets must never be passed as fields; ``redact`` masks keys that look
like credentials as a last line of defence.
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

_SENSITIVE = ("password", "secret", "token", "api_key", "apikey", "credential")


def redact(fields: dict) -> dict:
    out = {}
    for k, v in fields.items():
        if any(s in k.lower() for s in _SENSITIVE):
            out[k] = "***REDACTED***"
        elif isinstance(v, dict):
            out[k] = redact(v)
        else:
            out[k] = v
    return out


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
        }
        extra = getattr(record, "fields", None)
        if extra:
            payload.update(redact(extra))
        return json.dumps(payload, ensure_ascii=False, default=str)


def get_logger(name: str = "trendbot", log_file: str | Path | None = None, level: int = logging.INFO) -> logging.Logger:
    logger = logging.getLogger(name)
    logger.setLevel(level)
    if not any(isinstance(h.formatter, JsonFormatter) for h in logger.handlers):
        h = logging.StreamHandler(sys.stderr)
        h.setFormatter(JsonFormatter())
        h.setLevel(logging.WARNING)
        logger.addHandler(h)
    if log_file is not None:
        log_file = Path(log_file)
        log_file.parent.mkdir(parents=True, exist_ok=True)
        if not any(getattr(h, "baseFilename", None) == str(log_file.resolve()) for h in logger.handlers):
            fh = logging.FileHandler(log_file, encoding="utf-8")
            fh.setFormatter(JsonFormatter())
            logger.addHandler(fh)
    logger.propagate = False
    return logger


def log_event(logger: logging.Logger, event: str, level: int = logging.INFO, **fields) -> None:
    logger.log(level, event, extra={"fields": fields})
