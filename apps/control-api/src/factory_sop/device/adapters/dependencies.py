"""`device` route dependencies for the phase-one host seam.

The repositories come from the request's one transaction (ADR-0002). Backend storage remains
available only so host deletion can protect historical references; backend routes, use cases,
and probe dependencies are deliberately absent until phase two.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends
from sqlalchemy.orm import Session as DatabaseSession

from factory_sop.device.adapters.repository import (
    PostgresInferenceBackendRepository,
    PostgresInferenceHostRepository,
)
from factory_sop.device.repository import InferenceBackendRepository, InferenceHostRepository
from factory_sop.persistence import request_session


def hosts(session: Annotated[DatabaseSession, Depends(request_session)]) -> InferenceHostRepository:
    """`device_inference_host` on the request's transaction."""
    return PostgresInferenceHostRepository(session)


def backends(
    session: Annotated[DatabaseSession, Depends(request_session)],
) -> InferenceBackendRepository:
    """The shared backend-history lookup used to protect host deletion."""
    return PostgresInferenceBackendRepository(session)
