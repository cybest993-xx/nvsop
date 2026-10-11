"""auth：登记具体证据查看权限，默认授予系统管理员。"""

import sqlalchemy as sa
from alembic import op

revision: str = "0055"
down_revision: str | None = "0054"
RAW_SQL_TABLES = frozenset({"auth_role", "auth_role_permission", "auth_permission"})


def upgrade() -> None:
    op.bulk_insert(
        sa.table("auth_permission", sa.column("code", sa.String)),
        [{"code": "evidence.evidence.view"}],
    )
    op.get_bind().execute(
        sa.text(
            "INSERT INTO auth_role_permission (role_id, permission) "
            "SELECT id, :permission FROM auth_role WHERE code = :role"
        ),
        {"permission": "evidence.evidence.view", "role": "system_administrator"},
    )


def downgrade() -> None:
    permissions = sa.table("auth_permission", sa.column("code", sa.String))
    grants = sa.table("auth_role_permission", sa.column("permission", sa.String))
    bind = op.get_bind()
    bind.execute(sa.delete(grants).where(grants.c.permission == "evidence.evidence.view"))
    bind.execute(sa.delete(permissions).where(permissions.c.code == "evidence.evidence.view"))
