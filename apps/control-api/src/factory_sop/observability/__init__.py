"""Diagnostic logging and correlation-id propagation: shared infrastructure, not a module.

`solution-and-roadmap.md` §5.15 fixes the shape: structlog JSON with five mandatory fields —
`event`, `module`, `correlation_id`, `level`, `ts`. `event` is a stable snake_case identifier
(`template.version.publish.rejected`), so a search finds a fact rather than a sentence, and
there is deliberately no event-name registry.

This package owns no tables and no domain behavior, and imports no domain module: every
module imports it, so a dependency in the other direction would make it a cycle through the
whole backend. An `import-linter` contract holds that (harness §3).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING, TextIO

import structlog
from structlog.typing import EventDict, WrappedLogger

if TYPE_CHECKING:
    from factory_sop.settings import LogLevel

# Every line carries `correlation_id`, including the ones emitted outside any request —
# start-up, a scheduled sweep, a CLI. A literal beats an empty string: it says "no request
# was in flight" rather than looking like a field that failed to populate.
NO_CORRELATION_ID = "none"

_correlation_id: ContextVar[str] = ContextVar("correlation_id", default=NO_CORRELATION_ID)

_LEVEL_NUMBER = {"debug": 10, "info": 20, "warning": 30, "error": 40}


def configure_logging(*, log_level: LogLevel, stream: TextIO) -> None:
    """Install the JSON renderer. Call once per process, at the entrypoint.

    `stream` is a parameter rather than a module default so a test asserts on real rendered
    output without redirecting the process's own streams (harness §4).
    """
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            _add_correlation_id,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True, key="ts"),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(_LEVEL_NUMBER[log_level]),
        logger_factory=structlog.PrintLoggerFactory(file=stream),
        cache_logger_on_first_use=False,
    )


def get_logger(module: str) -> structlog.stdlib.BoundLogger:
    """Return a logger bound to `module`, the mandatory field naming the emitter.

    The returned proxy resolves the active configuration on each call, not at import. A
    module-level logger is the normal shape — one per file, next to the code that emits —
    and eager binding would freeze whatever configuration existed at import time, which is
    the default console renderer rather than the JSON one the entrypoint installs.
    """
    logger: structlog.stdlib.BoundLogger = structlog.get_logger(module=module)
    return logger


def new_correlation_id() -> str:
    """Mint an id for work that arrives without one."""
    return str(uuid.uuid4())


@contextmanager
def correlation_scope(correlation_id: str) -> Iterator[None]:
    """Bind `correlation_id` for everything logged inside the block.

    A `ContextVar` rather than an argument threaded through call sites: a use case several
    layers into a module has no business taking an id it only forwards, and the same scope
    has to cover an `await` without leaking into a concurrent request.
    """
    token = _correlation_id.set(correlation_id)
    try:
        yield
    finally:
        _correlation_id.reset(token)


def _add_correlation_id(logger: WrappedLogger, name: str, event_dict: EventDict) -> EventDict:
    event_dict["correlation_id"] = _correlation_id.get()
    return event_dict
