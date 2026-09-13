"""The seam a connection test reaches the network through.

The probe is an adapter like the repository: the use case takes one as an argument, the
urllib implementation sits in this module's `adapters/`, and the use-case tests pass a
scripted stand-in at this same seam. Q31 is why the seam exists: a test connection must be
a real request, and a use case that cannot tell one from a stub could not be held to that.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from factory_sop.device.model import ConnectionState


@dataclass(frozen=True, slots=True)
class ProbeReport:
    """What one real request to a backend's endpoint observed.

    `status` is `SUCCESS` or `FAILURE` — never `UNVERIFIED`, the state of a record no test
    has touched. On success `model_ids` carries the endpoint's self-reported identities
    (§5.13); on failure `detail` says why, in one line an operator can act on.
    """

    status: ConnectionState
    model_ids: tuple[str, ...] = ()
    detail: str | None = None


class ConnectionProbe(Protocol):
    """The one method a connection test is: ask the endpoint, report what happened."""

    def probe(self, *, base_url: str) -> ProbeReport:
        """Make a real request to the endpoint at `base_url` and report the outcome."""
        ...
