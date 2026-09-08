"""auth：登记模板草稿的查看与编辑权限。"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"

TEMPLATE_PERMISSIONS = (
    "template.draft.view",
    "template.draft.edit",
)

RAW_SQL_TABLES = frozenset({"auth_permission", "auth_role_permission"})


def upgrade() -> None:
    op.bulk_insert(
        sa.table("auth_permission", sa.column("code", sa.String)),
        [{"code": code} for code in TEMPLATE_PERMISSIONS],
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
            [{"role_id": role_ids[0], "permission": code} for code in TEMPLATE_PERMISSIONS],
        )


def downgrade() -> None:
    role_permissions = sa.table(
        "auth_role_permission",
        sa.column("permission", sa.String),
    )
    permissions = sa.table("auth_permission", sa.column("code", sa.String))
    bind = op.get_bind()
    bind.execute(
        sa.delete(role_permissions).where(role_permissions.c.permission.in_(TEMPLATE_PERMISSIONS))
    )
    bind.execute(sa.delete(permissions).where(permissions.c.code.in_(TEMPLATE_PERMISSIONS)))
