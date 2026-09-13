"""template：不可变版本和发布时保存的确定性制品。"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0018"
down_revision: str | None = "0017"

RAW_SQL_TABLES = frozenset({"template_version"})


def upgrade() -> None:
    op.add_column(
        "template_draft",
        sa.Column("boundary", postgresql.JSONB(), nullable=True),
    )
    op.create_table(
        "template_version",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("template_id", sa.Uuid(), nullable=False),
        sa.Column("source_import_id", sa.Uuid(), nullable=False),
        sa.Column("source_draft_id", sa.Uuid(), nullable=False),
        sa.Column("source_draft_revision", sa.Integer(), nullable=False),
        sa.Column("steps", postgresql.JSONB(), nullable=False),
        sa.Column(
            "ordering",
            sa.Enum("strict", "unordered", name="template_ordering", create_type=False),
            nullable=False,
        ),
        sa.Column("boundary", postgresql.JSONB(), nullable=False),
        sa.Column("runtime_defaults", postgresql.JSONB(), nullable=False),
        sa.Column("actions_json", sa.LargeBinary(), nullable=False),
        sa.Column("actions_sha256", sa.String(length=64), nullable=False),
        sa.Column("vlm_prompts", sa.LargeBinary(), nullable=False),
        sa.Column("vlm_prompts_sha256", sa.String(length=64), nullable=False),
        sa.Column("template_json", sa.LargeBinary(), nullable=False),
        sa.Column("template_sha256", sa.String(length=64), nullable=False),
        sa.Column("manifest_json", sa.LargeBinary(), nullable=False),
        sa.Column("manifest_sha256", sa.String(length=64), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("published_by", sa.Uuid(), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "source_draft_revision > 0",
            name=op.f("ck_template_version_source_draft_revision_positive"),
        ),
        sa.CheckConstraint(
            "char_length(actions_sha256) = 64",
            name=op.f("ck_template_version_actions_sha256"),
        ),
        sa.CheckConstraint(
            "char_length(vlm_prompts_sha256) = 64",
            name=op.f("ck_template_version_vlm_prompts_sha256"),
        ),
        sa.CheckConstraint(
            "char_length(template_sha256) = 64",
            name=op.f("ck_template_version_template_sha256"),
        ),
        sa.CheckConstraint(
            "char_length(manifest_sha256) = 64",
            name=op.f("ck_template_version_manifest_sha256"),
        ),
        sa.CheckConstraint(
            "char_length(sha256) = 64",
            name=op.f("ck_template_version_sha256"),
        ),
        sa.ForeignKeyConstraint(
            ["template_id"],
            ["template_sop_template.id"],
            name=op.f("fk_template_version_template_id_template_sop_template"),
        ),
        sa.ForeignKeyConstraint(
            ["source_import_id"],
            ["template_import.id"],
            name=op.f("fk_template_version_source_import_id_template_import"),
        ),
        sa.ForeignKeyConstraint(
            ["source_draft_id"],
            ["template_draft.id"],
            name=op.f("fk_template_version_source_draft_id_template_draft"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_template_version")),
        sa.UniqueConstraint(
            "source_draft_id",
            "source_draft_revision",
            name=op.f("uq_template_version_source_draft_revision"),
        ),
    )
    op.create_index(
        op.f("ix_template_version_template_id"),
        "template_version",
        ["template_id"],
    )
    op.create_index(
        op.f("ix_template_version_source_import_id"),
        "template_version",
        ["source_import_id"],
    )
    op.create_index(
        op.f("ix_template_version_source_draft_id"),
        "template_version",
        ["source_draft_id"],
    )
    op.create_index(
        op.f("ix_template_version_published_at"),
        "template_version",
        ["published_at"],
    )
    op.execute(
        """
        CREATE FUNCTION template_version_is_immutable() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'template_version rows are immutable'
                USING ERRCODE = 'restrict_violation';
        END;
        $$;
        CREATE TRIGGER template_version_immutable
        BEFORE UPDATE OR DELETE ON template_version
        FOR EACH ROW EXECUTE FUNCTION template_version_is_immutable();
        CREATE TRIGGER template_version_immutable_truncate
        BEFORE TRUNCATE ON template_version
        FOR EACH STATEMENT EXECUTE FUNCTION template_version_is_immutable();
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER template_version_immutable_truncate ON template_version")
    op.execute("DROP TRIGGER template_version_immutable ON template_version")
    op.execute("DROP FUNCTION template_version_is_immutable()")
    op.drop_index(op.f("ix_template_version_published_at"), table_name="template_version")
    op.drop_index(op.f("ix_template_version_source_draft_id"), table_name="template_version")
    op.drop_index(op.f("ix_template_version_source_import_id"), table_name="template_version")
    op.drop_index(op.f("ix_template_version_template_id"), table_name="template_version")
    op.drop_table("template_version")
    op.drop_column("template_draft", "boundary")
