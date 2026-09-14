"""原子确认本地配置,并回退到最后确认版本。"""

from __future__ import annotations

import json
import sqlite3
from contextlib import AbstractContextManager
from dataclasses import dataclass
from threading import RLock
from typing import cast

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
                "SELECT payload FROM local_config WHERE slot = 1"
            ).fetchone()
        if row is None:
            return None
        try:
            value = json.loads(row[0])
        except (TypeError, json.JSONDecodeError) as error:
            raise ValueError("confirmed local configuration is not valid JSON") from error
        if not isinstance(value, dict):
            raise ValueError("confirmed local configuration is not an object")
        return configuration_from_wire(value)

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
                    "SELECT config_revision, sha256, payload FROM local_config WHERE slot = 1"
                ).fetchone()
                if current is not None:
                    if current[0] > bundle.config_revision:
                        raise ValueError(
                            "configuration revision is older than the last confirmation"
                        )
                    if current[0] == bundle.config_revision and current[1] != bundle.sha256:
                        previous_generated_at = _payload_generated_at(current[2])
                        if bundle.generated_at <= previous_generated_at:
                            raise ValueError(
                                "configuration revision was reused with different content"
                            )
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


def _payload_generated_at(payload: object) -> str:
    """读取旧配置的生成时刻,拒绝时间倒退的同版本响应。"""
    try:
        value = json.loads(str(payload))
    except (TypeError, json.JSONDecodeError) as error:
        raise ValueError("confirmed local configuration is not valid JSON") from error
    if not isinstance(value, dict) or not isinstance(value.get("generated_at"), str):
        raise ValueError("confirmed local configuration has no generated_at")
    return cast(str, value["generated_at"])


__all__ = ["ConfigurationFailure", "LocalConfigurationStore"]
