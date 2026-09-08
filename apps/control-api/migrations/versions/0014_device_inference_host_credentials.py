"""device：为推理机增加独立的控制面凭据指纹。"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"


def upgrade() -> None:
    op.add_column(
        "device_inference_host",
        sa.Column("credential_hash", sa.String(length=64), nullable=False, server_default=""),
    )


def downgrade() -> None:
    op.drop_column("device_inference_host", "credential_hash")
