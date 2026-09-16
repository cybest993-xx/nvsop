from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID

from factory_sop.configuration.usecases import configuration_for_host
from nvsop_contracts import Unverified

HOST_ID = UUID("019937d8-0d10-7b31-8d2d-4e60c8f4f101")
FOREIGN_HOST_ID = UUID("019937d8-0d10-7b31-8d2d-4e60c8f4f102")
STATION_ID = UUID("019937d8-0d10-7b31-8d2d-4e60c8f4f103")
BACKEND_ID = UUID("019937d8-0d10-7b31-8d2d-4e60c8f4f104")
CONNECTOR_ID = UUID("019937d8-0d10-7b31-8d2d-4e60c8f4f105")
FOREIGN_CONNECTOR_ID = UUID("019937d8-0d10-7b31-8d2d-4e60c8f4f106")
POINT_ID = UUID("019937d8-0d10-7b31-8d2d-4e60c8f4f107")


class Repo:
    def __init__(self) -> None:
        self.host = SimpleNamespace(
            id=HOST_ID,
            revision=4,
            configuration_revision=8,
            configuration_sha256=None,
        )
        self.cameras_present = True
        self.recorded_configurations: list[object] = []
        self.backend = SimpleNamespace(
            id=BACKEND_ID,
            host_id=HOST_ID,
            status=SimpleNamespace(value="active"),
            self_reported_model_ids=("reported-model", "reported-model-2"),
            revision=3,
        )
        self.station = SimpleNamespace(
            id=STATION_ID,
            code="S-A",
            name="Station A",
            status=SimpleNamespace(value="active"),
            revision=2,
            runtime_parameters_revision=1,
        )
        self.station.runtime_parameters_for = lambda defaults: SimpleNamespace(
            idle_timeout_seconds=defaults.idle_timeout_seconds,
            step_deadline_seconds=defaults.step_deadline_seconds,
            disposition_policy=defaults.disposition_policy,
        )
        content = b"{}"
        artifact_values = [
            ("actions.json", "application/json", content),
            ("vlm_prompts.txt", "text/plain", b"prompt\n"),
            ("template.json", "application/json", content),
        ]
        artifact_digests = {
            name: hashlib.sha256(artifact_content).hexdigest()
            for name, _media_type, artifact_content in artifact_values
        }
        manifest_content = json.dumps(
            {
                "artifacts": [
                    {
                        "byte_length": len(artifact_content),
                        "media_type": media_type,
                        "name": name,
                        "sha256": artifact_digests[name],
                    }
                    for name, media_type, artifact_content in artifact_values
                ],
                "format_version": 1,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        artifact_values.append(("manifest.json", "application/json", manifest_content))
        artifact_digests["manifest.json"] = hashlib.sha256(manifest_content).hexdigest()
        self.version = SimpleNamespace(
            id=UUID("019937d8-0d10-7b31-8d2d-4e60c8f4f108"),
            template_id=UUID("019937d8-0d10-7b31-8d2d-4e60c8f4f109"),
            source_draft_revision=1,
            runtime_defaults=SimpleNamespace(
                idle_timeout_seconds=10.0,
                step_deadline_seconds=2.0,
                disposition_policy="stop",
            ),
            sha256=artifact_digests["manifest.json"],
            artifacts=tuple(
                SimpleNamespace(
                    name=SimpleNamespace(value=name),
                    media_type=media_type,
                    content=artifact_content,
                    sha256=artifact_digests[name],
                )
                for name, media_type, artifact_content in artifact_values
            ),
        )

    def by_id(self, value: UUID) -> object | None:
        if value == HOST_ID:
            return self.host
        if value == STATION_ID:
            return self.station
        return None

    def page_of(
        self, *, page: int, page_size: int, host_id: UUID | None = None
    ) -> tuple[list[object], int]:
        return ([self.backend], 1) if host_id == HOST_ID else ([], 0)

    def for_host(self, value: UUID) -> list[object]:
        if value != HOST_ID or not self.cameras_present:
            return []
        return [
            SimpleNamespace(
                id=UUID("019937d8-0d10-7b31-8d2d-4e60c8f4f110"),
                name="Camera A",
                address="camera.local",
                main_stream_path="/main",
                sub_stream_path="/sub",
                credentials_configured=True,
                station_id=STATION_ID,
                backend_id=BACKEND_ID,
                host_id=HOST_ID,
                status=SimpleNamespace(value="active"),
                revision=1,
                media_path_mode=SimpleNamespace(value="passthrough"),
                recording_mode=SimpleNamespace(value="continuous"),
            )
        ]

    def for_station(self, value: UUID) -> list[object]:
        if value == STATION_ID:
            return [
                SimpleNamespace(
                    id=CONNECTOR_ID,
                    station_id=STATION_ID,
                    host_id=HOST_ID,
                    status=SimpleNamespace(value="active"),
                    name="PLC",
                    connector_type=SimpleNamespace(value="modbus"),
                    revision=8,
                    configuration=SimpleNamespace(address="plc.local", port=502),
                    capability=Unverified(),
                ),
                SimpleNamespace(
                    id=FOREIGN_CONNECTOR_ID,
                    station_id=STATION_ID,
                    host_id=FOREIGN_HOST_ID,
                    status=SimpleNamespace(value="active"),
                    name="foreign",
                    connector_type=SimpleNamespace(value="modbus"),
                    revision=1,
                    configuration=SimpleNamespace(address="foreign.local", port=502),
                    capability=Unverified(),
                ),
            ]
        return []

    def points_for_station(self, value: UUID) -> list[object]:
        return (
            [
                SimpleNamespace(
                    id=POINT_ID,
                    station_id=STATION_ID,
                    connector_id=CONNECTOR_ID,
                    revision=9,
                    status=SimpleNamespace(value="active"),
                    semantic_label="start",
                    identifier="DI-01",
                    direction=SimpleNamespace(value="input"),
                )
            ]
            if value == STATION_ID
            else []
        )

    def binding_by_station(self, value: UUID) -> object | None:
        return SimpleNamespace(
            desired_version_id=self.version.id,
            desired_config_revision=1,
        )

    def version_by_id(self, value: UUID) -> object | None:
        return self.version if value == self.version.id else None

    def template_by_id(self, value: UUID) -> object | None:
        return SimpleNamespace(station_id=STATION_ID) if value == self.version.template_id else None

    def next_configuration_revision(
        self, *, host_id: UUID, content_sha256: str, minimum_revision: int = 0
    ) -> int:
        assert host_id == HOST_ID
        if self.host.configuration_sha256 == content_sha256:
            return int(self.host.configuration_revision)
        self.host.configuration_revision = max(
            int(self.host.configuration_revision) + 1,
            minimum_revision + 1,
        )
        self.host.configuration_sha256 = content_sha256
        return int(self.host.configuration_revision)

    def record_configuration_assignments(self, bundle: object) -> None:
        self.recorded_configurations.append(bundle)


def test_configuration_revision_survives_removing_the_highest_revision_object() -> None:
    repo = Repo()
    first = configuration_for_host(
        host_id=HOST_ID,
        generated_at=datetime(2026, 9, 13, tzinfo=UTC),
        hosts=repo,  # type: ignore[arg-type]
        backends=repo,  # type: ignore[arg-type]
        stations=repo,  # type: ignore[arg-type]
        cameras=repo,  # type: ignore[arg-type]
        connectors=repo,  # type: ignore[arg-type]
        points=SimpleNamespace(for_station=repo.points_for_station),
        templates=repo,  # type: ignore[arg-type]
    )
    repo.cameras_present = False
    second = configuration_for_host(
        host_id=HOST_ID,
        generated_at=datetime(2026, 9, 13, 0, 0, 1, tzinfo=UTC),
        hosts=repo,  # type: ignore[arg-type]
        backends=repo,  # type: ignore[arg-type]
        stations=repo,  # type: ignore[arg-type]
        cameras=repo,  # type: ignore[arg-type]
        connectors=repo,  # type: ignore[arg-type]
        points=SimpleNamespace(for_station=repo.points_for_station),
        templates=repo,  # type: ignore[arg-type]
    )

    assert second.config_revision > first.config_revision
    assert second.stations == ()


def test_configuration_for_host_excludes_foreign_connector_and_emits_effective_values() -> None:
    repo = Repo()
    bundle = configuration_for_host(
        host_id=HOST_ID,
        generated_at=datetime(2026, 9, 13, tzinfo=UTC),
        hosts=repo,  # type: ignore[arg-type]
        backends=repo,  # type: ignore[arg-type]
        stations=repo,  # type: ignore[arg-type]
        cameras=repo,  # type: ignore[arg-type]
        connectors=repo,  # type: ignore[arg-type]
        points=SimpleNamespace(for_station=repo.points_for_station),
        templates=repo,  # type: ignore[arg-type]
    )

    assert len(bundle.stations) == 1
    station = bundle.stations[0]
    assert station.backend_id == str(BACKEND_ID)
    assert station.model_ids == ("reported-model", "reported-model-2")
    assert bundle.config_revision == 10
    assert repo.recorded_configurations == [bundle]
    assert [connector.name for connector in station.connectors] == ["PLC"]
    assert [camera.camera_id for camera in station.cameras] == [
        "019937d8-0d10-7b31-8d2d-4e60c8f4f110"
    ]
    assert station.runtime_parameters.idle_timeout_seconds == 10.0
    assert station.template is not None
    assert [artifact.name for artifact in station.template.artifacts] == [
        "actions.json",
        "vlm_prompts.txt",
        "template.json",
        "manifest.json",
    ]
