"""Outward resource identity: UUIDv7. Shared infrastructure, like `observability`.

§5.15 fixes UUIDv7 as the outward identity of every resource, with natural keys (a station
code, a login name) kept as unique constraints and import-matching keys rather than as URL
identity. Version 7 specifically, because its leading field is the creation timestamp: rows
arrive in index order instead of scattering a B-tree the way v4 does, and an identifier sorts
by when it was made.

Python 3.12's `uuid` module offers v1, v3, v4 and v5 — `uuid.uuid7` arrives in 3.14, and the
interpreter is pinned to 3.12 (§六). The RFC's layout is a few lines of bit packing, so it is
written out here rather than taken as a dependency; the alternative on offer is a compiled
extension, which would have to be built for the offline deployment for this.

Ordering **within** one millisecond is not guaranteed: the RFC's optional monotonic counter
is not implemented, because nothing sorts by these identifiers. A listing orders by the
domain column it is about — `created_at`, a publication instant — and two rows written in the
same millisecond have equal values there too.
"""

from __future__ import annotations

import secrets
import time
import uuid

_VERSION = 7
_VARIANT_RFC_9562 = 0b10


def new_id() -> uuid.UUID:
    """Mint a UUIDv7: 48 bits of Unix milliseconds, then 74 random bits."""
    milliseconds = time.time_ns() // 1_000_000
    value = int.from_bytes(milliseconds.to_bytes(6, "big") + secrets.token_bytes(10), "big")
    # Overwrite the four version bits (RFC 9562 §4.2) and the two variant bits (§4.1). The
    # 74 bits they leave untouched stay random.
    value &= ~(0b1111 << 76)
    value |= _VERSION << 76
    value &= ~(0b11 << 62)
    value |= _VARIANT_RFC_9562 << 62
    return uuid.UUID(int=value)
