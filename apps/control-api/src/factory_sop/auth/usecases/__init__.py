"""`auth`'s use-case layer: where authorization is enforced (§5.15).

Authorization is checked here rather than at the HTTP route, because an ARQ worker, the
inference-host report intake and a smoke script call the same use cases — enforcing only at
the route would leave the permission to each caller's discretion. The session use cases in
this package are the ones that need no permission: they are how a caller acquires an identity
in the first place.

Every use case takes its repositories, its policy and its `now` as arguments. Nothing here
reads a clock or the process environment, so a lifetime rule is proved by passing two
datetimes.
"""
