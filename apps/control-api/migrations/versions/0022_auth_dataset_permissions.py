"""auth：登记训练数据集查看与专门导入权限。"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0022"
down_revision: str | None = "0021"

DATASET_PERMISSIONS = (
    "dataset.dataset.view",
    "dataset.dataset.import",
)

RAW_SQL_TABLES = frozenset({"auth_permission", "auth_role_permission"})


def upgrade() -> None:
    op.bulk_insert(
        sa.table("auth_permission", sa.column("code", sa.String)),
        [{"code": code} for code in DATASET_PERMISSIONS],
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
            [{"role_id": role_ids[0], "permission": code} for code in DATASET_PERMISSIONS],
        )


def downgrade() -> None:
    role_permissions = sa.table(
        "auth_role_permission",
        sa.column("permission", sa.String),
    )
    permissions = sa.table("auth_permission", sa.column("code", sa.String))
    bind = op.get_bind()
    bind.execute(
        sa.delete(role_permissions).where(role_permissions.c.permission.in_(DATASET_PERMISSIONS))
    )
    bind.execute(sa.delete(permissions).where(permissions.c.code.in_(DATASET_PERMISSIONS)))
