"""What `device` owns in this slice: the inference host and its process endpoints.

Pure domain types — no SQLAlchemy, no FastAPI, no clock, no network. Every instant arrives
from a caller holding one. The vocabulary is `CONTEXT.md`'s: the 推理机 is the physical
machine; the 推理后端 is a process endpoint carrying one template configuration — one host
runs several, and cameras will hang off the backend, not the machine (§5.10).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from urllib.parse import urlsplit
from uuid import UUID


class DeviceStatus(StrEnum):
    """Whether a configurable device still takes part in new bindings and operation.

    An enum rather than an `is_active` boolean: `CONTEXT.md` gives 停用 its own definition
    across every mutable configuration object. Deactivation is reversible and never
    cascades — a deactivated host's backends keep their rows and their history.
    """

    ACTIVE = "active"
    DEACTIVATED = "deactivated"


class ConnectionState(StrEnum):
    """What the last real connection test observed about a backend's endpoint.

    Three values, because Q31 fixes exactly these: a record that has never been tested is
    未验证 — not "working" — and only a test that truly reached the endpoint may say
    `success`. The facts belong to the placement (the host and endpoint a test observed):
    moving the backend, or pointing it at another endpoint, retires what was self-reported.
    """

    UNVERIFIED = "unverified"
    SUCCESS = "success"
    FAILURE = "failure"


def carries_userinfo(url: str) -> bool:
    """Report whether `url` is unsafe to persist as a device endpoint.

    A device endpoint may not carry userinfo, a query, or a fragment. Userinfo embeds a
    credential, while query and fragment components are common places for bearer tokens and
    other credentials to hide. The literal delimiters are rejected too, so an empty ``?`` or
    ``#`` cannot later become a secret-bearing URL through normalization. ADR-0008 and §5.12
    keep credentials off the center: not stored, not logged, and not returned by the API — so
    every URL this module persists passes this one check at its input contract.
    """
    parts = urlsplit(url)
    return parts.username is not None or parts.password is not None or "?" in url or "#" in url


@dataclass(frozen=True, slots=True)
class InferenceHost:
    """A physical inference machine: the autonomous judgment unit of its stations.

    The recording window and the disk watermark are per-host because the disk is the host's
    (§5.19), and the watermark is a safety threshold the edge runtime reports against.
    Health and clock offset are the host's to report; they arrive with the report contract.
    """

    id: UUID
    # Unique, and the natural key an operator matches on — never the URL identity (§5.15).
    name: str
    address: str
    mediamtx_address: str | None
    recording_window_seconds: int
    disk_watermark_percent: int
    status: DeviceStatus
    revision: int
    created_by: UUID
    updated_by: UUID
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        if self.mediamtx_address is not None and carries_userinfo(self.mediamtx_address):
            raise ValueError("device URLs cannot carry credentials, queries, or fragments")


@dataclass(frozen=True, slots=True)
class InferenceBackend:
    """One inference-service process endpoint carrying one template configuration.

    The connection fields record what the center observed by really asking the endpoint
    (§5.13). The center is not a model registry — no model table, no binding authority,
    nothing to distribute; `self_reported_model_ids` is an observed fact about a machine,
    kept for judgment provenance.
    """

    id: UUID
    # The one host this endpoint runs on. The foreign key is the database half of the
    # topology constraint; the use case is the other half, and the migration's trigger is
    # the third: a backend row may only be written while its host is active.
    host_id: UUID
    base_url: str
    # The one template configuration this endpoint carries — single-valued in the schema by
    # construction, which is the database half of "one backend, one template configuration"
    # (§5.10). Nullable until the `template` module lands (C5): its migration adds the foreign
    # key and the binding use case that writes it, and until then no writer exists, so the
    # column reads back `None` on every row.
    template_version_id: UUID | None
    status: DeviceStatus
    connection_state: ConnectionState
    connection_checked_at: datetime | None
    # Why the last test failed, for the operator's next step. Never a credential — the
    # endpoint URL carries none (ADR-0008: credentials live on the inference host).
    connection_detail: str | None
    self_reported_model_ids: tuple[str, ...]
    self_reported_at: datetime | None
    revision: int
    created_by: UUID
    updated_by: UUID
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        if carries_userinfo(self.base_url):
            raise ValueError("device URLs cannot carry credentials, queries, or fragments")
