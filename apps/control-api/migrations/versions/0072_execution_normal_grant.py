"""execution：冻结正常交权确认的消费结果。"""

import sqlalchemy as sa
from alembic import op

revision = "0072"
down_revision = "0071"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("execution_normal_handover", sa.Column("new_grant_id", sa.Uuid()))
    op.add_column("execution_normal_handover", sa.Column("granted_at", sa.DateTime(timezone=True)))
    op.add_column("execution_normal_handover", sa.Column("granted_by", sa.Uuid()))
    op.create_check_constraint(
        op.f("ck_execution_normal_handover_grant_together"),
        "execution_normal_handover",
        "(new_grant_id IS NULL) = (granted_at IS NULL) AND "
        "(new_grant_id IS NULL) = (granted_by IS NULL) AND "
        "(new_grant_id IS NULL OR (status = 'confirmed' AND confirmed_at IS NOT NULL "
        "AND new_grant_id <> grant_id AND granted_at >= confirmed_at))",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_execution_normal_handover_grant_together"),
        "execution_normal_handover",
        type_="check",
    )
    for name in ("granted_by", "granted_at", "new_grant_id"):
        op.drop_column("execution_normal_handover", name)
