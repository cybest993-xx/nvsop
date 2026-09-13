"""Password hashing for local accounts.

Argon2id, fixed by `solution-and-roadmap.md` §六. The cost parameters are named here rather
than left to the library's defaults, because they are a deployment property this repository
is answerable for: a default that changes between releases would silently re-cost every
login, and a reviewer cannot see a default in the diff.

The values are RFC 9106's second recommended configuration (the low-memory one): 64 MiB,
three passes, four lanes. It is the profile written for a server that also runs other work —
the center backend shares its host with PostgreSQL, Redis, MinIO and the reused training
microservices (§六).
"""

from __future__ import annotations

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

# The shortest password an administrator may set on an account. Length is the only rule: a
# composition rule ("one digit, one symbol") measurably pushes operators towards `Passw0rd!` and
# towards writing it on the terminal, which on a shop floor is the threat that actually happens.
# Enforced in the use cases that set one, and the API contract states the same number so a form
# can refuse it before a round trip.
MINIMUM_PASSWORD_LENGTH = 12

# RFC 9106 §4, second recommended configuration.
_MEMORY_COST_KIB = 65536
_TIME_COST = 3
_PARALLELISM = 4

_hasher = PasswordHasher(
    memory_cost=_MEMORY_COST_KIB,
    time_cost=_TIME_COST,
    parallelism=_PARALLELISM,
)


def hash_password(password: str) -> str:
    """Return the PHC-encoded Argon2id hash to store for `password`.

    The encoding carries its own salt and cost parameters, so `verify_password` needs
    nothing but the stored string — including for a row written under earlier parameters.
    """
    return _hasher.hash(password)


def verify_password(*, password: str, stored_hash: str) -> bool:
    """Report whether `password` is the one `stored_hash` was made from.

    Every failure is one answer: no. A malformed or foreign `stored_hash` is a damaged row
    or some earlier scheme, and the caller's only correct behavior either way is to reject
    the login — distinguishing the two here would offer an attacker a probe for which
    accounts are unusable, and force every call site to handle an exception whose only
    outcome is the same refusal.
    """
    try:
        return _hasher.verify(stored_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False
