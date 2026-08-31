"""The local state schema, as an ordered list of migrations.

One database per inference host, holding every station that host runs (§5.7). SQLite
because it is embedded, single-host and in the standard library — the inference host does
not get a second database service to keep alive.

**Migrations are append-only and run in order.** `PRAGMA user_version` records how far a
database has been taken, so a host that has been offline for two releases catches up by
running what it has not run yet. A landed migration is never edited: the next change adds
one. The center's Alembic history is separate and unrelated — nothing here is shared with
it, because this schema belongs to the edge and outlives an unreachable center.

**What this ticket creates and what it deliberately does not.** The five tables below are
the ones whose behaviour E5.2 owns: instances, decisions, latched violations, and the two
queues. `local_config`, `local_template_version` and `local_disposal` are named in §七 and
are *not* created here — their shapes belong to the tickets that write them (template
landing, and #50/#51 for disposal), and freezing a table now for a writer that does not
exist yet would fix a shape by guesswork rather than by use. Adding them is one migration
each.

Standard library only, like the core this state serves (edge-autonomy.md §5.11).
"""

from __future__ import annotations

import sqlite3

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

MIGRATIONS: tuple[tuple[str, ...], ...] = (_V1,)
"""Every migration in order. Index + 1 is the `user_version` it takes a database to."""


def migrate(connection: sqlite3.Connection) -> int:
    """Bring one database up to the latest version, returning that version.

    Idempotent: an already-current database runs nothing. Each migration is one
    transaction together with the version bump, so an interrupted upgrade leaves the
    database at the last version that completed rather than half-way into the next.
    """
    applied: int = connection.execute("PRAGMA user_version").fetchone()[0]
    for version, statements in enumerate(MIGRATIONS[applied:], start=applied + 1):
        connection.execute("BEGIN IMMEDIATE")
        try:
            for statement in statements:
                connection.execute(statement)
            # PRAGMA takes no parameter binding, and `version` is this module's own loop
            # index rather than anything a caller supplies.
            connection.execute(f"PRAGMA user_version = {version}")
        except Exception:
            connection.execute("ROLLBACK")
            raise
        connection.execute("COMMIT")
    return len(MIGRATIONS)
