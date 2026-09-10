"""`auth`'s cross-module contract: the authorization decision every module's use cases make.

This file exists because `device` is the first module whose use cases call `auth` (harness §3:
`api.py` exposes only the subset other modules actually call, and it is the one permitted
cross-module import target). The entries are re-exports rather than new behavior — the
decision itself is `auth/authorization.py`, the vocabulary is `auth/permissions.py` — so an
import of this module cannot drift from what `auth` enforces.

A module's use case takes a `Caller` as an argument, calls `authorize(caller, Permission.X)`
as its first statement, and never reaches into `auth`'s tables or adapters to do it. The HTTP
layer builds the `Caller` (see `auth/adapters/dependencies.py`); an ARQ worker or a smoke
script builds the same value the same way. The last two entries serve the caller side of
other modules' HTTP adapters: the annotated dependency that resolves a request into a
`Caller`, and the OpenAPI metadata a route declares its permission with.
"""

from factory_sop.auth.adapters.dependencies import Authorized, needs, needs_any
from factory_sop.auth.authorization import Caller, authorize
from factory_sop.auth.permissions import Permission

__all__ = ["Authorized", "Caller", "Permission", "authorize", "needs", "needs_any"]
