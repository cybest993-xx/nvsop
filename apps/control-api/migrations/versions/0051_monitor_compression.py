"""monitor：三张时序事实表的原生 columnstore 策略。

压缩年龄从 retention 完整策略解析；Timescale 原生作业负责压缩与失败统计。
"""

from __future__ import annotations

from typing import cast

import sqlalchemy as sa
from alembic import op

from factory_sop.retention.api import DEFAULT_RETENTION_POLICY, RetentionPolicy

revision: str = "0051"
down_revision: str | None = "0050"

RAW_SQL_TABLES = frozenset(
    {"monitor_reported_decision", "monitor_reported_health", "monitor_observation"}
)

_FACTS = ("monitor_reported_decision", "monitor_reported_health", "monitor_observation")


def _compression_age_seconds() -> int:
    stored = (
        op.get_bind()
        .execute(sa.text("SELECT policy FROM retention_policy WHERE singleton = 1"))
        .scalar_one_or_none()
    )
    policy = RetentionPolicy.from_wire(stored) if stored is not None else DEFAULT_RETENTION_POLICY
    return cast(int, policy.record_compression_age.seconds)


def upgrade() -> None:
    age = _compression_age_seconds()
    for fact in _FACTS:
        op.execute(
            f"ALTER TABLE {fact} SET (timescaledb.enable_columnstore = true, "
            "timescaledb.segmentby = 'station_id')"
        )
        op.get_bind().execute(
            sa.text(f"CALL add_columnstore_policy('{fact}', after => make_interval(secs => :age))"),
            {"age": age},
        )

    # alter_job 保留原生作业的内部身份及其它配置；缺任何一张表则整笔配置写入失败。
    op.execute(
        """
        CREATE FUNCTION monitor_set_compression_age(age_seconds integer)
        RETURNS void LANGUAGE plpgsql
        SET search_path = pg_catalog, public
        AS $$
        DECLARE
            job record;
            found_count integer := 0;
        BEGIN
            IF age_seconds IS NULL OR age_seconds <= 0 THEN
                RAISE EXCEPTION 'compression age must be positive';
            END IF;
            FOR job IN
                SELECT job_id, config
                FROM timescaledb_information.jobs
                WHERE proc_name = 'policy_compression'
                  AND hypertable_schema = 'public'
                  AND hypertable_name IN (
                      'monitor_reported_decision',
                      'monitor_reported_health',
                      'monitor_observation'
                  )
            LOOP
                found_count := found_count + 1;
                IF (job.config ->> 'compress_after')::interval
                    IS DISTINCT FROM make_interval(secs => age_seconds)
                THEN
                    PERFORM alter_job(
                        job.job_id,
                        config => jsonb_set(
                            job.config, '{compress_after}',
                            to_jsonb(make_interval(secs => age_seconds)::text)
                        )
                    );
                END IF;
            END LOOP;
            IF found_count <> 3 THEN
                RAISE EXCEPTION 'expected 3 monitor native compression policies, found %',
                    found_count;
            END IF;
        END;
        $$;
        """
    )
    op.execute("REVOKE ALL ON FUNCTION monitor_set_compression_age(integer) FROM PUBLIC")


def downgrade() -> None:
    for fact in _FACTS:
        op.execute(f"CALL remove_columnstore_policy('{fact}')")
        # 降级前将已压缩 chunk 原生转换回行存储，保留事件身份与事实数据。
        chunks = (
            op.get_bind()
            .execute(
                sa.text(
                    "SELECT chunk_schema, chunk_name FROM timescaledb_information.chunks "
                    "WHERE hypertable_schema = 'public' AND hypertable_name = :name "
                    "AND is_compressed"
                ),
                {"name": fact},
            )
            .all()
        )
        for schema, chunk in chunks:
            op.get_bind().execute(
                sa.text("CALL convert_to_rowstore(CAST(:chunk AS regclass))"),
                {"chunk": f"{schema}.{chunk}"},
            )
        op.execute(f"ALTER TABLE {fact} SET (timescaledb.enable_columnstore = false)")
    op.execute("DROP FUNCTION monitor_set_compression_age(integer)")
