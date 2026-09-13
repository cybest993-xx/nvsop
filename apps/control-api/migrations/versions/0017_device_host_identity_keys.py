"""device：把主机控制面身份迁移为非秘密公钥。"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0017"
down_revision: str | None = "0016"

# `_has_non_empty_value` uses a read-only SELECT through a dynamic column name; declare its
# single touched table so the static ownership check can verify the raw statement's scope.
RAW_SQL_TABLES = frozenset({"device_inference_host"})


def upgrade() -> None:
    """删除旧口令派生物，只保留主机公钥。

    `credential_hash` 不能转换为 RSA 公钥；若存量主机仍有非空旧值，静默删除会让旧部署
    失去可追溯的身份材料。因此先在迁移事务中拒绝该状态，只有空旧指纹才允许升级，并由
    后续登记流程重新配置公钥。
    """
    if _has_non_empty_value("credential_hash"):
        raise RuntimeError(
            "0017 cannot remove non-empty credential_hash values; migrate or reconfigure "
            "the inference host identity before upgrading"
        )

    op.add_column(
        "device_pending_command",
        sa.Column("result_credentials_configured", sa.Boolean(), nullable=True),
    )
    op.create_table(
        "device_inference_host_identity_nonce",
        sa.Column("host_id", sa.Uuid(), nullable=False),
        sa.Column("nonce", sa.String(length=128), nullable=False),
        sa.Column("seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["host_id"], ["device_inference_host.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("host_id", "nonce"),
    )
    op.add_column(
        "device_inference_host",
        sa.Column("identity_public_key", sa.String(length=4096), nullable=True),
    )
    op.drop_column("device_inference_host", "credential_hash")


def downgrade() -> None:
    """回退到旧表形状；已登记公钥的主机必须先由运维处理。

    公钥也不能转换回旧的口令指纹。没有已登记公钥时，回退会把所有主机恢复为旧方案的
    “未配置”空值；它不会声称恢复任何原始凭据。
    """
    if _has_non_empty_value("identity_public_key"):
        raise RuntimeError(
            "0017 cannot drop non-empty identity_public_key values during downgrade; "
            "remove or migrate the configured host identities first"
        )

    op.drop_table("device_inference_host_identity_nonce")
    op.drop_column("device_pending_command", "result_credentials_configured")
    op.add_column(
        "device_inference_host",
        sa.Column("credential_hash", sa.String(length=64), nullable=False, server_default=""),
    )
    op.drop_column("device_inference_host", "identity_public_key")


def _has_non_empty_value(column: str) -> bool:
    """在执行 0017 的任何 DDL 前检查不可逆的身份字段。"""
    if column not in {"credential_hash", "identity_public_key"}:
        raise ValueError(f"unsupported identity migration column: {column}")
    result = op.get_bind().execute(
        sa.text(
            f"SELECT EXISTS ("
            f"SELECT 1 FROM device_inference_host "
            f"WHERE {column} IS NOT NULL AND {column} <> ''"
            f")"
        )
    )
    return bool(result.scalar_one())
