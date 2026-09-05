"""auth: local accounts and their sessions

Revision ID: 0001
Revises:
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None


def upgrade() -> None:
    op.create_table(
        "auth_user",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("login_name", sa.String(length=64), nullable=False),
        sa.Column("display_name", sa.String(length=128), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column(
            "status",
            sa.Enum("active", "deactivated", name="auth_user_status"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_auth_user")),
        sa.UniqueConstraint("login_name", name=op.f("uq_auth_user_login_name")),
    )
    op.create_table(
        "auth_session",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("token_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["auth_user.id"],
            name=op.f("fk_auth_session_user_id_auth_user"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_auth_session")),
        sa.UniqueConstraint("token_fingerprint", name=op.f("uq_auth_session_token_fingerprint")),
    )
    op.create_index(op.f("ix_auth_session_user_id"), "auth_session", ["user_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_auth_session_user_id"), table_name="auth_session")
    op.drop_table("auth_session")
    op.drop_table("auth_user")
    sa.Enum(name="auth_user_status").drop(op.get_bind())
