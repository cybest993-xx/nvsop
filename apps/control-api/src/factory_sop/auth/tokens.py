"""The session token: what the browser carries, and what the table stores instead.

Two different values with two different lifetimes. The token itself is handed to the browser
once, in a `HttpOnly` cookie (§六); what `auth_session` holds is its fingerprint. So a
database dump — a backup, a support export, a read-only reporting connection — cannot be
replayed as a login, and the row remains what §六 asks a session record to be: revocable
server-side state rather than a copy of the credential.

A plain SHA-256 is the right fingerprint here, and Argon2id is not. `passwords.py` is
deliberately expensive because a password has perhaps 40 bits of entropy and must survive an
offline attack on the table. This token has 256 random bits, so there is nothing to guess and
nothing to be slow about; making the lookup expensive would only put an Argon2 hash on the
path of every authenticated request.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass

# 32 bytes, URL-safe base64: 256 bits of entropy in 43 cookie-safe characters.
_TOKEN_BYTES = 32


@dataclass(frozen=True, slots=True)
class SessionToken:
    """A session's secret, as the browser holds it.

    A type rather than a bare `str`, so a signature cannot take the token where it meant the
    fingerprint: the two are both strings, and swapping them would store the secret and look
    up by something the caller never has.
    """

    value: str

    @classmethod
    def issue(cls) -> SessionToken:
        """Mint a fresh token for a session that is being opened."""
        return cls(value=secrets.token_urlsafe(_TOKEN_BYTES))


def fingerprint(token: SessionToken) -> str:
    """Return the value `auth_session` stores for `token`: a hex SHA-256 digest.

    Used both to write the row when a session opens and to find it on each request, so the
    lookup is by exact match on a fixed-width indexed column.
    """
    return hashlib.sha256(token.value.encode("utf-8")).hexdigest()
