"""S041 生效保留组的机器契约：可选字段、严格解码与运行语义身份。"""

from __future__ import annotations

import hashlib
import unittest
from dataclasses import replace
from typing import cast

from nvsop_contracts import (
    RETENTION_POLICY_FIELDS,
    RETENTION_RULE_MODES,
    RETENTION_STATION_POLICY_CAPABILITY,
    ConfigurationBundle,
    ConfiguredStation,
    ResolvedRetentionPolicy,
    ResolvedRetentionRule,
    ResolvedRuntimeParameters,
    canonical_json,
    configuration_from_wire,
    configuration_to_wire,
)


def group() -> ResolvedRetentionPolicy:
    return ResolvedRetentionPolicy(
        evidence_pass=ResolvedRetentionRule("discard"),
        evidence_fail=ResolvedRetentionRule("permanent"),
        evidence_indeterminate=ResolvedRetentionRule("duration", 3600),
        record_detail=ResolvedRetentionRule("duration", 3600),
        record_compression_age=ResolvedRetentionRule("duration", 600),
        record_aggregate_age=ResolvedRetentionRule("duration", 7200),
        disposal=ResolvedRetentionRule("permanent"),
        local_reported=ResolvedRetentionRule("duration", 1200),
    )


def station(retention: ResolvedRetentionPolicy | None = None) -> ConfiguredStation:
    return ConfiguredStation(
        station_id="station-a",
        backend_id="backend-a",
        code="S-A",
        name="Station A",
        revision=3,
        runtime_parameters=ResolvedRuntimeParameters(10, 4, "record"),
        connectors=(),
        points=(),
        template=None,
        retention=retention,
    )


def bundle(value: ConfiguredStation, *, capability: bool = True) -> ConfigurationBundle:
    return ConfigurationBundle(
        host_id="host-a",
        config_revision=7,
        generated_at="2026-09-13T00:00:00Z",
        stations=(value,),
        required_capabilities=(RETENTION_STATION_POLICY_CAPABILITY,) if capability else (),
    )


def first_station_wire(value: ConfigurationBundle) -> dict[str, object]:
    stations = configuration_to_wire(value)["stations"]
    assert isinstance(stations, list)
    first = stations[0]
    assert isinstance(first, dict)
    return cast("dict[str, object]", first)


class RetentionPolicyContractTests(unittest.TestCase):
    def test_group_is_complete_strict_and_wire_round_trips(self) -> None:
        value = group()

        self.assertEqual(set(value.to_wire()), set(RETENTION_POLICY_FIELDS))
        self.assertEqual(ResolvedRetentionPolicy.from_wire(value.to_wire()), value)
        self.assertEqual(RETENTION_RULE_MODES, frozenset({"duration", "permanent", "discard"}))
        for rejected in (
            {"mode": "unset", "seconds": None},
            {"mode": "duration", "seconds": 0},
            {"mode": "permanent", "seconds": 60},
        ):
            with self.subTest(rule=rejected), self.assertRaises(ValueError):
                ResolvedRetentionRule.from_wire(rejected)
        with self.assertRaises(ValueError):
            ResolvedRetentionPolicy.from_wire(
                {**value.to_wire(), "evidence_fail": {"mode": "discard", "seconds": None}}
            )
        with self.assertRaises(ValueError):
            ResolvedRetentionPolicy.from_wire(
                {**value.to_wire(), "record_detail": {"mode": "permanent", "seconds": None}}
            )

    def test_absent_group_is_history_tolerated_and_the_group_changes_identity(self) -> None:
        without = bundle(station(), capability=False)

        self.assertNotIn("retention", first_station_wire(without))
        self.assertEqual(configuration_from_wire(configuration_to_wire(without)), without)

        with_group = bundle(station(group()))
        other_group = bundle(
            station(replace(group(), evidence_pass=ResolvedRetentionRule("permanent")))
        )

        self.assertEqual(configuration_from_wire(configuration_to_wire(with_group)), with_group)
        self.assertIn("retention", first_station_wire(with_group))
        self.assertNotEqual(with_group.effective_sha256, without.effective_sha256)
        self.assertNotEqual(with_group.effective_sha256, other_group.effective_sha256)

    def test_the_group_and_its_capability_are_required_together(self) -> None:
        # 双向门禁：带组必须声明能力；声明能力必须每站带完整组。
        with self.assertRaises(ValueError):
            bundle(station(group()), capability=False)
        with self.assertRaises(ValueError):
            bundle(station(), capability=True)

        good = bundle(station(group()))
        self.assertEqual(configuration_from_wire(configuration_to_wire(good)), good)

        without_capability = configuration_to_wire(good)
        without_capability.pop("required_capabilities")
        without_capability["sha256"] = hashlib.sha256(
            canonical_json(
                {key: value for key, value in without_capability.items() if key != "sha256"}
            ).encode("utf-8")
        ).hexdigest()
        with self.assertRaises(ValueError):
            configuration_from_wire(without_capability)

        without_group = configuration_to_wire(bundle(station(), capability=False))
        without_group["required_capabilities"] = [RETENTION_STATION_POLICY_CAPABILITY]
        without_group["sha256"] = hashlib.sha256(
            canonical_json(
                {key: value for key, value in without_group.items() if key != "sha256"}
            ).encode("utf-8")
        ).hexdigest()
        with self.assertRaises(ValueError):
            configuration_from_wire(without_group)
