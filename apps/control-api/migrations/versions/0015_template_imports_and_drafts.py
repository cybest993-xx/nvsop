"""template：规范化 Excel 导入、SOP 模板身份和可编辑草稿。"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0015"
down_revision: str | None = "0014"


def upgrade() -> None:
    op.create_table(
        "template_import",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("content_type", sa.String(length=255), nullable=False),
        sa.Column("original_document", sa.LargeBinary(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column(
            "status",
            sa.Enum("succeeded", "failed", name="template_import_status"),
            nullable=False,
        ),
        sa.Column("errors", postgresql.JSONB(), nullable=False),
        sa.Column("imported_by", sa.Uuid(), nullable=False),
        sa.Column("imported_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("char_length(filename) > 0", name=op.f("ck_template_import_filename")),
        sa.CheckConstraint("char_length(sha256) = 64", name=op.f("ck_template_import_sha256")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_template_import")),
    )
    op.create_index(
        op.f("ix_template_import_imported_at"),
        "template_import",
        ["imported_at"],
    )

    op.create_table(
        "template_sop_template",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("station_id", sa.Uuid(), nullable=False),
        sa.Column("station_code", sa.String(length=64), nullable=False),
        sa.Column("station_name", sa.String(length=128), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("updated_by", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["station_id"],
            ["device_station.id"],
            name=op.f("fk_template_sop_template_station_id_device_station"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_template_sop_template")),
    )
    op.create_index(
        op.f("ix_template_sop_template_station_id"),
        "template_sop_template",
        ["station_id"],
    )

    op.create_table(
        "template_draft",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("template_id", sa.Uuid(), nullable=False),
        sa.Column("source_import_id", sa.Uuid(), nullable=False),
        sa.Column("steps", postgresql.JSONB(), nullable=False),
        sa.Column(
            "ordering",
            sa.Enum("strict", "unordered", name="template_ordering"),
            nullable=False,
        ),
        sa.Column("runtime_defaults", postgresql.JSONB(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("updated_by", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("revision > 0", name=op.f("ck_template_draft_revision_positive")),
        sa.ForeignKeyConstraint(
            ["source_import_id"],
            ["template_import.id"],
            name=op.f("fk_template_draft_source_import_id_template_import"),
        ),
        sa.ForeignKeyConstraint(
            ["template_id"],
            ["template_sop_template.id"],
            name=op.f("fk_template_draft_template_id_template_sop_template"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_template_draft")),
    )
    op.create_index(op.f("ix_template_draft_template_id"), "template_draft", ["template_id"])
    op.create_index(
        op.f("ix_template_draft_source_import_id"),
        "template_draft",
        ["source_import_id"],
    )
    op.create_index(
        op.f("ix_template_draft_created_at"),
        "template_draft",
        ["created_at"],
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_template_draft_created_at"), table_name="template_draft")
    op.drop_index(op.f("ix_template_draft_source_import_id"), table_name="template_draft")
    op.drop_index(op.f("ix_template_draft_template_id"), table_name="template_draft")
    op.drop_table("template_draft")
    sa.Enum(name="template_ordering").drop(op.get_bind())
    op.drop_index(op.f("ix_template_sop_template_station_id"), table_name="template_sop_template")
    op.drop_table("template_sop_template")
    op.drop_index(op.f("ix_template_import_imported_at"), table_name="template_import")
    op.drop_table("template_import")
    sa.Enum(name="template_import_status").drop(op.get_bind())
