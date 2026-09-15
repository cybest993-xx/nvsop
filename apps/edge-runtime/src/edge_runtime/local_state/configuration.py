"""原子确认本地配置,并回退到最后确认版本。"""

from __future__ import annotations

import base64
import copy
import hashlib
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
                "SELECT host_id, config_revision, sha256, confirmed_at, payload "
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
        payload_value = configuration_to_wire(bundle)
        payload = json.dumps(
            payload_value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        # Confirm is the last integrity boundary before replacing the active local view.
        # Reparse the exact serialized bytes so the value validated here is the value stored.
        stored_value = json.loads(payload)
        if not isinstance(stored_value, dict):
            raise ValueError("serialized configuration payload is not an object")
        configuration_from_wire(stored_value)
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                current = self._connection.execute(
                    "SELECT host_id, config_revision, sha256, confirmed_at, payload "
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
                        (slot, host_id, config_revision, sha256, confirmed_at, payload)
                    VALUES (1, ?, ?, ?, ?, ?)
                    ON CONFLICT (slot) DO UPDATE SET
                        host_id = excluded.host_id,
                        config_revision = excluded.config_revision,
                        sha256 = excluded.sha256,
                        confirmed_at = excluded.confirmed_at,
                        payload = excluded.payload
                    """,
                    (
                        bundle.host_id,
                        bundle.config_revision,
                        bundle.sha256,
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
    payload = row[4]
    if not isinstance(payload, (str, bytes, bytearray)):
        raise ValueError("confirmed local configuration payload is not text")
    try:
        value = json.loads(payload)
    except (TypeError, json.JSONDecodeError) as error:
        raise ValueError("confirmed local configuration is not valid JSON") from error
    if not isinstance(value, dict):
        raise ValueError("confirmed local configuration is not an object")
    legacy_digest = value.get("sha256")
    original_contract_version = value.get("contract_version")
    bundle = configuration_from_wire(_upgrade_legacy_wire(value))
    if bundle.host_id != row[0] or bundle.config_revision != row[1]:
        raise ValueError("confirmed local configuration metadata does not match its payload")
    if bundle.sha256 != row[2] and not (original_contract_version == 1 and legacy_digest == row[2]):
        raise ValueError("confirmed local configuration metadata does not match its payload")
    return bundle


def _upgrade_legacy_wire(value: dict[str, object]) -> dict[str, object]:
    """把已落盘的旧摘要格式升级为当前格式,但不放宽 HTTP 契约。"""
    contract_version = value.get("contract_version")
    if contract_version not in {1, 2}:
        return value
    upgraded = copy.deepcopy(value)
    upgraded["contract_version"] = 2
    raw_stations = upgraded.get("stations")
    if not isinstance(raw_stations, list):
        return upgraded
    for raw_station in raw_stations:
        if not isinstance(raw_station, dict):
            raise ValueError("confirmed local configuration station is invalid")
        raw_station.setdefault("cameras", [])
        raw_template = raw_station.get("template")
        if raw_template is None:
            continue
        if not isinstance(raw_template, dict):
            raise ValueError("confirmed local configuration template is invalid")
        raw_artifacts = raw_template.get("artifacts")
        if not isinstance(raw_artifacts, list):
            raise ValueError("confirmed local configuration artifacts are invalid")
        if (
            all(isinstance(item, dict) and "sha256" in item for item in raw_artifacts)
            and "version_sha256" in raw_template
        ):
            continue
        non_manifest: list[dict[str, object]] = []
        manifest: dict[str, object] | None = None
        for raw_artifact in raw_artifacts:
            if not isinstance(raw_artifact, dict):
                raise ValueError("confirmed local configuration artifact is invalid")
            if raw_artifact.get("name") == "manifest.json":
                manifest = raw_artifact
            else:
                non_manifest.append(raw_artifact)
        if manifest is None:
            raise ValueError("confirmed local configuration has no manifest")
        for artifact in non_manifest:
            content = _legacy_artifact_content(artifact)
            artifact["sha256"] = hashlib.sha256(content).hexdigest()
        manifest_content = json.dumps(
            {
                "artifacts": [
                    {
                        "byte_length": len(_legacy_artifact_content(artifact)),
                        "media_type": artifact["media_type"],
                        "name": artifact["name"],
                        "sha256": artifact["sha256"],
                    }
                    for artifact in non_manifest
                ],
                "format_version": 1,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        manifest["content_base64"] = base64.b64encode(manifest_content).decode("ascii")
        manifest["sha256"] = hashlib.sha256(manifest_content).hexdigest()
        raw_template["version_sha256"] = manifest["sha256"]
    upgraded.pop("sha256", None)
    upgraded["sha256"] = hashlib.sha256(
        _canonical_json({key: item for key, item in upgraded.items() if key != "sha256"}).encode(
            "utf-8"
        )
    ).hexdigest()
    return upgraded


def _legacy_artifact_content(value: dict[str, object]) -> bytes:
    encoded = value.get("content_base64")
    if not isinstance(encoded, str):
        raise ValueError("confirmed local configuration artifact content is invalid")
    try:
        return base64.b64decode(encoded, validate=True)
    except (TypeError, ValueError) as error:
        raise ValueError("confirmed local configuration artifact content is invalid") from error


def _canonical_json(value: dict[str, object]) -> str:
    try:
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
    except (TypeError, ValueError) as error:
        raise ValueError("confirmed local configuration is not canonical JSON") from error


def _generated_at(bundle: ConfigurationBundle) -> datetime:
    value = bundle.generated_at.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed):
        raise ValueError("configuration generated_at must be UTC")
    return parsed


__all__ = ["ConfigurationFailure", "LocalConfigurationStore"]
