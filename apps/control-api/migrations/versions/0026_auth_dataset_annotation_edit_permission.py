"""auth：登记训练数据集标注编辑权限。"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0026"
down_revision: str | None = "0025"

_PERMISSION = "dataset.dataset.edit"
RAW_SQL_TABLES = frozenset({"auth_permission", "auth_role_permission"})


def upgrade() -> None:
    op.bulk_insert(
        sa.table("auth_permission", sa.column("code", sa.String)),
        [{"code": _PERMISSION}],
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
            [{"role_id": role_ids[0], "permission": _PERMISSION}],
        )


def downgrade() -> None:
    role_permissions = sa.table(
        "auth_role_permission",
        sa.column("permission", sa.String),
    )
    permissions = sa.table("auth_permission", sa.column("code", sa.String))
    bind = op.get_bind()
    bind.execute(sa.delete(role_permissions).where(role_permissions.c.permission == _PERMISSION))
    bind.execute(sa.delete(permissions).where(permissions.c.code == _PERMISSION))
