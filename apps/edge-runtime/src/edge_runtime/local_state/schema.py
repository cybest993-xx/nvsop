"""按顺序排列的 local state 迁移 schema。

每台推理机使用一个数据库, 保存该主机运行的全部工位(§5.7)。SQLite 嵌入式、单机且
属于标准库, 推理机不需要额外维护数据库服务。

**迁移只追加并按顺序执行。** `PRAGMA user_version` 记录数据库已经完成的版本; 主机离线
多个版本后, 会继续执行尚未运行的迁移。已经落地的迁移不得修改, 后续变更必须追加新迁移。
`apply_migrations` 只实现这条机制, 不了解具体 schema; `migrate` 负责把本 schema 交给它执行。
中心的 Alembic 历史与本地 schema 相互独立, 因为这里的状态属于 edge, 并且必须在中心不可达时
继续存活。

**本模块只负责本票拥有的表, 其他表由各自写入模块负责。** 下方表覆盖实例、判定、锁存违规和
两个队列。`local_config` 与 `local_template_version` 仍由配置落地代码拥有。连接器写入和
supervisor 处置共用一个持久去重账本, 因此 `local_disposal` 在此创建; 连接器适配器不得再建
第二套写入账本。

只使用标准库, 与本状态服务的判定核心保持一致(edge-autonomy.md §5.11)。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence

_V1 = (
    # `closed_at` 为 NULL 时表示正在运行的 SOP 实例。该行完整保存核心的 `Instance`,
    # 而不是保存摘要; 重启后恢复的内容必须与核心当时的状态一致, 避免产生悄然分叉的第二个模型。
    """
    CREATE TABLE local_sop_instance (
        station_id           TEXT    NOT NULL,
        instance_id          INTEGER NOT NULL,
        opened_at            REAL    NOT NULL,
        last_observation_at  REAL    NOT NULL,
        seen                 TEXT    NOT NULL,
        expected_index       INTEGER NOT NULL,
        impairments          TEXT    NOT NULL,
        settled              TEXT    NOT NULL,
        closed_at            REAL,
        lifecycle            TEXT,
        PRIMARY KEY (station_id, instance_id),
        CHECK ((closed_at IS NULL) = (lifecycle IS NULL))
    )
    """,
    # 判定产生的时刻就是证据锚点(§5.6): 要么是闭合条件成立的时刻, 要么是迫使判定产生的
    # 观测单调锚点。因此不另设 `decided_at`, 避免同一事实由两列分别保存而产生不一致。
    """
    CREATE TABLE local_decision (
        decision_id      INTEGER PRIMARY KEY,
        station_id       TEXT    NOT NULL,
        instance_id      INTEGER NOT NULL,
        verdict          TEXT    NOT NULL,
        reasons          TEXT    NOT NULL,
        lifecycle        TEXT    NOT NULL,
        evidence_anchor  REAL    NOT NULL,
        evidence_from    REAL    NOT NULL,
        evidence_to      REAL    NOT NULL,
        FOREIGN KEY (station_id, instance_id)
            REFERENCES local_sop_instance (station_id, instance_id)
    )
    """,
    """
    CREATE INDEX local_decision_by_instance
        ON local_decision (station_id, instance_id)
    """,
    # 违规一旦锁存就只允许插入; 本模块不提供该表的更新和删除, 因此“违规不会消失”(§5.2)
    # 由结构保证, 而不是依赖每个调用方自觉遵守。唯一性表达同一事实的另一面: 一个实例中的
    # 一次偏差只有一行, 无论它被重复上报多少次。
    """
    CREATE TABLE local_violation (
        violation_id     INTEGER PRIMARY KEY,
        station_id       TEXT    NOT NULL,
        instance_id      INTEGER NOT NULL,
        decision_id      INTEGER NOT NULL,
        reason           TEXT    NOT NULL,
        steps            TEXT    NOT NULL,
        evidence_anchor  REAL    NOT NULL,
        evidence_from    REAL    NOT NULL,
        evidence_to      REAL    NOT NULL,
        UNIQUE (station_id, instance_id, reason, steps),
        FOREIGN KEY (decision_id) REFERENCES local_decision (decision_id),
        FOREIGN KEY (station_id, instance_id)
            REFERENCES local_sop_instance (station_id, instance_id)
    )
    """,
    # 中心镜像的事务性 outbox: 它与所属判定写入同一事务, 因此中心未收到的判定是不可达状态,
    # 而不是偶然丢失。`decision_id` 唯一, 因为一个判定只对应一条上报; 重试因此仍是重试,
    # 不会变成第二个事件。
    #
    # 使用 `sent_at` 标记已结算行而不是删除它, 因为中心幂等 upsert 需要本地事件身份在重试中
    # 保持稳定(#46), 保留策略也需要知道主机已经上报过什么(§5.19)。只有带有 `sent_at`
    # 的已结算行才允许由保留策略裁剪。
    """
    CREATE TABLE local_report_queue (
        queue_id         INTEGER PRIMARY KEY,
        station_id       TEXT    NOT NULL,
        decision_id      INTEGER NOT NULL UNIQUE,
        attempts         INTEGER NOT NULL DEFAULT 0,
        last_attempt_at  REAL,
        last_error       TEXT,
        sent_at          REAL,
        FOREIGN KEY (decision_id) REFERENCES local_decision (decision_id)
    )
    """,
    # 每个锚点对应一条待交付证据片段。CHECK 约束保证“失败重试不会丢掉唯一副本”: 没有远端
    # 引用就不能记录为已上传, 因此任何失败路径都不能把本地唯一文件推进到可删除状态。
    """
    CREATE TABLE local_evidence_queue (
        queue_id          INTEGER PRIMARY KEY,
        station_id        TEXT    NOT NULL,
        instance_id       INTEGER NOT NULL,
        anchor            REAL    NOT NULL,
        window_from       REAL    NOT NULL,
        window_to         REAL    NOT NULL,
        attempts          INTEGER NOT NULL DEFAULT 0,
        last_attempt_at   REAL,
        last_error        TEXT,
        uploaded_at       REAL,
        remote_reference  TEXT,
        UNIQUE (station_id, instance_id, anchor),
        CHECK ((uploaded_at IS NULL) = (remote_reference IS NULL)),
        FOREIGN KEY (station_id, instance_id)
            REFERENCES local_sop_instance (station_id, instance_id)
    )
    """,
)

_V2 = (
    # 每个工位和幂等键各有一行, 这是唯一的持久写入账本。结果产生后仍保留该行, 避免重启把
    # 已确认的输出变成第二个物理意图。租约只是调用方恢复遗弃尝试所需的元数据, 不会删除
    # 已完成写入的身份或结果。
    """
    CREATE TABLE local_disposal (
        station_id       TEXT    NOT NULL,
        idempotency_key  TEXT    NOT NULL,
        connector_id     TEXT    NOT NULL,
        point_id         TEXT    NOT NULL,
        actor            TEXT    NOT NULL,
        requested_state  TEXT    NOT NULL,
        result_kind      TEXT,
        result_detail    TEXT,
        result_at        REAL,
        attempts         INTEGER NOT NULL DEFAULT 0,
        last_attempt_at  REAL,
        lease_until      REAL,
        PRIMARY KEY (station_id, idempotency_key),
        CHECK (attempts >= 0),
        CHECK ((result_kind IS NULL) = (result_at IS NULL))
    )
    """,
    """
    CREATE INDEX local_disposal_by_connector
        ON local_disposal (station_id, connector_id, point_id)
    """,
)

_V3 = (
    """
    CREATE TABLE local_config (
        slot             INTEGER PRIMARY KEY CHECK (slot = 1),
        host_id          TEXT    NOT NULL,
        config_revision  INTEGER NOT NULL,
        sha256           TEXT    NOT NULL,
        confirmed_at     REAL    NOT NULL,
        payload          TEXT    NOT NULL,
        CHECK (config_revision > 0),
        CHECK (length(sha256) = 64)
    )
    """,
    """
    CREATE TABLE local_config_failure (
        slot          INTEGER PRIMARY KEY CHECK (slot = 1),
        code          TEXT    NOT NULL,
        detail        TEXT    NOT NULL,
        observed_at   REAL    NOT NULL
    )
    """,
)

MIGRATIONS: tuple[tuple[str, ...], ...] = (_V1, _V2, _V3)
"""按顺序排列的全部迁移; 索引加一就是迁移后数据库的 `user_version`。"""


def apply_migrations(connection: sqlite3.Connection, migrations: Sequence[Sequence[str]]) -> int:
    """执行数据库尚未运行的迁移, 并返回达到的版本。

    迁移由 SQL 语句序列组成, `PRAGMA user_version` 记录已经执行的数量。机制与 `MIGRATIONS`
    分离, 因此调用方可以提供自己的迁移列表; 真实列表目前只有一个迁移集合, 执行结果只会
    表示 schema 创建或升级。

    该过程幂等: 版本已经达到 `len(migrations)` 的数据库不会重复执行。每个迁移和版本递增
    在同一事务中完成; 失败时保留最后一个已完成版本, 不会留下半建表状态, 并会继续要求重试
    失败的迁移。
    """
    applied: int = connection.execute("PRAGMA user_version").fetchone()[0]
    for version, statements in enumerate(migrations[applied:], start=applied + 1):
        connection.execute("BEGIN IMMEDIATE")
        try:
            for statement in statements:
                connection.execute(statement)
            # PRAGMA 不支持参数绑定; `version` 是本循环自己的索引, 不来自调用方输入。
            connection.execute(f"PRAGMA user_version = {version}")
        except Exception:
            connection.execute("ROLLBACK")
            raise
        connection.execute("COMMIT")
    return len(migrations)


def migrate(connection: sqlite3.Connection) -> int:
    """把一个数据库迁移到本模块当前 schema,并返回版本。"""
    return apply_migrations(connection, MIGRATIONS)
