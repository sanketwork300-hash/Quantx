"""Structured logging with a request-scoped correlation id.

The correlation id is stored in a ``ContextVar`` so it survives across ``await``
boundaries and is injected into every log line without being threaded through
function signatures. It is also copied into Celery task headers so one HTTP
request and the jobs it spawns share an id.
"""

from __future__ import annotations

import logging
import sys
import uuid
from contextvars import ContextVar, Token
from typing import Any

import structlog

_correlation_id: ContextVar[str | None] = ContextVar("qip_correlation_id", default=None)


def new_correlation_id() -> str:
    return uuid.uuid4().hex


def set_correlation_id(value: str | None) -> Token:
    return _correlation_id.set(value)


def get_correlation_id() -> str | None:
    return _correlation_id.get()


def reset_correlation_id(token: Token) -> None:
    _correlation_id.reset(token)


def _add_correlation_id(_logger: Any, _name: str, event_dict: dict) -> dict:
    cid = _correlation_id.get()
    if cid is not None:
        event_dict.setdefault("correlation_id", cid)
    return event_dict


_configured = False


def configure_logging(level: str = "INFO", fmt: str = "json") -> None:
    global _configured
    if _configured:
        return

    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=getattr(logging, level.upper(), logging.INFO),
    )

    processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        _add_correlation_id,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
    if fmt == "json":
        processors.append(structlog.processors.JSONRenderer())
    else:
        processors.append(structlog.dev.ConsoleRenderer())

    structlog.configure(
        processors=processors,
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )
    _configured = True


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)
