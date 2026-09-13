"""The process entrypoint: resolve configuration, install logging, build the application.

Run with `uvicorn --factory factory_sop.entrypoint:build`. This is the one place that reads
the process environment; everything below it takes what it needs as an argument, which is
what keeps a use case callable from an ARQ worker or a smoke script (§5.15).

Deliberately thin and deliberately untested: the configuration loading, the logging shape
and the application are each covered at their own seam, and a test for this function would
have to mutate the process environment to reach it (harness §4).
"""

from __future__ import annotations

import os
import sys

from fastapi import FastAPI

from factory_sop.app import create_app
from factory_sop.job.adapters.dispatcher import ArqJobDispatcher
from factory_sop.observability import configure_logging, get_logger
from factory_sop.persistence import create_database_engine, session_factory
from factory_sop.settings import Settings


def build() -> FastAPI:
    """Build the application, or fail start-up loudly if the environment is unusable."""
    settings = Settings.from_environment(os.environ)
    configure_logging(log_level=settings.log_level, stream=sys.stdout)
    get_logger("app").info(
        "app.started", database_host=settings.database_host, log_level=settings.log_level
    )
    app = create_app(settings)
    # The connection pool, opened once per process. It is attached here rather than inside
    # `create_app` so the application can be built against a test's own engine, and so the
    # adapter tests that need no database do not open one (ADR-0002's Unit of Work draws its
    # session from this factory).
    factory = session_factory(create_database_engine(settings))
    app.state.session_factory = factory
    app.state.job_dispatcher = ArqJobDispatcher.from_settings(
        settings,
        session_factory=factory,
    )
    return app
