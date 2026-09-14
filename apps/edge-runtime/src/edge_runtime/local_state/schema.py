"""按顺序排列的 local state 迁移 schema。

One database per inference host, holding every station that host runs (§5.7). SQLite
because it is embedded, single-host and in the standard library — the inference host does
not get a second database service to keep alive.

**Migrations are append-only and run in order.** `PRAGMA user_version` records how far a
database has been taken, so a host that has been offline for two releases catches up by
running what it has not run yet. A landed migration is never edited: the next change adds
one. `apply_migrations` is that rule as a function and holds no knowledge of this schema;
`migrate` is this schema going through it. The center's Alembic history is separate and
unrelated — nothing here is shared with it, because this schema belongs to the edge and
outlives an unreachable center.

**本阶段交付配置确认表。** `local_config` 保存最后一个完整确认 bundle, 失败记录只用于诊断;
**连接器处置账本由后续阶段追加.**

Standard library only, like the core this state serves (edge-autonomy.md §5.11).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence

_V1 = (
    # An SOP instance, in flight while `closed_at` is NULL. The row carries the core's
    # `Instance` faithfully rather than a summary of it, so what comes back after a restart
    # is what the core had — a summary would be a second, quietly divergent model of the
    # same thing.
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
    # The instant a decision was reached is its evidence anchor (§5.6): the moment the
    # closing condition held, or the monotonic anchor of the observation that forced it.
    # There is therefore no separate `decided_at` — a second column holding the same fact
    # is a second chance to disagree with itself.
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
    # Latched, and therefore insert-only: this module exposes no update and no delete for
    # this table, which is how "a violation never disappears" (§5.2) holds structurally
    # rather than by every caller's good behaviour. The uniqueness is the same fact seen
    # from the other side — one deviation within one instance is one row, however many
    # times it is re-reported.
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
    # The transactional outbox for the center's mirror: written in the same transaction as
    # the decision it is about, so a decision the center never hears about is unreachable
    # rather than unlikely. `decision_id` is unique because one decision owes one report,
    # which is what makes a retry a retry instead of a second event.
    #
    # `sent_at` marks the row settled instead of deleting it, because the center's
    # idempotent upsert needs the local half of the event's identity to stay stable across
    # retries (#46), and because "what has this host already reported" is the question
    # retention answers when it trims reported data (§5.19). Trimming settled rows is
    # retention's, and only ever for rows that carry `sent_at`.
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
    # The clip we owe, one row per anchor. The CHECK is the structural half of "a failed
    # retry never drops the only copy we hold": a row cannot be recorded as uploaded
    # without naming where the remote copy is, so no failure path can reach the state that
    # would let the local file go.
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

_V3 = (
    "ALTER TABLE local_config RENAME TO local_config_v2",
    """
    CREATE TABLE local_config_v3 (
        slot             INTEGER PRIMARY KEY CHECK (slot = 1),
        host_id          TEXT    NOT NULL,
        config_revision  INTEGER NOT NULL,
        confirmed_at     REAL    NOT NULL,
        payload          TEXT    NOT NULL,
        CHECK (config_revision > 0)
    )
    """,
    """
    INSERT INTO local_config_v3 (slot, host_id, config_revision, confirmed_at, payload)
    SELECT slot, host_id, config_revision, confirmed_at, payload
      FROM local_config_v2
    """,
    "DROP TABLE local_config_v2",
    "ALTER TABLE local_config_v3 RENAME TO local_config",
)


MIGRATIONS: tuple[tuple[str, ...], ...] = (_V1, _V2, _V3)
"""Every migration in order. Index + 1 is the `user_version` it takes a database to."""


def apply_migrations(connection: sqlite3.Connection, migrations: Sequence[Sequence[str]]) -> int:
    """执行数据库尚未运行的迁移,并返回达到的版本。

    The mechanism, with no knowledge of the schema it usually carries: a migration is a
    sequence of statements, and `PRAGMA user_version` is how many have run. It is separate
    from `MIGRATIONS` so that the evolution path can be driven over a list a caller owns —
    the real list is one entry long, so running it can only ever show creation.

    Idempotent: a database already at `len(migrations)` runs nothing, which is what a restart
    does. Each migration is one transaction together with its version bump, so a failure
    leaves the database at the last version that completed — no half-created table, and a
    version that still asks for the migration that failed.
    """
    applied: int = connection.execute("PRAGMA user_version").fetchone()[0]
    for version, statements in enumerate(migrations[applied:], start=applied + 1):
        connection.execute("BEGIN IMMEDIATE")
        try:
            for statement in statements:
                connection.execute(statement)
            # PRAGMA takes no parameter binding, and `version` is this loop's own index
            # rather than anything a caller supplies.
            connection.execute(f"PRAGMA user_version = {version}")
        except Exception:
            connection.execute("ROLLBACK")
            raise
        connection.execute("COMMIT")
    return len(migrations)


def migrate(connection: sqlite3.Connection) -> int:
    """把一个数据库迁移到本模块当前 schema,并返回版本。"""
    return apply_migrations(connection, MIGRATIONS)
