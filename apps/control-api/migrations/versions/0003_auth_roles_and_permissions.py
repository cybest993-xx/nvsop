"""auth: roles, their permissions, who holds them, and the permission registry

Revision ID: 0003
Revises: 0002
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"

# The registry rows for the permissions this change introduces, in enum order. When a later
# module registers its own members in `auth/permissions.py`, the same change adds a new
# `auth`-prefixed migration inserting their rows here — the registry belongs to `auth`, and its
# growth is part of this linear history rather than a runtime side effect.
REGISTERED_PERMISSIONS = (
    "auth.user.view",
    "auth.user.edit",
    "auth.user.delete",
    "auth.role.view",
    "auth.role.edit",
    "auth.role.delete",
)


def upgrade() -> None:
    op.create_table(
        "auth_permission",
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.PrimaryKeyConstraint("code", name=op.f("pk_auth_permission")),
    )
    op.bulk_insert(
        sa.table("auth_permission", sa.column("code", sa.String)),
        [{"code": code} for code in REGISTERED_PERMISSIONS],
    )
    op.create_table(
        "auth_role",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("updated_by", sa.Uuid(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_auth_role")),
        sa.UniqueConstraint("code", name=op.f("uq_auth_role_code")),
    )
    op.create_table(
        "auth_role_permission",
        sa.Column("role_id", sa.Uuid(), nullable=False),
        # A plain string, not a database enum: this set gains a member whenever a module is
        # added, and an enum would make each of those an `ALTER TYPE` in `auth` for a value
        # `auth` does not own. Membership is enforced by `parse_permission` on the way in and by
        # the foreign key to the registry on the way down.
        sa.Column("permission", sa.String(length=64), nullable=False),
        sa.ForeignKeyConstraint(
            ["role_id"],
            ["auth_role.id"],
            name=op.f("fk_auth_role_permission_role_id_auth_role"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["permission"],
            ["auth_permission.code"],
            name=op.f("fk_auth_role_permission_permission_auth_permission"),
            ondelete="RESTRICT",
        ),
        # Composite primary key: a role either grants a permission or it does not, and a
        # duplicate row would be a second grant of the same thing.
        sa.PrimaryKeyConstraint("role_id", "permission", name=op.f("pk_auth_role_permission")),
    )
    # The last-administration guard asks "which active accounts hold this permission", which is a
    # lookup by permission across every role. Without this it is a scan of the table.
    op.create_index(
        op.f("ix_auth_role_permission_permission"),
        "auth_role_permission",
        ["permission"],
        unique=False,
    )
    op.create_table(
        "auth_user_role",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("role_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["auth_user.id"],
            name=op.f("fk_auth_user_role_user_id_auth_user"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["role_id"],
            ["auth_role.id"],
            name=op.f("fk_auth_user_role_role_id_auth_role"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("user_id", "role_id", name=op.f("pk_auth_user_role")),
    )
    # The primary key already indexes `user_id` first, which serves "this account's roles". This
    # one serves the other direction — "who holds this role" — which the guard also asks.
    op.create_index(op.f("ix_auth_user_role_role_id"), "auth_user_role", ["role_id"], unique=False)
    # §5.15 carries 变更归属 in `created_by` / `updated_by`; the account rows predate the rule,
    # so the columns arrive nullable and stay null on rows no one has written since.
    op.add_column("auth_user", sa.Column("created_by", sa.Uuid(), nullable=True))
    op.add_column("auth_user", sa.Column("updated_by", sa.Uuid(), nullable=True))
    _backfill_administrator_role()


# A fixed identity for the backfilled seed role: the migration must be deterministic and
# re-runnable against the same state (alembic runs it once, but a rehearsal or a restored
# backup re-applies it against rows that may already carry this id), so the id is written
# here rather than minted at apply time.
BACKFILLED_ADMINISTRATOR_ROLE_ID = "018f9c2a-0000-7000-8000-c2a200000001"


# The backfill's writes are `bulk_insert` operations, whose table ownership the gate reads
# statically; only the one lookup of the account to attribute runs as SQL, and it is a read.
AUTH_ROLE_TABLE = sa.table(
    "auth_role",
    sa.column("id", sa.Uuid),
    sa.column("code", sa.String),
    sa.column("name", sa.String),
    sa.column("created_by", sa.Uuid),
    sa.column("updated_by", sa.Uuid),
)
AUTH_ROLE_PERMISSION_TABLE = sa.table(
    "auth_role_permission",
    sa.column("role_id", sa.Uuid),
    sa.column("permission", sa.String),
)
AUTH_USER_ROLE_TABLE = sa.table(
    "auth_user_role",
    sa.column("user_id", sa.Uuid),
    sa.column("role_id", sa.Uuid),
)


def _backfill_administrator_role() -> None:
    """Give a pre-existing C2.1 deployment its administrator role back.

    A deployment upgraded from 0002 holds the bootstrap account — and nothing else, because
    the bootstrap guard allowed only one — but no roles: the role tables are created by this
    very revision. Without a backfill, that administrator logs in to `permissions: []` and can
    no longer create a role or grant anything through the product, while `register_first_operator`
    skips forever because the guard row exists — the only repair would be SQL against production.

    The backfill seeds the same ordinary role `register_first_operator` creates and assigns it
    to the deployment's first account (earliest `id`: these are UUIDv7, which sort by creation
    time). A fresh deployment has no accounts and receives nothing here — its bootstrap creates
    the role when it runs.
    """
    first_account_id = (
        op.get_bind().execute(sa.text("SELECT id FROM auth_user ORDER BY id LIMIT 1")).scalar()
    )
    if first_account_id is None:
        return
    role_id = uuid.UUID(BACKFILLED_ADMINISTRATOR_ROLE_ID)
    op.bulk_insert(
        AUTH_ROLE_TABLE,
        [
            {
                "id": role_id,
                "code": "system_administrator",
                "name": "系统管理员",
                "created_by": first_account_id,
                "updated_by": first_account_id,
            }
        ],
    )
    op.bulk_insert(
        AUTH_ROLE_PERMISSION_TABLE,
        [{"role_id": role_id, "permission": code} for code in REGISTERED_PERMISSIONS],
    )
    op.bulk_insert(
        AUTH_USER_ROLE_TABLE,
        [{"user_id": first_account_id, "role_id": role_id}],
    )


def downgrade() -> None:
    op.drop_column("auth_user", "updated_by")
    op.drop_column("auth_user", "created_by")
    op.drop_index(op.f("ix_auth_user_role_role_id"), table_name="auth_user_role")
    op.drop_table("auth_user_role")
    op.drop_index(op.f("ix_auth_role_permission_permission"), table_name="auth_role_permission")
    op.drop_table("auth_role_permission")
    op.drop_table("auth_role")
    op.drop_table("auth_permission")
