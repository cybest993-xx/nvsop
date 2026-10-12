"""S108 离线验证准备包 CLI 的公开接缝回归。

测试在临时真实 SQLite（由权威迁移建表）与合成媒体上，通过子进程调用 CLI，
只断言可观察行为：只读采集、稳定身份/摘要比较、场景化失败非零、脱敏。
Center 接入用临时 fake psql 验证适配器（argv/管道/字段），不证明真实 SQL 成功。
"""

from __future__ import annotations

import json
import os
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest
from hashlib import sha256
from pathlib import Path

from edge_runtime.local_state.schema import migrate

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "validate_edge_restart.py"
EXPECTED_SCHEMA_VERSION = 19
CANDIDATE = "544e41a30c8c75bb1d8bd5fbf07f95b75d3ed34b"  # pragma: allowlist secret
HOST = "018f0000-0000-7000-8000-0000000000aa"
OTHER_HOST = "018f0000-0000-7000-8000-0000000000bb"
STATION = "018f0000-0000-7000-8000-000000000001"
INSTANCE_EVENT = f"{HOST}:{STATION}:instance:10"


def _run(*arguments: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    environment = dict(os.environ)
    if env:
        environment.update(env)
    return subprocess.run(
        [sys.executable, str(SCRIPT), *arguments],
        capture_output=True,
        text=True,
        env=environment,
        check=False,
    )


def _database(
    path: Path,
    *,
    open_instance: bool = True,
    interrupted: bool = False,
    config_revision: int = 3,
    result_kind: str | None = "written",
    attempts: int = 0,
) -> None:
    connection = sqlite3.connect(path)
    try:
        migrate(connection)
        closed_at = None if open_instance and not interrupted else 123.0
        lifecycle = None if closed_at is None else "indeterminate"
        connection.execute(
            "INSERT INTO local_sop_instance VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                STATION,
                10,
                100.0,
                110.0,
                "[]",
                0,
                "[]",
                "{}",
                closed_at,
                lifecycle,
                None,
                None,
                None,
            ),
        )
        if interrupted:
            connection.execute(
                "INSERT INTO local_decision VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    1,
                    STATION,
                    10,
                    "indeterminate",
                    '["RUN_INTERRUPTED"]',
                    "indeterminate",
                    123.0,
                    120.0,
                    123.0,
                ),
            )
        connection.execute(
            "INSERT INTO local_report_queue "
            "(queue_id, station_id, decision_id, instance_id, report_kind, attempts, sent_at, "
            " report_template_version_id, report_template_sha256, report_model_ids, "
            " configuration_revision, configuration_sha256, report_backend_provenance) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                5,
                STATION,
                None,
                10,
                "instance",
                0,
                None,
                "tv-1",
                "a" * 64,
                '["m1"]',
                3,
                "b" * 64,
                '[{"backend_id":"b1","model_ids":["m1"]}]',
            ),
        )
        connection.execute(
            "INSERT INTO local_evidence_queue "
            "(queue_id, station_id, instance_id, anchor, window_from, window_to) "
            "VALUES (?,?,?,?,?,?)",
            (7, STATION, 10, 1.0, 0.0, 2.0),
        )
        connection.execute(
            "INSERT INTO local_disposal "
            "(disposal_id, station_id, idempotency_key, action_kind, actor, result_kind, "
            " result_at, attempts) VALUES (?,?,?,?,?,?,?,?)",
            (9, STATION, "idem-1", "write_output", "supervisor", result_kind, 130.0, attempts),
        )
        connection.execute(
            "INSERT INTO local_config "
            "(slot, host_id, config_revision, sha256, confirmed_at, payload) "
            "VALUES (1,?,?,?,?,?)",
            (HOST, config_revision, "b" * 64, 100.0, "{}"),
        )
        connection.commit()
    finally:
        connection.close()


def _evidence(root: Path) -> None:
    clip = root / "evidence" / "e-1"
    clip.mkdir(parents=True, exist_ok=True)
    (clip / "clip.mp4").write_bytes(b"synthetic-video-bytes")


class ValidateEdgeRestartTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name)
        self.db = self.root / "edge.sqlite3"
        _database(self.db)
        self.evidence_dir = self.root / "evidence"
        self.evidence_dir.mkdir()
        _evidence(self.root)

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def _snapshot(
        self,
        output: Path,
        *,
        role: str = "before",
        extra: list[str] | None = None,
        db: Path | None = None,
        env: dict[str, str] | None = None,
        evidence: Path | None = None,
        center: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        arguments = list(extra or [])
        if center:
            arguments.append("--center")
        return _run(
            "snapshot",
            "--role",
            role,
            "--sqlite",
            str(db or self.db),
            "--evidence-dir",
            str(evidence or self.evidence_dir),
            "--host-id",
            HOST,
            "--candidate-sha",
            CANDIDATE,
            "--environment",
            "bench-a",
            "--output",
            str(output),
            *arguments,
            env=env,
        )

    def _fake_psql(self, *, instance_rows: str | None = None) -> tuple[dict[str, str], Path]:
        fake = self.root / "bin"
        fake.mkdir(exist_ok=True)
        recorded = self.root / "psql"
        recorded.mkdir(exist_ok=True)
        (recorded / "monitor_sop_instance.tsv").write_text(
            instance_rows
            if instance_rows is not None
            else f"{INSTANCE_EVENT}\t{STATION}\t10\t\t\t\t\t\n",
            encoding="utf-8",
        )
        for name in (
            "monitor_reported_decision",
            "monitor_observation",
            "monitor_violation",
            "monitor_disposal",
            "monitor_reported_health",
            "evidence_evidence",
        ):
            (recorded / f"{name}.tsv").write_text("", encoding="utf-8")
        script = fake / "psql"
        script.write_text(
            "#!/bin/sh\n"
            ': > "$FAKE_PSQL_DIR/argv.txt"\n'
            'for a in "$@"; do\n'
            '  printf "%s\\n" "$a" >> "$FAKE_PSQL_DIR/argv.txt"\n'
            "done\n"
            'echo "STDERR-LEAK-MARKER" >&2\n'
            "while IFS= read -r line; do\n"
            '  case "$line" in\n'
            '    *nvsop_table:*) q="${line##*nvsop_table:}"; echo "nvsop_table:${q}"; '
            'cat "$FAKE_PSQL_DIR/${q}.tsv";;\n'
            "  esac\n"
            "done\n"
            "exit 0\n",
            encoding="utf-8",
        )
        script.chmod(0o755)
        return {
            "PATH": f"{fake}{os.pathsep}{os.environ['PATH']}",
            "FAKE_PSQL_DIR": str(recorded),
            "PGHOST": "center-db",
            "PGDATABASE": "nvsop",
        }, recorded

    def test_snapshot_is_read_only_and_incomplete_without_center(self) -> None:
        output = self.root / "before.json"
        os.chmod(self.db, stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)

        result = self._snapshot(output)

        self.assertEqual(3, result.returncode, result.stderr)
        document = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(EXPECTED_SCHEMA_VERSION, document["sqlite"]["schema_version"])
        self.assertTrue(document["sqlite"]["compatible"])
        self.assertEqual(1, document["sqlite"]["table_counts"]["local_sop_instance"])
        self.assertEqual(1, document["sqlite"]["queue_depths"]["report_pending"])
        self.assertEqual(3, document["sqlite"]["configuration"]["config_revision"])
        self.assertEqual(
            {
                "sha256": sha256(b"synthetic-video-bytes").hexdigest(),
                "size": len(b"synthetic-video-bytes"),
            },
            document["evidence_files"][sha256(b"e-1/clip.mp4").hexdigest()],
        )
        self.assertEqual("not_attempted", document["center"]["status"])

    def test_snapshot_reads_rows_visible_only_in_an_active_wal(self) -> None:
        writer = sqlite3.connect(self.db)
        try:
            writer.execute("PRAGMA journal_mode=WAL")
            writer.execute(
                "INSERT INTO local_sop_instance VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("st-wal", 99, 1.0, 2.0, "[]", 0, "[]", "{}", None, None, None, None, None),
            )
            writer.commit()
            output = self.root / "wal.json"
            result = self._snapshot(output)
            document = json.loads(output.read_text(encoding="utf-8"))
        finally:
            writer.close()

        self.assertEqual(3, result.returncode, result.stderr)
        self.assertIn(
            "instance:st-wal:99", [item["key"] for item in document["sqlite"]["instances"]]
        )

    def test_snapshot_omits_payloads_paths_and_free_text(self) -> None:
        connection = sqlite3.connect(self.db)
        try:
            connection.execute(
                "UPDATE local_report_queue SET report_model_ids = ?", ('["sk-live-SECRET"]',)
            )
            connection.execute(
                "UPDATE local_config SET payload = ?",
                ('{"db_password":"pg-secret"}',),  # pragma: allowlist secret
            )
            connection.commit()
        finally:
            connection.close()
        output = self.root / "before.json"

        self._snapshot(output)
        rendered = output.read_text(encoding="utf-8")

        for forbidden in (
            "synthetic-video-bytes",
            str(self.root),
            "sk-live-SECRET",
            "pg-secret",
            "report_configuration",
            "last_error",
            "result_detail",
            "/run/secrets",
        ):
            self.assertNotIn(forbidden, rendered)

    def test_snapshot_refuses_to_overwrite_an_existing_result(self) -> None:
        output = self.root / "before.json"
        output.write_text("{}", encoding="utf-8")

        result = self._snapshot(output)

        self.assertEqual(2, result.returncode)
        self.assertEqual("{}", output.read_text(encoding="utf-8"))

    def test_snapshot_refuses_output_inside_the_evidence_tree(self) -> None:
        output = self.evidence_dir / "before.json"

        result = self._snapshot(output)

        self.assertEqual(2, result.returncode)
        self.assertFalse(output.exists())

    def test_snapshot_rejects_an_incompatible_schema_version(self) -> None:
        connection = sqlite3.connect(self.db)
        try:
            connection.execute("PRAGMA user_version = 5")
            connection.commit()
        finally:
            connection.close()
        output = self.root / "before.json"

        result = self._snapshot(output)

        self.assertEqual(2, result.returncode)
        self.assertFalse(json.loads(output.read_text(encoding="utf-8"))["sqlite"]["compatible"])

    def test_snapshot_rejects_a_database_from_another_host(self) -> None:
        # 本地配置 host_id 与显式 --host-id 不一致必须拒绝，避免采错库。
        result = self._snapshot(self.root / "before.json", extra=["--host-id", OTHER_HOST])

        self.assertEqual(2, result.returncode)
        self.assertFalse((self.root / "before.json").exists())

    def test_snapshot_collects_center_through_psql_without_dsn_or_stderr(self) -> None:
        env, recorded = self._fake_psql()
        output = self.root / "before.json"

        result = self._snapshot(output, center=True, env=env)

        self.assertEqual(0, result.returncode, result.stderr)
        document = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual("collected", document["center"]["status"])
        center = document["center"]["tables"]["monitor_sop_instance"]
        self.assertEqual(1, center["count"])
        self.assertIn(INSTANCE_EVENT, center["objects"])
        argv = (recorded / "argv.txt").read_text(encoding="utf-8")
        self.assertNotIn("postgresql://", argv)
        self.assertNotIn("password", argv.lower())
        self.assertNotIn("STDERR-LEAK-MARKER", result.stdout + result.stderr)

    def _full_compare(
        self,
        scenario: str,
        *,
        after_db: Path | None = None,
        after_evidence: Path | None = None,
        mutate: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        env, _ = self._fake_psql()
        before = self.root / "before.json"
        after = self.root / "after.json"
        self._snapshot(before, role="before", center=True, env=env)
        self._snapshot(
            after,
            role="after",
            center=True,
            env=env,
            db=after_db or self._after_database(interrupted=True),
            evidence=after_evidence or self._after_evidence(),
        )
        if mutate is not None:
            document = json.loads(after.read_text(encoding="utf-8"))
            if mutate == "center_provenance":
                objects = document["center"]["tables"]["monitor_sop_instance"]["objects"]
                next(iter(objects.values()))["provenance"] = "deadbeef"
            elif mutate == "provenance":
                document["sqlite"]["provenance"][0]["report_backend_id"] = "other-backend"
            elif mutate == "attempts":
                document["sqlite"]["disposals"][0]["attempts"] = 7
            after.write_text(json.dumps(document), encoding="utf-8")
        return _run(
            "compare", "--scenario", scenario, "--before", str(before), "--after", str(after)
        )

    def test_restart_passes_when_instance_closes_interrupted_and_config_is_stable(self) -> None:
        result = self._full_compare("restart")

        self.assertEqual(0, result.returncode, result.stderr)
        document = json.loads(result.stdout)
        statuses = {check["id"]: check["status"] for check in document["checks"]}
        self.assertEqual("restart", document["scenario"])
        self.assertEqual("pass", statuses["interrupted_closed"])
        self.assertEqual("pass", statuses["configuration_effective"])
        self.assertEqual("pass", statuses["center_mirror"])
        self.assertEqual("not_asserted", document["field_acceptance"])
        self.assertIn("physical_execution_count", document["needs_field"])

    def test_restart_accepts_two_legitimate_instance_events(self) -> None:
        env, recorded = self._fake_psql()
        before = self.root / "before.json"
        after = self.root / "after.json"
        open_event = f"{INSTANCE_EVENT}\t{STATION}\t10\t\t\t\t\t\n"
        self._snapshot(before, role="before", center=True, env=env)
        # 同一实例的开、闭两条独立 Center 事实事件都必须被接受，不当作重复。
        (recorded / "monitor_sop_instance.tsv").write_text(
            open_event + f"{INSTANCE_EVENT}:close\t{STATION}\t10\t\t\t\t\t\n", encoding="utf-8"
        )
        self._snapshot(
            after,
            role="after",
            center=True,
            env=env,
            db=self._after_database(interrupted=True),
            evidence=self._after_evidence(),
        )

        result = _run(
            "compare", "--scenario", "restart", "--before", str(before), "--after", str(after)
        )

        self.assertEqual(0, result.returncode, result.stderr)

    def test_restart_rejects_changed_center_provenance(self) -> None:
        result = self._full_compare("restart", mutate="center_provenance")

        self.assertEqual(1, result.returncode)
        self.assertIn("center_mirror", result.stdout)

    def test_restart_rejects_changed_terminal_disposal_attempts(self) -> None:
        result = self._full_compare("restart", mutate="attempts")

        self.assertEqual(1, result.returncode)
        self.assertIn("terminal_disposals_stable", result.stdout)

    def test_rebind_accepts_a_confirmed_configuration_change(self) -> None:
        result = self._full_compare(
            "rebind", after_db=self._after_database(interrupted=False, config_revision=4)
        )

        self.assertEqual(0, result.returncode, result.stderr)
        document = json.loads(result.stdout)
        statuses = {check["id"]: check["status"] for check in document["checks"]}
        self.assertNotIn("configuration_effective", statuses)
        self.assertNotIn("interrupted_closed", statuses)

    def test_rebind_rejects_a_replaced_backlog_provenance(self) -> None:
        result = self._full_compare("rebind", mutate="provenance")

        self.assertEqual(1, result.returncode)
        self.assertIn("provenance_locked", result.stdout)

    def test_replay_passes_on_a_fixed_backlog_confirmation(self) -> None:
        result = self._full_compare("replay")

        self.assertEqual(0, result.returncode, result.stderr)
        document = json.loads(result.stdout)
        statuses = {check["id"]: check["status"] for check in document["checks"]}
        self.assertEqual("pass", statuses["center_mirror"])
        self.assertNotIn("interrupted_closed", statuses)

    def test_replay_rejects_a_new_mirror_identity(self) -> None:
        env, recorded = self._fake_psql()
        before = self.root / "before.json"
        after = self.root / "after.json"
        self._snapshot(before, role="before", center=True, env=env)
        (recorded / "evidence_evidence.tsv").write_text(
            f"ev-new\t{STATION}\t10\t{'c' * 64}\tgen-1\t1.0\t0.0\t2.0\n", encoding="utf-8"
        )
        self._snapshot(
            after,
            role="after",
            center=True,
            env=env,
            db=self._after_database(interrupted=True),
            evidence=self._after_evidence(),
        )

        result = _run(
            "compare", "--scenario", "replay", "--before", str(before), "--after", str(after)
        )

        self.assertEqual(1, result.returncode)
        self.assertIn("center_mirror", result.stdout)

    def test_compare_rejects_snapshots_from_different_hosts(self) -> None:
        before = self.root / "before.json"
        after = self.root / "after.json"
        self._snapshot(before, role="before")
        self._snapshot(
            after,
            role="after",
            db=self._after_database(interrupted=True),
            evidence=self._after_evidence(),
        )
        document = json.loads(after.read_text(encoding="utf-8"))
        document["environment"]["host_id"] = OTHER_HOST
        after.write_text(json.dumps(document), encoding="utf-8")

        result = _run(
            "compare", "--scenario", "restart", "--before", str(before), "--after", str(after)
        )

        self.assertEqual(2, result.returncode)

    def test_compare_rejects_missing_media(self) -> None:
        env, _ = self._fake_psql()
        before = self.root / "before.json"
        after_evidence = self._after_evidence()
        (after_evidence / "e-1" / "clip.mp4").unlink()
        after = self.root / "after.json"
        self._snapshot(before, role="before", center=True, env=env)
        self._snapshot(
            after,
            role="after",
            center=True,
            env=env,
            db=self._after_database(interrupted=True),
            evidence=after_evidence,
        )

        result = _run(
            "compare", "--scenario", "rebind", "--before", str(before), "--after", str(after)
        )

        self.assertEqual(1, result.returncode)
        self.assertIn("media_intact", result.stdout)

    def test_replay_rejects_new_local_disposal_and_evidence_identities(self) -> None:
        env, _ = self._fake_psql()
        before = self.root / "before.json"
        after = self.root / "after.json"
        after_db = self._after_database(interrupted=True)
        connection = sqlite3.connect(after_db)
        try:
            # 暂停新输入的固定确认阶段若又新建本机处置/证据待办，必须被判为不通过。
            connection.execute(
                "INSERT INTO local_disposal "
                "(disposal_id, station_id, idempotency_key, action_kind, actor, result_kind, "
                " result_at, attempts) VALUES (?,?,?,?,?,?,?,?)",
                (10, STATION, "idem-2", "write_output", "supervisor", None, None, 0),
            )
            connection.execute(
                "INSERT INTO local_evidence_queue "
                "(queue_id, station_id, instance_id, anchor, window_from, window_to) "
                "VALUES (?,?,?,?,?,?)",
                (8, STATION, 10, 5.0, 4.0, 6.0),
            )
            connection.commit()
        finally:
            connection.close()
        self._snapshot(before, role="before", center=True, env=env)
        self._snapshot(
            after, role="after", center=True, env=env, db=after_db, evidence=self._after_evidence()
        )

        result = _run(
            "compare", "--scenario", "replay", "--before", str(before), "--after", str(after)
        )

        self.assertEqual(1, result.returncode, result.stdout + result.stderr)
        # 两项新增身份都必须被识别（而非只命中其一）。
        self.assertIn(f"disposal:{STATION}:idem-2", result.stdout)
        self.assertIn(f"evidence:{STATION}:10:5.0", result.stdout)

    def _after_database(self, *, interrupted: bool, config_revision: int = 3) -> Path:
        closed = self.root / f"after-{interrupted}-{config_revision}.sqlite3"
        _database(
            closed, open_instance=False, interrupted=interrupted, config_revision=config_revision
        )
        return closed

    def _after_evidence(self) -> Path:
        directory = self.root / "evidence-after"
        directory.mkdir(exist_ok=True)
        clip = directory / "e-1"
        clip.mkdir(parents=True, exist_ok=True)
        (clip / "clip.mp4").write_bytes(b"synthetic-video-bytes")
        return directory


if __name__ == "__main__":
    unittest.main()
