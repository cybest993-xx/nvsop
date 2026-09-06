"""The seams `device`'s use cases reach persistence through.

`Protocol`s rather than base classes: the PostgreSQL adapter arrives in this module's
`adapters/`, and the use-case tests pass in-memory stand-ins at this same seam (harness §4).

No method commits — one request is one transaction, opened and committed by the HTTP adapter
layer (ADR-0002). A duplicate natural key refuses at `add` and at `save` (an edit can collide
too, by renaming or by moving an endpoint), and a save whose expected revision no longer
matches refuses rather than overwriting, so two concurrent administrators get one
deterministic outcome instead of a silent last-write-wins.

The lookup names say what they match: `by_id` is the row's UUID (§5.15's URL identity), and
`page_of` is the one paged listing a repository serves.
"""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from factory_sop.device.model import InferenceBackend, InferenceHost


class InferenceHostRepository(Protocol):
    """`device_inference_host`."""

    def add(self, host: InferenceHost) -> None:
        """Insert a host.

        Raises `DeviceRefusedError` with `INFERENCE_HOST_NAME_TAKEN` on a name that is
        already taken: that is a real unique constraint rather than a check the caller makes
        first, so two concurrent creations cannot both succeed.
        """
        ...

    def save(self, host: InferenceHost, *, expected_revision: int) -> None:
        """Write `host`'s fields over the stored row, but only if it still sits at
        `expected_revision`. Raises `STALE_REVISION` when another write moved the row first,
        `INFERENCE_HOST_NOT_FOUND` when the row is gone entirely (§5.15), and
        `INFERENCE_HOST_NAME_TAKEN` when the edit took a name another row already holds.
        """
        ...

    def by_id(self, host_id: UUID) -> InferenceHost | None:
        """Return the host whose row identity is `host_id`, if it still exists."""
        ...

    def remove(self, host_id: UUID) -> bool:
        """Delete the host, reporting whether a row was actually removed.

        Raises `INFERENCE_HOST_HAS_BACKENDS` if a backend still references this host — the
        foreign key refuses the delete, and the use case refuses it first with the same code.
        """
        ...

    def page_of(self, *, page: int, page_size: int) -> tuple[list[InferenceHost], int]:
        """One page of hosts, newest first, with the unpaginated total (§5.15's envelope)."""
        ...


class InferenceBackendRepository(Protocol):
    """`device_inference_backend`."""

    def add(self, backend: InferenceBackend) -> None:
        """Insert a backend.

        Raises `INFERENCE_BACKEND_ENDPOINT_TAKEN` when the host already carries an endpoint
        with this `base_url` — one row per process endpoint — and `INFERENCE_HOST_NOT_FOUND`
        when the foreign key's host row is gone (a use-case race reaching the constraint).
        """
        ...

    def save(self, backend: InferenceBackend, *, expected_revision: int) -> None:
        """Write `backend`'s fields over the stored row, but only at `expected_revision`.

        Raises `INFERENCE_BACKEND_ENDPOINT_TAKEN` when the edit took another row's
        `(host_id, base_url)` pair.
        """
        ...

    def by_id(self, backend_id: UUID) -> InferenceBackend | None:
        """Return the backend whose row identity is `backend_id`, if it still exists."""
        ...

    def remove(self, backend_id: UUID) -> bool:
        """Delete the backend, reporting whether a row was actually removed."""
        ...

    def any_for_host(self, host_id: UUID) -> bool:
        """Report whether any backend still hangs off the host.

        What `delete_host` consults before removing one; the foreign key holds the same rule
        in the database.
        """
        ...

    def page_of(
        self, *, page: int, page_size: int, host_id: UUID | None
    ) -> tuple[list[InferenceBackend], int]:
        """One page of backends — optionally only one host's — newest first, with the
        unpaginated total (§5.15's envelope)."""
        ...
