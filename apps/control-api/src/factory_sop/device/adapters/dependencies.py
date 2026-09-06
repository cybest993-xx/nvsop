"""What a `device` route declares to reach the module's seams.

The repositories come from the request's one transaction (ADR-0002), the probe from the
process. Authentication is `auth`'s `Authenticated` dependency — `device` is above `auth`
in the layering because its routes resolve the caller through it, and because #23's
`authorize` calls will land in this module's use cases the same way (§5.15). Authorization
itself is not here: it is a use-case-layer decision, and no permission is wired yet.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends
from sqlalchemy.orm import Session as DatabaseSession

from factory_sop.device.adapters.probe import UrllibConnectionProbe
from factory_sop.device.adapters.repository import (
    PostgresInferenceBackendRepository,
    PostgresInferenceHostRepository,
)
from factory_sop.device.probing import ConnectionProbe
from factory_sop.device.repository import InferenceBackendRepository, InferenceHostRepository
from factory_sop.persistence import request_session


def hosts(session: Annotated[DatabaseSession, Depends(request_session)]) -> InferenceHostRepository:
    """`device_inference_host` on the request's transaction.

    Overridden in the route tests with an in-memory stand-in at this same seam.
    """
    return PostgresInferenceHostRepository(session)


def backends(
    session: Annotated[DatabaseSession, Depends(request_session)],
) -> InferenceBackendRepository:
    """`device_inference_backend` on the request's transaction."""
    return PostgresInferenceBackendRepository(session)


def probe() -> ConnectionProbe:
    """The real network probe. The route tests replace it with a scripted stand-in."""
    return UrllibConnectionProbe()
