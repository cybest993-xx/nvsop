"""原子确认本地配置,并回退到最后确认版本。"""

from __future__ import annotations

import base64
import copy
import json
import sqlite3
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, datetime
from threading import RLock

from nvsop_contracts import ConfigurationBundle, configuration_from_wire, configuration_to_wire


@dataclass(frozen=True, slots=True)
class ConfigurationFailure:
    code: str
    detail: str
    observed_at: float


class LocalConfigurationStore:
    """拥有 ``local_config``,绝不使用未验证响应替换它。"""

    def __init__(
        self,
        connection: sqlite3.Connection,
        lock: AbstractContextManager[object] | None = None,
    ) -> None:
        self._connection = connection
        self._lock = lock or RLock()

    def confirmed(self) -> ConfigurationBundle | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT host_id, config_revision, confirmed_at, payload "
                "FROM local_config WHERE slot = 1"
            ).fetchone()
        if row is None:
            return None
        return _bundle_from_row(row)

    def failure(self) -> ConfigurationFailure | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT code, detail, observed_at FROM local_config_failure WHERE slot = 1"
            ).fetchone()
        if row is None:
            return None
        return ConfigurationFailure(code=row[0], detail=row[1], observed_at=row[2])

    def confirm(self, bundle: ConfigurationBundle, *, confirmed_at: float) -> None:
        """在一个事务中替换完整 bundle 并清除之前的失败记录。"""
        payload = json.dumps(
            configuration_to_wire(bundle), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                current = self._connection.execute(
                    "SELECT host_id, config_revision, confirmed_at, payload "
                    "FROM local_config WHERE slot = 1"
                ).fetchone()
                if current is not None:
                    current_bundle = _bundle_from_row(current)
                    if current_bundle.host_id != bundle.host_id:
                        raise ValueError("configuration host scope cannot change in local state")
                    if current_bundle.config_revision > bundle.config_revision:
                        raise ValueError(
                            "configuration revision is older than the last confirmation"
                        )
                    if (
                        current_bundle.config_revision == bundle.config_revision
                        and current_bundle.stable_content_wire() != bundle.stable_content_wire()
                    ):
                        raise ValueError("configuration revision was reused with different content")
                    if current_bundle.config_revision == bundle.config_revision and _generated_at(
                        bundle
                    ) < _generated_at(current_bundle):
                        raise ValueError("configuration revision was reused with different content")
                self._connection.execute(
                    """
                    INSERT INTO local_config
                        (slot, host_id, config_revision, confirmed_at, payload)
                    VALUES (1, ?, ?, ?, ?)
                    ON CONFLICT (slot) DO UPDATE SET
                        host_id = excluded.host_id,
                        config_revision = excluded.config_revision,
                        confirmed_at = excluded.confirmed_at,
                        payload = excluded.payload
                    """,
                    (
                        bundle.host_id,
                        bundle.config_revision,
                        confirmed_at,
                        payload,
                    ),
                )
                self._connection.execute("DELETE FROM local_config_failure WHERE slot = 1")
            except Exception:
                self._connection.execute("ROLLBACK")
                raise
            self._connection.execute("COMMIT")

    def record_failure(self, *, code: str, detail: str, observed_at: float) -> None:
        if not code or not detail:
            raise ValueError("configuration failures need a code and detail")
        with self._lock:
            self._connection.execute(
                """
                INSERT INTO local_config_failure (slot, code, detail, observed_at)
                VALUES (1, ?, ?, ?)
                ON CONFLICT (slot) DO UPDATE SET
                    code = excluded.code,
                    detail = excluded.detail,
                    observed_at = excluded.observed_at
                """,
                (code, detail, observed_at),
            )


def _bundle_from_row(row: sqlite3.Row | tuple[object, ...]) -> ConfigurationBundle:
    payload = row[3]
    if not isinstance(payload, (str, bytes, bytearray)):
        raise ValueError("confirmed local configuration payload is not text")
    try:
        value = json.loads(payload)
    except (TypeError, json.JSONDecodeError) as error:
        raise ValueError("confirmed local configuration is not valid JSON") from error
    if not isinstance(value, dict):
        raise ValueError("confirmed local configuration is not an object")
    bundle = configuration_from_wire(_upgrade_legacy_wire(value))
    if bundle.host_id != row[0] or bundle.config_revision != row[1]:
        raise ValueError("confirmed local configuration metadata does not match its payload")
    return bundle


def _upgrade_legacy_wire(value: dict[str, object]) -> dict[str, object]:
    """把已落盘的旧摘要字段清洗为当前本地配置格式。"""
    if value.get("contract_version") != 1:
        return value
    upgraded = copy.deepcopy(value)
    upgraded.pop("sha256", None)
    upgraded["contract_version"] = 2
    raw_stations = upgraded.get("stations")
    if not isinstance(raw_stations, list):
        return upgraded
    for raw_station in raw_stations:
        if not isinstance(raw_station, dict):
            continue
        raw_station.setdefault("cameras", [])
        raw_template = raw_station.get("template")
        if not isinstance(raw_template, dict):
            continue
        raw_template.pop("version_sha256", None)
        raw_artifacts = raw_template.get("artifacts", [])
        if not isinstance(raw_artifacts, list):
            continue
        for raw_artifact in raw_artifacts:
            if isinstance(raw_artifact, dict):
                raw_artifact.pop("sha256", None)
        manifest = next(
            (
                item
                for item in raw_artifacts
                if isinstance(item, dict) and item.get("name") == "manifest.json"
            ),
            None,
        )
        if manifest is None:
            continue
        try:
            non_manifest = [item for item in raw_artifacts if item is not manifest]
            manifest_content = json.dumps(
                {
                    "artifacts": [
                        {
                            "byte_length": len(base64.b64decode(item["content_base64"])),
                            "media_type": item["media_type"],
                            "name": item["name"],
                        }
                        for item in non_manifest
                    ],
                    "format_version": 1,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            manifest["content_base64"] = base64.b64encode(manifest_content).decode("ascii")
        except (KeyError, TypeError, ValueError):
            continue
    return upgraded


def _generated_at(bundle: ConfigurationBundle) -> datetime:
    value = bundle.generated_at.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed):
        raise ValueError("configuration generated_at must be UTC")
    return parsed


__all__ = ["ConfigurationFailure", "LocalConfigurationStore"]
