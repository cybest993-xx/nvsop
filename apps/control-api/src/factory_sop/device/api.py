"""The deliberately small cross-module contract for ``device``.

C4.1 has no caller in another center module yet: stations, cameras, connectors, template
binding, and report intake arrive in later slices. The CRUD use cases therefore stay private
to this module's HTTP adapter and direct callers; re-exporting them here would turn this file
into a second, pass-through public surface and violate harness §3 / control-plane §5.16.

When a later module needs device behavior, add only the named deep operation that caller uses
and document its role and invocation here. Its implementation remains in ``usecases/`` and
its persistence remains behind the repository seam; this file only defines the cross-module
contract.
"""

from __future__ import annotations

# No cross-module device behavior is consumed in this slice. Keeping the export list explicit
# makes accidental CRUD publication visible in review and to any future boundary check.
__all__: tuple[str, ...] = ()
