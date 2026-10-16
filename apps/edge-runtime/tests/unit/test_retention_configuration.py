"""S041: Edge 只消费中心解析后的生效保留组, 并按严格能力门禁拒绝半套配置。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import unittest
from dataclasses import replace
from typing import cast

from nvsop_contracts import (
    RETENTION_STATION_POLICY_CAPABILITY,
    ConfigurationBundle,
    ResolvedRetentionPolicy,
    ResolvedRetentionRule,
    canonical_json,
    configuration_from_wire,
    configuration_to_wire,
)
from test_runtime_configuration import confirmed_bundle, local_connector, local_station

from edge_runtime.configuration_sync import ConfigurationSynchronizer
from edge_runtime.local_state.schema import migrate
from edge_runtime.local_state.store import LocalState
from edge_runtime.runtime_configuration import confirmed_runtime_configuration


def resolved_retention() -> ResolvedRetentionPolicy:
    return ResolvedRetentionPolicy(
        evidence_pass=ResolvedRetentionRule("discard"),
        evidence_fail=ResolvedRetentionRule("permanent"),
        evidence_indeterminate=ResolvedRetentionRule("duration", 86400),
        record_detail=ResolvedRetentionRule("duration", 86400),
        record_compression_age=ResolvedRetentionRule("duration", 3600),
        record_aggregate_age=ResolvedRetentionRule("duration", 86400),
        disposal=ResolvedRetentionRule("permanent"),
        local_reported=ResolvedRetentionRule("duration", 604800),
    )


def with_retention(bundle: ConfigurationBundle) -> ConfigurationBundle:
    """带生效组的候选必须同时声明对应能力, 否则契约自身就拒绝。"""

    return replace(
        bundle,
        required_capabilities=tuple(
            sorted((*bundle.required_capabilities, RETENTION_STATION_POLICY_CAPABILITY))
        ),
        stations=tuple(
            replace(station, retention=resolved_retention()) for station in bundle.stations
        ),
    )


def with_capability_only(bundle: ConfigurationBundle) -> ConfigurationBundle:
    return replace(
        bundle,
        required_capabilities=tuple(
            sorted((*bundle.required_capabilities, RETENTION_STATION_POLICY_CAPABILITY))
        ),
    )


def first_station_wire(bundle: ConfigurationBundle) -> dict[str, object]:
    stations = configuration_to_wire(bundle)["stations"]
    assert isinstance(stations, list)
    first = stations[0]
    assert isinstance(first, dict)
    return cast("dict[str, object]", first)


def re_digest(wire: dict[str, object]) -> dict[str, object]:
    """按中心与边界同一 canonical 规则重算摘要, 用于手工构造非法 wire。"""

    wire["sha256"] = hashlib.sha256(
        canonical_json({key: value for key, value in wire.items() if key != "sha256"}).encode(
            "utf-8"
        )
    ).hexdigest()
    return wire


def drop_station_retention(wire: dict[str, object]) -> dict[str, object]:
    raw_stations = wire["stations"]
    assert isinstance(raw_stations, list)
    station = raw_stations[0]
    assert isinstance(station, dict)
    station.pop("retention")
    return wire


class ScriptedPuller:
    def __init__(self, candidate: ConfigurationBundle) -> None:
        self.candidate = candidate

    def pull(self) -> ConfigurationBundle:
        return self.candidate


class RetentionConfigurationTests(unittest.TestCase):
    def test_confirmed_station_consumes_the_center_resolved_group(self) -> None:
        bundle = with_retention(confirmed_bundle())

        result = confirmed_runtime_configuration(
            bundle=bundle,
            bootstrap_stations=(local_station(),),
            local_connectors=(local_connector(),),
        )

        self.assertEqual(result.stations[0].configuration.retention_policy, resolved_retention())

    def test_history_without_the_group_still_composes(self) -> None:
        # 引入本字段之前的确认 payload 没有生效组也没有该能力: 仍可组合, 且不凭空产生保留行为。

        result = confirmed_runtime_configuration(
            bundle=confirmed_bundle(),
            bootstrap_stations=(local_station(),),
            local_connectors=(local_connector(),),
        )

        self.assertIsNone(result.stations[0].configuration.retention_policy)

    def test_a_group_without_the_capability_is_refused(self) -> None:
        wire = configuration_to_wire(with_retention(confirmed_bundle()))
        wire.pop("required_capabilities")

        with self.assertRaises(ValueError):
            configuration_from_wire(re_digest(wire))

    def test_the_capability_without_every_group_is_refused(self) -> None:
        # 声明能力必须每站带完整组: 构造与解析都拒绝。
        with self.assertRaises(ValueError):
            with_capability_only(confirmed_bundle())

        wire = drop_station_retention(configuration_to_wire(with_retention(confirmed_bundle())))

        with self.assertRaises(ValueError):
            configuration_from_wire(re_digest(wire))

    def test_synchronizer_accepts_the_declared_retention_capability(self) -> None:
        connection = sqlite3.connect(":memory:", isolation_level=None)
        connection.row_factory = sqlite3.Row
        migrate(connection)
        state = LocalState(connection)
        try:
            synchronizer = ConfigurationSynchronizer(
                puller=ScriptedPuller(with_retention(confirmed_bundle())),
                store=state.configuration(),
            )

            result = synchronizer.synchronize(observed_at=1.0)

            self.assertIsNotNone(result.candidate)
            self.assertIsNone(result.failure)
        finally:
            state.close()


class HistoricalRetentionPayloadTests(unittest.TestCase):
    def test_current_format_payload_without_the_group_restores_and_reconfirms(self) -> None:
        connection = sqlite3.connect(":memory:", isolation_level=None)
        connection.row_factory = sqlite3.Row
        migrate(connection)
        state = LocalState(connection)
        try:
            bundle = replace(
                confirmed_bundle(),
                config_revision=1,
                required_capabilities=(),
            )
            self.assertNotIn("retention", first_station_wire(bundle))
            payload = json.dumps(
                configuration_to_wire(bundle),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            connection.execute(
                """
                INSERT INTO local_config
                    (slot, host_id, config_revision, sha256, confirmed_at, payload)
                VALUES (1, ?, ?, ?, ?, ?)
                """,
                (bundle.host_id, bundle.config_revision, bundle.sha256, 1.0, payload),
            )

            restored = state.configuration().confirmed()

            self.assertIsNotNone(restored)
            assert restored is not None
            self.assertEqual(restored, bundle)
            state.configuration().confirm(restored, confirmed_at=2.0)
            self.assertEqual(state.configuration().confirmed(), bundle)
        finally:
            state.close()
