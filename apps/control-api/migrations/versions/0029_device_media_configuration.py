"""device：保存 MediaMTX 回放地址和相机媒体策略。"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0029"
down_revision: str | None = "0028"

_MEDIA_PATH_VALUES = ("passthrough", "cpu_transcode")
_RECORDING_MODE_VALUES = ("preview_only", "continuous")


def upgrade() -> None:
    media_path_mode = postgresql.ENUM(
        *_MEDIA_PATH_VALUES,
        name="device_media_path_mode",
    )
    recording_mode = postgresql.ENUM(
        *_RECORDING_MODE_VALUES,
        name="device_recording_mode",
    )
    bind = op.get_bind()
    media_path_mode.create(bind, checkfirst=True)
    recording_mode.create(bind, checkfirst=True)

    op.add_column(
        "device_inference_host",
        sa.Column("mediamtx_playback_address", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "device_camera",
        sa.Column(
            "media_path_mode",
            sa.Enum(
                *_MEDIA_PATH_VALUES,
                name="device_media_path_mode",
                create_type=False,
            ),
            nullable=False,
            server_default="passthrough",
        ),
    )
    op.add_column(
        "device_camera",
        sa.Column(
            "recording_mode",
            sa.Enum(
                *_RECORDING_MODE_VALUES,
                name="device_recording_mode",
                create_type=False,
            ),
            nullable=False,
            server_default="continuous",
        ),
    )
    op.alter_column("device_camera", "media_path_mode", server_default=None)
    op.alter_column("device_camera", "recording_mode", server_default=None)


def downgrade() -> None:
    op.drop_column("device_camera", "recording_mode")
    op.drop_column("device_camera", "media_path_mode")
    op.drop_column("device_inference_host", "mediamtx_playback_address")
    bind = op.get_bind()
    postgresql.ENUM(*_RECORDING_MODE_VALUES, name="device_recording_mode").drop(
        bind, checkfirst=True
    )
    postgresql.ENUM(*_MEDIA_PATH_VALUES, name="device_media_path_mode").drop(bind, checkfirst=True)
