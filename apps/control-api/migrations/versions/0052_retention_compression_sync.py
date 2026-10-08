"""retention：策略写入时在同一事务中更新 monitor 原生压缩年龄。

触发器仅挂载在 retention 拥有的策略表；更新压缩作业的函数归 monitor 拥有。
"""

from __future__ import annotations

from alembic import op

revision: str = "0052"
down_revision: str | None = "0051"

RAW_SQL_TABLES = frozenset({"retention_policy"})


def upgrade() -> None:
    # runtime role 没有管理 Timescale 作业的权限，definer 的唯一动作是转交受控策略年龄。
    op.execute(
        """
        CREATE FUNCTION retention_sync_monitor_compression_age()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        BEGIN
            PERFORM public.monitor_set_compression_age(
                (NEW.policy -> 'record_compression_age' ->> 'seconds')::integer
            );
            RETURN NEW;
        END;
        $$;
        """
    )
    op.execute("REVOKE ALL ON FUNCTION retention_sync_monitor_compression_age() FROM PUBLIC")
    op.execute(
        """
        CREATE TRIGGER retention_sync_monitor_compression_age
        AFTER INSERT OR UPDATE OF policy ON retention_policy
        FOR EACH ROW EXECUTE FUNCTION retention_sync_monitor_compression_age();
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER retention_sync_monitor_compression_age ON retention_policy")
    op.execute("DROP FUNCTION retention_sync_monitor_compression_age()")
