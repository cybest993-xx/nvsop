"""auth: register the inference-backend permissions used by ``device``

Revision ID: 0006
Revises: 0005

The device host permissions landed in 0004. Backend management is a separate vertical slice,
so its three permission registry rows and the upgrade path for the seeded administrator role
land here rather than rewriting the already-applied migration.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"

BACKEND_PERMISSIONS = (
    "device.inference_backend.view",
    "device.inference_backend.edit",
    "device.inference_backend.delete",
)

# The ownership checker cannot infer the target of Core DELETE statements passed to execute.
RAW_SQL_TABLES = frozenset({"auth_permission", "auth_role_permission"})
ADMINISTRATOR_ROLE_CODE = "system_administrator"


def upgrade() -> None:
    op.bulk_insert(
        sa.table("auth_permission", sa.column("code", sa.String)),
        [{"code": code} for code in BACKEND_PERMISSIONS],
    )
    _grant_to_existing_administrator()


def _grant_to_existing_administrator() -> None:
    """Extend an already-bootstrapped administrator without touching renamed roles."""
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
        [{"role_id": role_ids[0], "permission": code} for code in BACKEND_PERMISSIONS],
    )


def downgrade() -> None:
    """Remove only the rows this migration registered, grants before registry entries."""
    bind = op.get_bind()
    role_permissions = sa.table(
        "auth_role_permission",
        sa.column("permission", sa.String),
    )
    permissions = sa.table("auth_permission", sa.column("code", sa.String))
    bind.execute(
        sa.delete(role_permissions).where(role_permissions.c.permission.in_(BACKEND_PERMISSIONS))
    )
    bind.execute(sa.delete(permissions).where(permissions.c.code.in_(BACKEND_PERMISSIONS)))
