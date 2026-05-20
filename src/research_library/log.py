"""Structured logging helpers.

Replaces ad-hoc ``print(... file=sys.stderr)`` with a module-level
``logging.Logger`` plus :func:`log_event` for one-line JSON events. Callers
that previously did ``print("[chain] ...", file=sys.stderr)`` should switch
to ``get_logger(__name__).info(...)`` or ``log_event("chain.sync_failed",
paper_id=..., error=...)``.

We default to a single stderr handler at INFO level. Set
``RESEARCH_LOG_LEVEL=DEBUG`` to enable verbose output, or
``RESEARCH_LOG_FORMAT=json`` for one-line JSON records (useful when piping
through ``jq``).
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from typing import Any

_CONFIGURED = False


def _configure_root() -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return
    level_name = (os.environ.get("RESEARCH_LOG_LEVEL") or "INFO").upper().strip()
    level = getattr(logging, level_name, logging.INFO)
    fmt = (os.environ.get("RESEARCH_LOG_FORMAT") or "text").strip().lower()

    handler = logging.StreamHandler(stream=sys.stderr)
    if fmt == "json":
        class _JsonFormatter(logging.Formatter):
            def format(self, record: logging.LogRecord) -> str:  # noqa: D401
                payload = {
                    "ts": int(record.created * 1000),
                    "level": record.levelname,
                    "logger": record.name,
                    "msg": record.getMessage(),
                }
                for k, v in record.__dict__.items():
                    if k in payload or k in {
                        "args",
                        "asctime",
                        "created",
                        "exc_info",
                        "exc_text",
                        "filename",
                        "funcName",
                        "levelname",
                        "levelno",
                        "lineno",
                        "module",
                        "msecs",
                        "msg",
                        "name",
                        "pathname",
                        "process",
                        "processName",
                        "relativeCreated",
                        "stack_info",
                        "thread",
                        "threadName",
                    }:
                        continue
                    try:
                        json.dumps(v, default=str)
                        payload[k] = v
                    except (TypeError, ValueError):
                        payload[k] = str(v)
                return json.dumps(payload, ensure_ascii=False, default=str)

        handler.setFormatter(_JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter("[%(name)s] %(message)s"))

    root = logging.getLogger("research_library")
    root.setLevel(level)
    # Replace any prior handlers we installed earlier in the same process.
    if not any(getattr(h, "_research_library_handler", False) for h in root.handlers):
        handler._research_library_handler = True  # type: ignore[attr-defined]
        root.addHandler(handler)
    root.propagate = False
    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    _configure_root()
    if not name.startswith("research_library"):
        name = f"research_library.{name}"
    return logging.getLogger(name)


def log_event(event: str, **fields: Any) -> None:
    """One-line structured event. Use for token-usage, retries, ingest etc."""
    logger = get_logger("event")
    logger.info(event, extra={"event": event, "ts_ms": int(time.time() * 1000), **fields})


__all__ = ["get_logger", "log_event"]
