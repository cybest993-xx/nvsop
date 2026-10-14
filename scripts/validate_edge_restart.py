#!/usr/bin/env python3
"""Edge 重启/恢复对账演练的离线只读证据采集与比较。

两个子命令：

- ``snapshot``：对显式给出的本机 SQLite 与证据目录做一次**只读**采集，可选经
  系统 ``psql``/libpq 外部 secret 环境读取真实 Center PostgreSQL，导出前后可比较的逻辑
  摘要、队列深度、冻结来源、活动实例/判定/处置结果、最后确认配置、证据文件 sha256，以及
  Center 对象计数/身份。中心采集缺失时结果标记为不完整并以非零退出，但保留已采集部分。
- ``compare``：只依据两份快照中的原始事实，检查重启后仍能从数据证明的条件（旧待办/终态
  身份保留、来源未被新归属替换、活动实例以 RUN_INTERRUPTED 结案、原媒体仍在且摘要不变、
  已终态处置结果与尝试次数不变、最后确认配置仍有效、镜像对象未被丢弃或重复）。无法从离线
  数据证明的部分明确列为现场项，绝不把人工布尔或本工具的输出当作 AC 通过。

这是运维证据准备与数据核对工具，不重实现判定逻辑，也不写入任何 SQLite 或 Center 数据。
标准库即可离线运行；现场执行不依赖代理访问或在线下载。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

EXPECTED_SCHEMA_VERSION = 20
SNAPSHOT_SCHEMA = 1
TOOL_VERSION = 1
CANDIDATE_SHA = re.compile(r"[0-9a-f]{40}")
# 只有明文令牌（标识/枚举/十进制数/十六进制摘要）可原样进入证据包；其余字符串一律只留摘要，
# 不假设任何字符串天然无秘密。JSON 列按“冻结来源摘要”处理，不做自由文本透传。
SAFE_TOKEN = re.compile(r"[A-Za-z0-9._:-]{1,200}")
ENUM_TOKEN = re.compile(r"[A-Za-z0-9_]{1,64}")
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_DIR = Path(".nvsop/artifacts/edge-restart-validation")
# 每个 (表, 列...) 是允许进入证据包的逻辑摘要来源。禁止读取 local_config.payload /
# report_configuration / last_error / result_detail / sources / media_results /
# terminal_error_detail 等原始载荷或自由文本列。
SQLITE_TABLES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "local_sop_instance",
        ("station_id", "instance_id", "opened_at", "last_observation_at", "closed_at", "lifecycle"),
    ),
    (
        "local_decision",
        (
            "decision_id",
            "station_id",
            "instance_id",
            "verdict",
            "reasons",
            "lifecycle",
            "evidence_anchor",
        ),
    ),
    ("local_violation", ("violation_id", "station_id", "instance_id", "decision_id", "reason")),
    (
        "local_report_queue",
        (
            "queue_id",
            "station_id",
            "decision_id",
            "instance_id",
            "report_kind",
            "sent_at",
            "superseded_at",
            "report_host_id",
            "report_backend_id",
            "report_template_version_id",
            "report_template_sha256",
            "report_model_ids",
            "configuration_revision",
            "configuration_sha256",
            "report_backend_provenance",
        ),
    ),
    (
        "local_observation_queue",
        (
            "queue_id",
            "station_id",
            "instance_id",
            "source",
            "observed_at",
            "sent_at",
            "report_host_id",
            "report_template_version_id",
            "report_template_sha256",
            "report_backend_provenance",
        ),
    ),
    (
        "local_health_queue",
        ("queue_id", "station_id", "stream_id", "status", "reason_code", "occurred_at", "sent_at"),
    ),
    (
        "local_evidence_queue",
        (
            "queue_id",
            "station_id",
            "instance_id",
            "anchor",
            "window_from",
            "window_to",
            "uploaded_at",
            "registered_at",
        ),
    ),
    (
        "local_disposal",
        (
            "disposal_id",
            "station_id",
            "idempotency_key",
            "action_kind",
            "result_kind",
            "result_at",
            "attempts",
            "sent_at",
        ),
    ),
    ("local_execution_lease", ("station_id", "grant_id", "holder_host_id", "lease_expires_at")),
    ("local_config", ("host_id", "config_revision", "sha256", "confirmed_at")),
    ("local_config_failure", ("code", "observed_at")),
    ("local_station_judgment_control", ("station_id", "paused")),
)
# 按列名决定脱敏方式：枚举列表只保留受控令牌，其余 JSON/路径列只保留摘要。
ENUM_LIST_COLUMNS = frozenset({"reasons"})
JSON_DIGEST_COLUMNS = frozenset({"report_model_ids", "report_backend_provenance"})
TEXT_DIGEST_COLUMNS = frozenset({"stream_id"})
# Center 只读白名单；(键, SQL, 列名序列)。经 psql 变量 :'nvsop_host' 过滤来源主机，
# 不把主机写死进 SQL。每张表只取不可变字段（身份 + 产生时来源/结果证明），payload 只取具名
# 契约字段并整体 hash，不整段透传自由文本；状态/闭合等会合法变化的列不进白名单。
CENTER_QUERIES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (
        "monitor_sop_instance",
        "SELECT event_id, station_id, instance_id, "
        "COALESCE(payload->>'template_version_id', ''), "
        "COALESCE(payload->>'template_sha256', ''), COALESCE(payload->>'backend_provenance', ''), "
        "COALESCE(payload->>'configuration_revision', ''), "
        "COALESCE(payload->>'configuration_sha256', '') FROM monitor_sop_instance "
        "WHERE host_id = :'nvsop_host' ORDER BY event_id",
        (
            "station_id",
            "instance_id",
            "template_version_id",
            "template_sha256",
            "backend_provenance",
            "configuration_revision",
            "configuration_sha256",
        ),
    ),
    (
        "monitor_reported_decision",
        "SELECT event_id, station_id, COALESCE(backend_id, ''), "
        "COALESCE(payload->>'model_ids', ''), COALESCE(payload->>'template_version_id', ''), "
        "COALESCE(payload->>'template_sha256', ''), COALESCE(payload->>'backend_provenance', ''), "
        "COALESCE(payload->>'configuration_revision', ''), "
        "COALESCE(payload->>'configuration_sha256', '') FROM monitor_reported_decision "
        "WHERE host_id = :'nvsop_host' ORDER BY event_id",
        (
            "station_id",
            "backend_id",
            "model_ids",
            "template_version_id",
            "template_sha256",
            "backend_provenance",
            "configuration_revision",
            "configuration_sha256",
        ),
    ),
    (
        "monitor_observation",
        "SELECT event_id, station_id, instance_id::text, payload->>'source', "
        "COALESCE(payload->>'signal', ''), COALESCE(payload->>'template_version_id', ''), "
        "COALESCE(payload->>'template_sha256', ''), COALESCE(payload->>'backend', '') "
        "FROM monitor_observation WHERE host_id = :'nvsop_host' ORDER BY event_id",
        (
            "station_id",
            "instance_id",
            "source",
            "signal",
            "template_version_id",
            "template_sha256",
            "backend",
        ),
    ),
    (
        "monitor_violation",
        "SELECT event_id, station_id, instance_id::text, decision_event_id, reason_code "
        "FROM monitor_violation WHERE host_id = :'nvsop_host' ORDER BY event_id",
        ("station_id", "instance_id", "decision_event_id", "reason_code"),
    ),
    (
        "monitor_disposal",
        "SELECT event_id, COALESCE(payload->>'station_id', ''), "
        "COALESCE(payload->>'instance_id', ''), COALESCE(payload->>'idempotency_key', ''), "
        "COALESCE(payload->>'action_kind', ''), COALESCE(payload->>'result_kind', ''), "
        "COALESCE(payload->>'result_at', ''), COALESCE(payload->>'attempts', '') "
        "FROM monitor_disposal WHERE host_id = :'nvsop_host' ORDER BY event_id",
        (
            "station_id",
            "instance_id",
            "idempotency_key",
            "action_kind",
            "result_kind",
            "result_at",
            "attempts",
        ),
    ),
    (
        "monitor_reported_health",
        "SELECT event_id, station_id, COALESCE(stream_id, ''), COALESCE(payload->>'status', ''), "
        "COALESCE(payload->>'reason_code', ''), COALESCE(payload->>'occurred_at', ''), "
        "COALESCE(payload->>'source_anchor', '') FROM monitor_reported_health "
        "WHERE host_id = :'nvsop_host' ORDER BY event_id",
        ("station_id", "stream_id", "status", "reason_code", "occurred_at", "source_anchor"),
    ),
    (
        "evidence_evidence",
        "SELECT evidence_id, station_id, instance_id::text, COALESCE(sha256, ''), generation, "
        "anchor::text, window_start::text, window_end::text FROM evidence_evidence "
        "WHERE host_id = :'nvsop_host' ORDER BY evidence_id",
        (
            "station_id",
            "instance_id",
            "sha256",
            "generation",
            "anchor",
            "window_start",
            "window_end",
        ),
    ),
)
SCENARIOS = ("restart", "replay", "rebind")
NEEDS_FIELD = ("physical_execution_count", "protocol_rejection", "real_host_reboot")


class UsageError(Exception):
    """调用方输入或环境不满足时的明确拒绝。"""


class CenterUnavailableError(Exception):
    """Center 采集不可用；不携带 stderr 或 secret。"""


def _utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _fingerprint(values: Sequence[str]) -> str:
    return _sha256_bytes("\x1f".join(values).encode("utf-8"))


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _digest_text(value: object) -> object:
    if value is None:
        return None
    return {"sha256": _sha256_bytes(str(value).encode("utf-8"))}


def _safe_token(value: object) -> object:
    """标识/枚举/摘要原样保留；其余字符串只留摘要，避免自由文本或秘密进入证据包。"""
    if not isinstance(value, str):
        return value
    return value if SAFE_TOKEN.fullmatch(value) else _digest_text(value)


def _enum_list(value: object) -> object:
    if not isinstance(value, str):
        return value
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return _digest_text(value)
    if isinstance(parsed, list) and all(
        isinstance(item, str) and ENUM_TOKEN.fullmatch(item) for item in parsed
    ):
        return parsed
    return _digest_text(value)


def _sanitize_row(columns: tuple[str, ...], row: Sequence[Any]) -> dict[str, Any]:
    sanitized: dict[str, Any] = {}
    for column, value in zip(columns, row, strict=True):
        if column in ENUM_LIST_COLUMNS:
            sanitized[column] = _enum_list(value)
        elif column in JSON_DIGEST_COLUMNS or column in TEXT_DIGEST_COLUMNS:
            sanitized[column] = _digest_text(value)
        else:
            sanitized[column] = _safe_token(value)
    return sanitized


def _read_sqlite(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise UsageError("sqlite database not found")
    connection = sqlite3.connect(f"file:{quote(str(path.resolve()), safe='/')}?mode=ro", uri=True)
    try:
        # 单一读事务，配合 mode=ro；不设置 immutable=1，因此活动 WAL 中的行同样可见。
        connection.execute("BEGIN")
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        rows_by_table: dict[str, list[dict[str, Any]]] = {}
        if version == EXPECTED_SCHEMA_VERSION:
            for table, columns in SQLITE_TABLES:
                cursor = connection.execute(
                    f"SELECT {', '.join(columns)} FROM {table} ORDER BY rowid"
                )
                rows_by_table[table] = [_sanitize_row(columns, row) for row in cursor]
        connection.execute("ROLLBACK")
    finally:
        connection.close()
    summary = _sqlite_summary(version, rows_by_table)
    summary["compatible"] = version == EXPECTED_SCHEMA_VERSION
    return summary


def _sqlite_summary(version: int, rows: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    configuration = rows.get("local_config", [])
    summary: dict[str, Any] = {
        "schema_version": version,
        "expected_schema_version": EXPECTED_SCHEMA_VERSION,
        "table_counts": {table: len(items) for table, items in rows.items()},
        "queue_depths": {},
        "queue_identities": {},
        "instances": [],
        "open_instances": [],
        "decisions": [],
        "provenance": [],
        "disposals": [],
        "configuration": configuration[0] if configuration else None,
    }
    _queue_summary(rows, summary)
    _instance_summary(rows, summary)
    _provenance_summary(rows, summary)
    summary["logical_digest"] = _logical_digest(version, rows)
    return summary


def _queue_summary(rows: dict[str, list[dict[str, Any]]], summary: dict[str, Any]) -> None:
    identities: dict[str, list[dict[str, str]]] = {}

    def record(queue: str, key: str, pending: bool) -> None:
        identities.setdefault(queue, []).append(
            {"key": key, "state": "pending" if pending else "settled"}
        )

    report = rows.get("local_report_queue", [])
    for row in report:
        settled = row["sent_at"] is not None or row["superseded_at"] is not None
        record("report", f"report:{row['queue_id']}", not settled)
    summary["queue_depths"]["report_pending"] = sum(
        1 for row in report if row["sent_at"] is None and row["superseded_at"] is None
    )

    observation = rows.get("local_observation_queue", [])
    for row in observation:
        record("observation", f"observation:{row['queue_id']}", row["sent_at"] is None)
    summary["queue_depths"]["observation_pending"] = sum(
        1 for row in observation if row["sent_at"] is None
    )

    health = rows.get("local_health_queue", [])
    for row in health:
        record("health", f"health:{row['queue_id']}", row["sent_at"] is None)
    summary["queue_depths"]["health_pending"] = sum(1 for row in health if row["sent_at"] is None)

    evidence = rows.get("local_evidence_queue", [])
    for row in evidence:
        key = f"evidence:{row['station_id']}:{row['instance_id']}:{row['anchor']}"
        record("evidence", key, row["uploaded_at"] is None)
    summary["queue_depths"]["evidence_pending"] = sum(
        1 for row in evidence if row["uploaded_at"] is None
    )

    disposals = rows.get("local_disposal", [])
    for row in disposals:
        key = f"disposal:{row['station_id']}:{row['idempotency_key']}"
        record("disposal", key, row["result_kind"] is None)
        summary["disposals"].append(
            {
                "key": key,
                "action_kind": row["action_kind"],
                "result_kind": row["result_kind"],
                "result_at": row["result_at"],
                "attempts": row["attempts"],
            }
        )
    summary["queue_depths"]["disposal_pending"] = sum(
        1 for row in disposals if row["result_kind"] is None
    )
    summary["queue_identities"] = identities


def _instance_summary(rows: dict[str, list[dict[str, Any]]], summary: dict[str, Any]) -> None:
    for row in rows.get("local_sop_instance", []):
        key = f"instance:{row['station_id']}:{row['instance_id']}"
        item = {
            "key": key,
            "station_id": row["station_id"],
            "instance_id": row["instance_id"],
            "closed_at": row["closed_at"],
            "lifecycle": row["lifecycle"],
        }
        summary["instances"].append(item)
        if row["closed_at"] is None:
            summary["open_instances"].append(item)
    for row in rows.get("local_decision", []):
        summary["decisions"].append(
            {
                "key": f"decision:{row['decision_id']}",
                "station_id": row["station_id"],
                "instance_id": row["instance_id"],
                "verdict": row["verdict"],
                "reasons": row["reasons"],
                "lifecycle": row["lifecycle"],
            }
        )


def _provenance_summary(rows: dict[str, list[dict[str, Any]]], summary: dict[str, Any]) -> None:
    for row in rows.get("local_report_queue", []):
        summary["provenance"].append(
            {
                "key": f"report:{row['queue_id']}",
                "report_kind": row["report_kind"],
                "station_id": row["station_id"],
                "instance_id": row["instance_id"],
                "report_host_id": row["report_host_id"],
                "report_backend_id": row["report_backend_id"],
                "template_version_id": row["report_template_version_id"],
                "template_sha256": row["report_template_sha256"],
                "model_ids": row["report_model_ids"],
                "backend_provenance": row["report_backend_provenance"],
                "configuration_revision": row["configuration_revision"],
                "configuration_sha256": row["configuration_sha256"],
            }
        )
    for row in rows.get("local_observation_queue", []):
        summary["provenance"].append(
            {
                "key": f"observation:{row['queue_id']}",
                "report_kind": "observation",
                "station_id": row["station_id"],
                "instance_id": row["instance_id"],
                "report_host_id": row["report_host_id"],
                "report_backend_id": None,
                "template_version_id": row["report_template_version_id"],
                "template_sha256": row["report_template_sha256"],
                "model_ids": None,
                "backend_provenance": row["report_backend_provenance"],
                "configuration_revision": None,
                "configuration_sha256": None,
            }
        )


def _logical_digest(version: int, rows: dict[str, list[dict[str, Any]]]) -> str:
    lines = [f"schema:{version}"]
    for table, items in rows.items():
        lines.append(f"table:{table}:{len(items)}")
        for item in items:
            lines.append(
                "\x1f".join("" if value is None else str(value) for value in item.values())
            )
    return _sha256_bytes("\n".join(lines).encode("utf-8"))


def _evidence_files(directory: Path) -> dict[str, dict[str, Any]]:
    if not directory.is_dir():
        raise UsageError("evidence directory not found")
    files: dict[str, dict[str, Any]] = {}
    for path in sorted(directory.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        relative = path.relative_to(directory).as_posix()
        # 只保留路径摘要，文件树布局不进入证据包。
        files[_sha256_bytes(relative.encode("utf-8"))] = {
            "sha256": _sha256_file(path),
            "size": path.stat().st_size,
        }
    return files


def _center_script() -> str:
    lines = ["BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY;"]
    for key, sql, _ in CENTER_QUERIES:
        lines.append(f"\\echo nvsop_table:{key}")
        lines.append(f"{sql};")
    lines.append("COMMIT;")
    return "\n".join(lines) + "\n"


def _run_psql(script: str, host_id: str) -> str:
    # SQL 经 stdin（-f -）交给 psql，让它替换 :'nvsop_host'；-w 永不提示密码；
    # 不传 DSN，连接参数只来自调用方的 libpq 外部 secret 环境。
    args = [
        "psql",
        "-X",
        "-q",
        "-A",
        "-t",
        "-F",
        "\t",
        "-w",
        "-v",
        "ON_ERROR_STOP=1",
        "-v",
        f"nvsop_host={host_id}",
        "-f",
        "-",
    ]
    try:
        result = subprocess.run(
            args, input=script, capture_output=True, text=True, timeout=120, check=False
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise CenterUnavailableError("psql could not run") from error
    if result.returncode != 0:
        # 不记录 stderr：可能含连接串片段或其他秘密。
        raise CenterUnavailableError(f"psql returned {result.returncode}")
    return result.stdout


def collect_center(host_id: str) -> dict[str, Any]:
    if shutil.which("psql") is None:
        raise CenterUnavailableError("psql not found on PATH")
    if not os.environ.get("PGHOST") or not os.environ.get("PGDATABASE"):
        raise CenterUnavailableError("libpq environment (PGHOST/PGDATABASE) is not configured")
    columns_by_table = {key: columns for key, _, columns in CENTER_QUERIES}
    objects_by_table: dict[str, dict[str, Any]] = {key: {} for key, _, _ in CENTER_QUERIES}
    current: str | None = None
    for line in _run_psql(_center_script(), host_id).splitlines():
        if line.startswith("nvsop_table:"):
            current = line.split(":", 1)[1]
            continue
        if current is None or not line:
            continue
        columns = columns_by_table[current]
        values = line.split("\t")
        if len(values) != len(columns) + 1:
            raise CenterUnavailableError(f"unexpected column count for {current}")
        # 全列都在白名单内且不可变，所以整行 fingerprint 就是该镜像对象的不可变来源证明。
        objects_by_table[current][values[0]] = {"provenance": _fingerprint(values[1:])}
    return {
        "status": "collected",
        "tables": {
            key: {"count": len(objects), "objects": objects}
            for key, objects in objects_by_table.items()
        },
    }


def build_snapshot(arguments: argparse.Namespace) -> tuple[dict[str, Any], int]:
    if not CANDIDATE_SHA.fullmatch(arguments.candidate_sha):
        raise UsageError("--candidate-sha must be a 40-character lowercase hex SHA")
    if not arguments.host_id.strip():
        raise UsageError("--host-id must be non-empty")
    sqlite_path = Path(arguments.sqlite)
    summary = _read_sqlite(sqlite_path)
    configuration = summary.get("configuration")
    if isinstance(configuration, dict) and configuration.get("host_id") != arguments.host_id:
        # 采错库是最危险的静默错误：明确拒绝而不是产出看似完整的证据。
        raise UsageError("local_state host_id does not match --host-id")
    document: dict[str, Any] = {
        "tool": "validate_edge_restart",
        "snapshot_schema": SNAPSHOT_SCHEMA,
        "tool_version": TOOL_VERSION,
        "role": arguments.role,
        "captured_at_utc": _utc_now(),
        "environment": {
            "host_id": arguments.host_id,
            "candidate_sha": arguments.candidate_sha,
            "label": arguments.environment,
            "sqlite_name": _safe_token(sqlite_path.name),
            "evidence_name": _safe_token(Path(arguments.evidence_dir).name),
        },
        "sqlite": summary,
        "evidence_files": _evidence_files(Path(arguments.evidence_dir)),
        "center": {"status": "not_attempted", "tables": {}},
    }
    if not summary["compatible"]:
        return document, 2
    if arguments.center:
        try:
            document["center"] = collect_center(arguments.host_id)
        except CenterUnavailableError as error:
            document["center"] = {"status": "unavailable", "tables": {}, "reason": str(error)}
            return document, 3
        return document, 0
    return document, 3


def _check(check_id: str, status: str, detail: str) -> dict[str, str]:
    return {"id": check_id, "status": status, "detail": detail}


def _preserved(before: dict[str, Any], after: dict[str, Any], state: str) -> tuple[bool, str]:
    before_keys = {
        item["key"]
        for items in before["sqlite"]["queue_identities"].values()
        for item in items
        if item["state"] == state
    }
    after_keys = {
        item["key"] for items in after["sqlite"]["queue_identities"].values() for item in items
    }
    missing = sorted(before_keys - after_keys)
    return not missing, f"missing {len(missing)} {state} identities: {missing[:5]}"


PROVENANCE_FIELDS = (
    "station_id",
    "instance_id",
    "report_host_id",
    "report_backend_id",
    "template_version_id",
    "template_sha256",
    "model_ids",
    "backend_provenance",
    "configuration_revision",
    "configuration_sha256",
)


def _provenance_check(before: dict[str, Any], after: dict[str, Any]) -> tuple[bool, str]:
    after_by_key = {item["key"]: item for item in after["sqlite"]["provenance"]}
    replaced: list[str] = []
    for item in before["sqlite"]["provenance"]:
        current = after_by_key.get(item["key"])
        if current is None or any(current[field] != item[field] for field in PROVENANCE_FIELDS):
            replaced.append(item["key"])
    return not replaced, f"provenance missing or replaced: {sorted(replaced)[:5]}"


def _interrupted_check(before: dict[str, Any], after: dict[str, Any]) -> tuple[bool, str]:
    instances = {item["key"]: item for item in after["sqlite"]["instances"]}
    interrupted = {
        (item["station_id"], item["instance_id"])
        for item in after["sqlite"]["decisions"]
        if isinstance(item["reasons"], list) and "RUN_INTERRUPTED" in item["reasons"]
    }
    failures: list[str] = []
    for item in before["sqlite"]["open_instances"]:
        current = instances.get(item["key"])
        if (
            current is None
            or current["closed_at"] is None
            or (item["station_id"], item["instance_id"]) not in interrupted
        ):
            failures.append(item["key"])
    return not failures, f"not closed as RUN_INTERRUPTED: {sorted(failures)[:5]}"


def _media_check(before: dict[str, Any], after: dict[str, Any]) -> tuple[bool, str]:
    after_files = after["evidence_files"]
    changed = sorted(
        name
        for name, item in before["evidence_files"].items()
        if after_files.get(name, {}).get("sha256") != item["sha256"]
    )
    return not changed, f"missing or changed media: {changed[:5]}"


def _terminal_disposal_check(before: dict[str, Any], after: dict[str, Any]) -> tuple[bool, str]:
    after_by_key = {item["key"]: item for item in after["sqlite"]["disposals"]}
    changed: list[str] = []
    for item in before["sqlite"]["disposals"]:
        if item["result_kind"] is None:
            continue
        current = after_by_key.get(item["key"])
        if current is None or any(
            current[field] != item[field] for field in ("result_kind", "result_at", "attempts")
        ):
            changed.append(item["key"])
    return not changed, f"terminal disposal result or attempts changed: {sorted(changed)[:5]}"


def _configuration_check(before: dict[str, Any], after: dict[str, Any]) -> tuple[str, str]:
    current = before["sqlite"].get("configuration")
    later = after["sqlite"].get("configuration")
    if not isinstance(current, dict) or not isinstance(later, dict):
        return "needs_field", "no confirmed local configuration in both snapshots"
    if current.get("config_revision") == later.get("config_revision") and current.get(
        "sha256"
    ) == later.get("sha256"):
        return "pass", "last confirmed configuration revision and digest unchanged"
    return "fail", "last confirmed configuration revision or digest changed"


def _replay_local_identity_check(before: dict[str, Any], after: dict[str, Any]) -> tuple[bool, str]:
    """replay 固定确认阶段只允许确认既有积压，不应新建本机处置/证据待办身份。"""
    new: list[str] = []
    for queue in ("evidence", "disposal"):
        known = {item["key"] for item in before["sqlite"]["queue_identities"].get(queue, [])}
        current = {item["key"] for item in after["sqlite"]["queue_identities"].get(queue, [])}
        new.extend(sorted(current - known))
    return not new, f"new local evidence/disposal identities during fixed replay: {new[:5]}"


def _center_check(before: dict[str, Any], after: dict[str, Any], scenario: str) -> tuple[str, str]:
    if before["center"]["status"] != "collected" or after["center"]["status"] != "collected":
        return "needs_field", "center PostgreSQL evidence not collected in both snapshots"
    current_tables = after["center"]["tables"]
    loss: list[str] = []
    provenance: list[str] = []
    for table, payload in before["center"]["tables"].items():
        current = current_tables.get(table, {}).get("objects", {})
        for event_id, entry in payload["objects"].items():
            if event_id not in current:
                loss.append(f"{table}:{event_id}")
            elif current[event_id].get("provenance") != entry.get("provenance"):
                provenance.append(f"{table}:{event_id}")
    # replay 场景在现场已暂停新输入后只做固定积压重放：受控主机不应出现任何新镜像身份。
    # restart/rebind 允许合法新事件与新实例；也不把同一 instance 的开/闭两条 fact 误当重复。
    added: list[str] = []
    if scenario == "replay":
        for table, payload in current_tables.items():
            known = before["center"]["tables"].get(table, {}).get("objects", {})
            added.extend(
                f"{table}:{event_id}"
                for event_id in payload.get("objects", {})
                if event_id not in known
            )
    if loss:
        return "fail", f"mirror identity missing after restart: {sorted(loss)[:5]}"
    if provenance:
        return "fail", f"immutable center provenance changed: {sorted(provenance)[:5]}"
    if added:
        return "fail", f"new mirror identities during fixed replay: {sorted(added)[:5]}"
    return "pass", "pre-restart mirror identity and provenance preserved per scenario"


def compare_snapshots(
    before: dict[str, Any], after: dict[str, Any], scenario: str
) -> dict[str, Any]:
    if scenario not in SCENARIOS:
        raise UsageError(f"--scenario must be one of {', '.join(SCENARIOS)}")
    if before["snapshot_schema"] != SNAPSHOT_SCHEMA or after["snapshot_schema"] != SNAPSHOT_SCHEMA:
        raise UsageError("unsupported snapshot schema")
    if before["environment"]["host_id"] != after["environment"]["host_id"]:
        raise UsageError("snapshots belong to different hosts")
    if before["environment"]["candidate_sha"] != after["environment"]["candidate_sha"]:
        raise UsageError("snapshots were captured under different candidate SHAs")

    checks: list[dict[str, str]] = []
    versions = {before["sqlite"]["schema_version"], after["sqlite"]["schema_version"]}
    if versions == {EXPECTED_SCHEMA_VERSION}:
        checks.append(_check("schema_version", "pass", f"schema {EXPECTED_SCHEMA_VERSION}"))
    else:
        checks.append(
            _check("schema_version", "fail", f"incompatible schema versions {sorted(versions)}")
        )

    # 三场景共同保留的基本约束：旧待办/终态身份、冻结来源、媒体与已终态处置。
    for check_id, state in (
        ("pending_identities_preserved", "pending"),
        ("terminal_identities_preserved", "settled"),
    ):
        passed, detail = _preserved(before, after, state)
        checks.append(_check(check_id, "pass" if passed else "fail", detail))

    passed, detail = _provenance_check(before, after)
    checks.append(_check("provenance_locked", "pass" if passed else "fail", detail))

    passed, detail = _media_check(before, after)
    checks.append(_check("media_intact", "pass" if passed else "fail", detail))

    passed, detail = _terminal_disposal_check(before, after)
    checks.append(_check("terminal_disposals_stable", "pass" if passed else "fail", detail))

    # restart 要求活动实例以 RUN_INTERRUPTED 结案、最后确认配置不变；rebind 明确允许改绑
    # 与配置变化，不要求活动实例被关闭；replay 只验证固定积压重放的镜像幂等。
    if scenario == "restart":
        passed, detail = _interrupted_check(before, after)
        checks.append(_check("interrupted_closed", "pass" if passed else "fail", detail))
        configuration_status, configuration_detail = _configuration_check(before, after)
        checks.append(_check("configuration_effective", configuration_status, configuration_detail))

    if scenario == "replay":
        passed, detail = _replay_local_identity_check(before, after)
        checks.append(
            _check("replay_local_identities_stable", "pass" if passed else "fail", detail)
        )

    center_status, center_detail = _center_check(before, after, scenario)
    checks.append(_check("center_mirror", center_status, center_detail))

    statuses = {check["status"] for check in checks}
    if "fail" in statuses:
        data_checks = "failed"
    elif "needs_field" in statuses:
        data_checks = "incomplete"
    else:
        data_checks = "passed"
    return {
        "tool": "validate_edge_restart",
        "kind": "comparison",
        "tool_version": TOOL_VERSION,
        "scenario": scenario,
        "before": {
            "host_id": before["environment"]["host_id"],
            "candidate_sha": before["environment"]["candidate_sha"],
            "captured_at_utc": before["captured_at_utc"],
        },
        "after": {
            "host_id": after["environment"]["host_id"],
            "candidate_sha": after["environment"]["candidate_sha"],
            "captured_at_utc": after["captured_at_utc"],
        },
        "checks": checks,
        "data_checks": data_checks,
        "field_acceptance": "not_asserted",
        "needs_field": list(NEEDS_FIELD),
    }


def _is_within(path: Path, base: Path) -> bool:
    return path == base or base in path.parents


def _ensure_distinct_output(
    output: Path, *, files: Sequence[Path] = (), directories: Sequence[Path] = ()
) -> None:
    resolved = output.resolve()
    for candidate in files:
        if resolved == candidate.resolve():
            raise UsageError("output must differ from an input file")
    for directory in directories:
        if _is_within(resolved, directory.resolve()):
            raise UsageError("output must not be written inside an input directory")


def _write_result(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # 独占创建，避免并发或重复运行覆盖已有结果。
    try:
        with path.open("x", encoding="utf-8") as stream:
            json.dump(document, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
    except FileExistsError as error:
        raise UsageError(f"refusing to overwrite existing result: {path.name}") from error


def _load_snapshot(path: Path) -> dict[str, Any]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise UsageError(f"cannot read snapshot: {path.name}") from error
    if not isinstance(document, dict) or document.get("tool") != "validate_edge_restart":
        raise UsageError(f"not a validate_edge_restart snapshot: {path.name}")
    return document


def _snapshot_output(arguments: argparse.Namespace) -> Path:
    if arguments.output is not None:
        return Path(arguments.output)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return REPOSITORY_ROOT / DEFAULT_OUTPUT_DIR / f"{arguments.role}-{stamp}.json"


def _snapshot_command(arguments: argparse.Namespace) -> int:
    output = _snapshot_output(arguments)
    _ensure_distinct_output(
        output,
        files=[Path(arguments.sqlite)],
        directories=[Path(arguments.evidence_dir)],
    )
    document, exit_code = build_snapshot(arguments)
    _write_result(output, document)
    if exit_code == 2:
        print("snapshot refused: local_state schema is not the supported version", file=sys.stderr)
    elif exit_code == 3:
        print("snapshot incomplete: center evidence not collected", file=sys.stderr)
    return exit_code


def _compare_command(arguments: argparse.Namespace) -> int:
    before_path = Path(arguments.before)
    after_path = Path(arguments.after)
    before = _load_snapshot(before_path)
    after = _load_snapshot(after_path)
    document = compare_snapshots(before, after, arguments.scenario)
    if arguments.output is not None:
        _ensure_distinct_output(Path(arguments.output), files=[before_path, after_path])
        _write_result(Path(arguments.output), document)
    else:
        sys.stdout.write(json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    if document["data_checks"] == "failed":
        return 1
    if document["data_checks"] == "incomplete":
        return 3
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    snapshot = subparsers.add_parser("snapshot", help="只读采集一次本机与中心证据")
    snapshot.add_argument("--role", choices=("before", "after"), required=True)
    snapshot.add_argument("--sqlite", required=True, help="本机 local_state SQLite 路径")
    snapshot.add_argument("--evidence-dir", required=True, help="本机证据目录")
    snapshot.add_argument("--host-id", required=True, help="推理机主机身份")
    snapshot.add_argument("--candidate-sha", required=True, help="候选 40hex SHA")
    snapshot.add_argument("--environment", default="", help="操作员环境身份标签")
    snapshot.add_argument("--center", action="store_true", help="经 psql/libpq 采集 Center")
    snapshot.add_argument("--output", help="结果路径；缺省写入 .nvsop/artifacts/")

    compare = subparsers.add_parser("compare", help="按场景比较前后两份快照的事实")
    compare.add_argument("--scenario", choices=SCENARIOS, required=True)
    compare.add_argument("--before", required=True)
    compare.add_argument("--after", required=True)
    compare.add_argument("--output", help="结果路径；缺省打印到 stdout")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    arguments = parser.parse_args(argv)
    try:
        if arguments.command == "snapshot":
            return _snapshot_command(arguments)
        return _compare_command(arguments)
    except UsageError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
