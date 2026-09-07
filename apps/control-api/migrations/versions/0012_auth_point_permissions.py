"""auth：登记点位管理权限。"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"

POINT_PERMISSIONS = (
    "device.point.view",
    "device.point.edit",
    "device.point.delete",
)
RAW_SQL_TABLES = frozenset({"auth_permission", "auth_role_permission"})


def upgrade() -> None:
    op.bulk_insert(
        sa.table("auth_permission", sa.column("code", sa.String)),
        [{"code": permission} for permission in POINT_PERMISSIONS],
    )
    role_ids = list(
        op.get_bind()
        .execute(sa.text("SELECT id FROM auth_role WHERE code = 'system_administrator'"))
        .scalars()
    )
    if role_ids:
        op.bulk_insert(
            sa.table(
                "auth_role_permission",
                sa.column("role_id", sa.Uuid),
                sa.column("permission", sa.String),
            ),
            [
                {"role_id": role_ids[0], "permission": permission}
                for permission in POINT_PERMISSIONS
            ],
        )


def downgrade() -> None:
    bind = op.get_bind()
    role_permissions = sa.table("auth_role_permission", sa.column("permission", sa.String))
    permissions = sa.table("auth_permission", sa.column("code", sa.String))
    bind.execute(
        sa.delete(role_permissions).where(role_permissions.c.permission.in_(POINT_PERMISSIONS))
    )
    bind.execute(sa.delete(permissions).where(permissions.c.code.in_(POINT_PERMISSIONS)))
