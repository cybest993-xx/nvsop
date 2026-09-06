"""`device`'s use cases. Authorization is enforced here, at the use-case boundary (§5.15):
each use case's first statement is `authorize(caller, ...)` through `auth.api`, so every
caller — HTTP, ARQ worker, smoke script — crosses the same check."""
