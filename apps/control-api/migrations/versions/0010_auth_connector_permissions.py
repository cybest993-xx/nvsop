"""auth：登记连接器管理权限。"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"

CONNECTOR_PERMISSIONS = (
    "device.connector.view",
    "device.connector.edit",
    "device.connector.delete",
)
RAW_SQL_TABLES = frozenset({"auth_permission", "auth_role_permission"})


def upgrade() -> None:
    op.bulk_insert(
        sa.table("auth_permission", sa.column("code", sa.String)),
        [{"code": permission} for permission in CONNECTOR_PERMISSIONS],
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
                for permission in CONNECTOR_PERMISSIONS
            ],
        )


def downgrade() -> None:
    """移除本迁移登记的权限和管理员角色授予。"""
    bind = op.get_bind()
    role_permissions = sa.table("auth_role_permission", sa.column("permission", sa.String))
    permissions = sa.table("auth_permission", sa.column("code", sa.String))
    bind.execute(
        sa.delete(role_permissions).where(role_permissions.c.permission.in_(CONNECTOR_PERMISSIONS))
    )
    bind.execute(sa.delete(permissions).where(permissions.c.code.in_(CONNECTOR_PERMISSIONS)))
