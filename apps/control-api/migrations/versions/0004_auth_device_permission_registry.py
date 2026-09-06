"""auth: the registry rows for the permissions `device` enforces

Revision ID: 0004
Revises: 0003
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"

# The members `device` registers in `auth/permissions.py` with this same change, in enum
# order. The registry belongs to `auth`; a role cannot grant a permission that has no row
# here, so the rows and the enum members are one change.
DEVICE_PERMISSIONS = (
    "device.inference_host.view",
    "device.inference_host.edit",
    "device.inference_host.delete",
    "device.inference_backend.view",
    "device.inference_backend.edit",
    "device.inference_backend.delete",
)

# The downgrade removes rows from these auth-owned registry tables. The ownership checker
# cannot infer targets from a Core DELETE passed to `execute`, so the declaration makes the
# touched tables explicit just as the device trigger migration does.
RAW_SQL_TABLES = frozenset({"auth_permission", "auth_role_permission"})


def upgrade() -> None:
    op.bulk_insert(
        sa.table("auth_permission", sa.column("code", sa.String)),
        [{"code": code} for code in DEVICE_PERMISSIONS],
    )
    _grant_device_permissions_to_the_administrator_role()


# The registry rows and the grants are `bulk_insert` operations, whose table ownership the
# gate reads statically; the one lookup of the role to extend runs as SQL, and it is a read —
# the same allowance 0003's backfill uses.
ADMINISTRATOR_ROLE_CODE = "system_administrator"


def _grant_device_permissions_to_the_administrator_role() -> None:
    """Extend the seeded administrator role with the permissions this migration registers.

    A deployment upgraded from before C2.2 holds the backfilled `system_administrator` role,
    whose grants are a snapshot of the permissions that existed when it was seeded. Without
    this, nobody on such a deployment could grant `device.*`: the account that may create
    roles holds nothing that covers the new members, and the only repair would be SQL against
    production. The role is found by its unique code; a deployment that has not bootstrapped
    yet has no role row, and its `register_first_operator` grants the whole enum when it runs.
    A renamed or deleted seed role is an administrator's deliberate act, not an upgrade state
    to repair — the lookup finding no row is that case, and it leaves the registry rows alone.
    """
    # The code is a compile-time constant, not caller input. The lookup is the same
    # read-only literal SELECT 0003's backfill uses, which the ownership checker accepts.
    role_ids = list(
        op.get_bind()
        .execute(sa.text("SELECT id FROM auth_role WHERE code = 'system_administrator'"))
        .scalars()
    )
    if not role_ids:
        return
    op.bulk_insert(
        sa.table(
            "auth_role_permission",
            sa.column("role_id", sa.Uuid),
            sa.column("permission", sa.String),
        ),
        [{"role_id": role_ids[0], "permission": code} for code in DEVICE_PERMISSIONS],
    )


def downgrade() -> None:
    """Remove this module's registry rows and grants before returning to revision 0003."""
    role_permissions = sa.table(
        "auth_role_permission",
        sa.column("permission", sa.String),
    )
    permissions = sa.table("auth_permission", sa.column("code", sa.String))
    bind = op.get_bind()
    # Grants reference the registry rows, so delete the dependent rows first. Both statements
    # are limited to this migration's fixed literals; no administrator data is removed.
    bind.execute(
        sa.delete(role_permissions).where(role_permissions.c.permission.in_(DEVICE_PERMISSIONS))
    )
    bind.execute(sa.delete(permissions).where(permissions.c.code.in_(DEVICE_PERMISSIONS)))
