from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from auth_fakes import caller_holding
from device_fakes import (
    FakeCameras,
    FakeConnectors,
    FakeInferenceBackends,
    FakeInferenceHosts,
    FakeInferenceStations,
    FakePoints,
)

from factory_sop.auth.api import Permission
from factory_sop.device.api import DeviceHostGateway
from factory_sop.device.model import (
    Camera,
    DeviceStatus,
    InferenceBackend,
    InferenceHost,
    InferenceHostIdentity,
    RuntimeParameterMode,
    Station,
    StationRuntimeParameters,
)
from factory_sop.device.usecases.template_binding import RepositoryDeviceTemplateBindingGateway
from factory_sop.identifiers import new_id
from factory_sop.template.artifacts import build_template_artifacts
from factory_sop.template.errors import TemplateRefusalCode, TemplateRefusedError
from factory_sop.template.model import (
    OrderingMode,
    SopTemplate,
    TemplateBindingStatus,
    TemplateBoundaryDraft,
    TemplateConfigurationReport,
    TemplateReportRejectionCode,
    TemplateRuntimeDefaults,
    TemplateSignal,
    TemplateSignalKind,
    TemplateStationBinding,
    TemplateStep,
    TemplateVersion,
)
from factory_sop.template.usecases.bindings import (
    bind_template_version,
    read_station_configuration,
    report_template_configuration,
    update_station_runtime_parameters,
)

NOW = datetime(2026, 9, 8, 1, 0, tzinfo=UTC)
CALLER = caller_holding(Permission.STATION_EDIT, Permission.STATION_VIEW)


@dataclass
class TemplateStore:
    templates: dict[UUID, SopTemplate] = field(default_factory=dict)
    versions: dict[UUID, TemplateVersion] = field(default_factory=dict)
    bindings: dict[UUID, TemplateStationBinding] = field(default_factory=dict)
    reports: dict[tuple[UUID, UUID], TemplateConfigurationReport] = field(default_factory=dict)

    def template_by_id(self, template_id: UUID) -> SopTemplate | None:
        return self.templates.get(template_id)

    def version_by_id(self, version_id: UUID) -> TemplateVersion | None:
        return self.versions.get(version_id)

    def binding_by_station(self, station_id: UUID) -> TemplateStationBinding | None:
        return self.bindings.get(station_id)

    def save_binding(
        self, binding: TemplateStationBinding, *, expected_revision: int | None = None
    ) -> None:
        current = self.bindings.get(binding.station_id)
        if expected_revision is None:
            if current is not None:
                raise AssertionError("binding already exists")
        elif current is None or current.revision != expected_revision:
            raise AssertionError("stale binding")
        self.bindings[binding.station_id] = binding

    def report_by_backend(
        self, station_id: UUID, backend_id: UUID
    ) -> TemplateConfigurationReport | None:
        return self.reports.get((station_id, backend_id))

    def reports_for_station(self, station_id: UUID) -> list[TemplateConfigurationReport]:
        return [report for (sid, _), report in self.reports.items() if sid == station_id]

    def save_report(
        self,
        report: TemplateConfigurationReport,
        *,
        expected: TemplateConfigurationReport | None = None,
    ) -> None:
        key = (report.station_id, report.backend_id)
        current = self.reports.get(key)
        if expected is None:
            if current is not None:
                raise TemplateRefusedError(TemplateRefusalCode.REPORT_CONFLICT)
        elif current != expected:
            raise TemplateRefusedError(TemplateRefusalCode.REPORT_CONFLICT)
        self.reports[key] = report


@dataclass
class HostGateway(DeviceHostGateway):
    allowed: bool = True
    authenticated: int = 0

    def authenticate(self, *, host: InferenceHostIdentity, now: datetime) -> None:
        del host, now
        self.authenticated += 1

    def owns_station(self, *, host_id: UUID, station_id: UUID) -> bool:
        del host_id, station_id
        return self.allowed

    def owns_station_backend(self, *, host_id: UUID, station_id: UUID, backend_id: UUID) -> bool:
        del host_id, station_id, backend_id
        return self.allowed


def build_fixture() -> tuple[
    TemplateStore,
    RepositoryDeviceTemplateBindingGateway,
    Station,
    InferenceHost,
    InferenceBackend,
    Camera,
    TemplateVersion,
]:
    stations = FakeInferenceStations()
    hosts = FakeInferenceHosts()
    backends = FakeInferenceBackends()
    cameras = FakeCameras()
    connectors = FakeConnectors()
    points = FakePoints()
    station = stations.register(code="A-001", name="装配一号工位")
    host = hosts.register(name="推理机一")
    backend = backends.register(host_id=host.id, base_url="http://10.0.0.1:8000")
    camera = cameras.register(station_id=station.id, host_id=host.id, backend_id=backend.id)

    actor = CALLER.user.id
    template = SopTemplate(
        id=new_id(),
        station_id=station.id,
        station_code=station.code,
        station_name=station.name,
        created_by=actor,
        updated_by=actor,
        created_at=NOW,
        updated_at=NOW,
    )
    steps = (TemplateStep(number=1, name="取料", description="(1)取料"),)
    defaults = TemplateRuntimeDefaults(30.0, 90.0, "record")
    boundary = TemplateBoundaryDraft(
        start_signal=TemplateSignal(TemplateSignalKind.ACTION, 1),
        end_signals=(),
    )
    artifacts = build_template_artifacts(
        steps=steps,
        ordering=OrderingMode.STRICT,
        boundary=boundary,
        runtime_defaults=defaults,
    )
    version = TemplateVersion(
        id=new_id(),
        template_id=template.id,
        source_import_id=new_id(),
        source_draft_id=new_id(),
        source_draft_revision=1,
        steps=steps,
        ordering=OrderingMode.STRICT,
        boundary=boundary,
        runtime_defaults=defaults,
        artifacts=artifacts.artifacts,
        sha256=artifacts.sha256,
        published_by=actor,
        published_at=NOW,
    )
    templates_store = TemplateStore(
        templates={template.id: template}, versions={version.id: version}
    )
    gateway = RepositoryDeviceTemplateBindingGateway(
        stations=stations,
        hosts=hosts,
        backends=backends,
        cameras=cameras,
        connectors=connectors,
        points=points,
    )
    return templates_store, gateway, station, host, backend, camera, version


def test_bind_persists_desired_but_not_reported_and_resolves_template_defaults() -> None:
    store, device, station, _host, backend, camera, version = build_fixture()

    configuration = bind_template_version(
        station_id=station.id,
        version_id=version.id,
        runtime_mode=None,
        runtime_overrides=None,
        expected_station_revision=station.revision,
        caller=CALLER,
        now=NOW,
        templates=store,
        device=device,
    )

    assert configuration.binding is not None
    assert configuration.binding.desired_version_id == version.id
    assert configuration.binding.desired_sha256 == version.sha256
    assert configuration.binding.desired_config_revision == 2
    assert configuration.status is TemplateBindingStatus.WAITING
    assert configuration.runtime.effective == StationRuntimeParameters(30.0, 90.0, "record")
    assert store.reports == {}
    assert device.participating_backend_ids(station.id) == (backend.id,)
    assert camera.station_id == station.id


def test_custom_parameters_survive_a_rebind_and_are_not_merged_with_new_defaults() -> None:
    store, device, station, _host, _backend, _camera, first = build_fixture()
    first_configuration = bind_template_version(
        station_id=station.id,
        version_id=first.id,
        runtime_mode=None,
        runtime_overrides=None,
        expected_station_revision=station.revision,
        caller=CALLER,
        now=NOW,
        templates=store,
        device=device,
    )
    custom = StationRuntimeParameters(12.0, 44.0, "hold")
    custom_configuration = update_station_runtime_parameters(
        station_id=station.id,
        mode=RuntimeParameterMode.CUSTOM,
        overrides=custom,
        expected_station_revision=first_configuration.station_revision,
        caller=CALLER,
        now=NOW + timedelta(minutes=1),
        templates=store,
        device=device,
    )

    second_defaults = TemplateRuntimeDefaults(60.0, 120.0, "discard")
    second_boundary = first.boundary
    second_artifacts = build_template_artifacts(
        steps=first.steps,
        ordering=first.ordering,
        boundary=second_boundary,
        runtime_defaults=second_defaults,
    )
    second = replace(
        first,
        id=new_id(),
        runtime_defaults=second_defaults,
        artifacts=second_artifacts.artifacts,
        sha256=second_artifacts.sha256,
    )
    store.versions[second.id] = second
    rebound = bind_template_version(
        station_id=station.id,
        version_id=second.id,
        runtime_mode=None,
        runtime_overrides=None,
        expected_station_revision=custom_configuration.station_revision,
        caller=CALLER,
        now=NOW + timedelta(minutes=2),
        templates=store,
        device=device,
    )

    assert rebound.runtime.mode is RuntimeParameterMode.CUSTOM
    assert rebound.runtime.overrides == custom
    assert rebound.runtime.defaults == StationRuntimeParameters(60.0, 120.0, "discard")
    assert rebound.runtime.effective == custom
    assert rebound.binding is not None
    assert rebound.binding.desired_version_id == second.id


def test_report_acceptance_and_digest_rejection_keep_last_valid_confirmation() -> None:
    store, device, station, host, backend, _camera, version = build_fixture()
    bound = bind_template_version(
        station_id=station.id,
        version_id=version.id,
        runtime_mode=None,
        runtime_overrides=None,
        expected_station_revision=station.revision,
        caller=CALLER,
        now=NOW,
        templates=store,
        device=device,
    )
    assert bound.binding is not None
    host_gateway = HostGateway()
    identity = InferenceHostIdentity(host_id=host.id)
    accepted = report_template_configuration(
        host=identity,
        station_id=station.id,
        backend_id=backend.id,
        reported_version_id=version.id,
        reported_sha256=version.sha256,
        reported_config_revision=bound.binding.desired_config_revision,
        now=NOW + timedelta(minutes=1),
        templates=store,
        host_gateway=host_gateway,
    )
    rejected = report_template_configuration(
        host=identity,
        station_id=station.id,
        backend_id=backend.id,
        reported_version_id=version.id,
        reported_sha256="0" * 64,
        reported_config_revision=bound.binding.desired_config_revision,
        now=NOW + timedelta(minutes=2),
        templates=store,
        host_gateway=host_gateway,
    )

    assert accepted.accepted
    assert rejected.rejection_code is TemplateReportRejectionCode.DIGEST_MISMATCH
    assert rejected.report is not None
    assert rejected.report.reported_sha256 == version.sha256
    assert rejected.report.last_rejection_code is TemplateReportRejectionCode.DIGEST_MISMATCH
    read = read_station_configuration(
        station_id=station.id,
        caller=CALLER,
        templates=store,
        device=device,
    )
    assert read.status is TemplateBindingStatus.REJECTED
    assert read.backends[0].reported_sha256 == version.sha256


def test_first_old_confirmation_is_waiting_not_confirmed() -> None:
    store, device, station, host, backend, _camera, version = build_fixture()
    bound = bind_template_version(
        station_id=station.id,
        version_id=version.id,
        runtime_mode=None,
        runtime_overrides=None,
        expected_station_revision=station.revision,
        caller=CALLER,
        now=NOW,
        templates=store,
        device=device,
    )
    assert bound.binding is not None
    old = report_template_configuration(
        host=InferenceHostIdentity(host_id=host.id),
        station_id=station.id,
        backend_id=backend.id,
        reported_version_id=version.id,
        reported_sha256=version.sha256,
        reported_config_revision=bound.binding.desired_config_revision - 1,
        now=NOW + timedelta(minutes=1),
        templates=store,
        host_gateway=HostGateway(),
    )

    assert old.accepted
    read = read_station_configuration(
        station_id=station.id,
        caller=CALLER,
        templates=store,
        device=device,
    )
    assert read.status is TemplateBindingStatus.WAITING
    assert read.backends[0].reported_config_revision == bound.binding.desired_config_revision - 1


def test_old_confirmation_does_not_rollback_a_newer_report_and_future_revision_is_rejected() -> (
    None
):
    store, device, station, host, backend, _camera, version = build_fixture()
    bound = bind_template_version(
        station_id=station.id,
        version_id=version.id,
        runtime_mode=None,
        runtime_overrides=None,
        expected_station_revision=station.revision,
        caller=CALLER,
        now=NOW,
        templates=store,
        device=device,
    )
    assert bound.binding is not None
    host_gateway = HostGateway()
    identity = InferenceHostIdentity(host_id=host.id)
    current = bound.binding.desired_config_revision
    first = report_template_configuration(
        host=identity,
        station_id=station.id,
        backend_id=backend.id,
        reported_version_id=version.id,
        reported_sha256=version.sha256,
        reported_config_revision=current,
        now=NOW + timedelta(minutes=1),
        templates=store,
        host_gateway=host_gateway,
    )
    future = report_template_configuration(
        host=identity,
        station_id=station.id,
        backend_id=backend.id,
        reported_version_id=version.id,
        reported_sha256=version.sha256,
        reported_config_revision=current + 1,
        now=NOW + timedelta(minutes=2),
        templates=store,
        host_gateway=host_gateway,
    )
    late = report_template_configuration(
        host=identity,
        station_id=station.id,
        backend_id=backend.id,
        reported_version_id=version.id,
        reported_sha256=version.sha256,
        reported_config_revision=current - 1,
        now=NOW + timedelta(minutes=3),
        templates=store,
        host_gateway=host_gateway,
    )

    assert first.accepted
    assert future.rejection_code is TemplateReportRejectionCode.FUTURE_REVISION
    assert late.rejection_code is TemplateReportRejectionCode.STALE_REVISION
    report = store.report_by_backend(station.id, backend.id)
    assert report is not None
    assert report.reported_config_revision == current
    assert report.last_rejection_code is TemplateReportRejectionCode.STALE_REVISION


def test_binding_rechecks_station_revision_and_refuses_stale_precheck() -> None:
    store, device, station, _host, _backend, _camera, version = build_fixture()
    first = bind_template_version(
        station_id=station.id,
        version_id=version.id,
        runtime_mode=None,
        runtime_overrides=None,
        expected_station_revision=station.revision,
        caller=CALLER,
        now=NOW,
        templates=store,
        device=device,
    )

    with pytest.raises(TemplateRefusedError) as raised:
        bind_template_version(
            station_id=station.id,
            version_id=version.id,
            runtime_mode=None,
            runtime_overrides=None,
            expected_station_revision=station.revision,
            caller=CALLER,
            now=NOW + timedelta(minutes=1),
            templates=store,
            device=device,
        )

    assert "STALE_REVISION" in str(raised.value)
    assert first.binding is not None
    assert store.binding_by_station(station.id) == first.binding


def _bind_for_report(
    store: TemplateStore,
    device: RepositoryDeviceTemplateBindingGateway,
    station: Station,
    version: TemplateVersion,
) -> TemplateStationBinding:
    configuration = bind_template_version(
        station_id=station.id,
        version_id=version.id,
        runtime_mode=None,
        runtime_overrides=None,
        expected_station_revision=station.revision,
        caller=CALLER,
        now=NOW,
        templates=store,
        device=device,
    )
    assert configuration.binding is not None
    return configuration.binding


def test_repeated_confirmation_is_idempotent_and_keeps_the_first_reported_at() -> None:
    store, device, station, host, backend, _camera, version = build_fixture()
    binding = _bind_for_report(store, device, station, version)
    gateway = HostGateway()
    identity = InferenceHostIdentity(host_id=host.id)
    first = report_template_configuration(
        host=identity,
        station_id=station.id,
        backend_id=backend.id,
        reported_version_id=version.id,
        reported_sha256=version.sha256,
        reported_config_revision=binding.desired_config_revision,
        now=NOW + timedelta(minutes=1),
        templates=store,
        host_gateway=gateway,
    )
    replay = report_template_configuration(
        host=identity,
        station_id=station.id,
        backend_id=backend.id,
        reported_version_id=version.id,
        reported_sha256=version.sha256,
        reported_config_revision=binding.desired_config_revision,
        now=NOW + timedelta(minutes=2),
        templates=store,
        host_gateway=gateway,
    )

    assert first.accepted
    assert replay.accepted
    assert replay.report == first.report
    assert replay.report is not None
    assert replay.report.reported_at == NOW + timedelta(minutes=1)
    assert gateway.authenticated == 2


def test_same_revision_with_a_different_version_is_rejected_without_overwriting_fact() -> None:
    store, device, station, host, backend, _camera, version = build_fixture()
    binding = _bind_for_report(store, device, station, version)
    alternate = replace(version, id=new_id())
    store.versions[alternate.id] = alternate
    gateway = HostGateway()
    identity = InferenceHostIdentity(host_id=host.id)
    accepted = report_template_configuration(
        host=identity,
        station_id=station.id,
        backend_id=backend.id,
        reported_version_id=version.id,
        reported_sha256=version.sha256,
        reported_config_revision=binding.desired_config_revision,
        now=NOW + timedelta(minutes=1),
        templates=store,
        host_gateway=gateway,
    )
    conflict = report_template_configuration(
        host=identity,
        station_id=station.id,
        backend_id=backend.id,
        reported_version_id=alternate.id,
        reported_sha256=alternate.sha256,
        reported_config_revision=binding.desired_config_revision,
        now=NOW + timedelta(minutes=2),
        templates=store,
        host_gateway=gateway,
    )

    assert accepted.accepted
    assert conflict.rejection_code is TemplateReportRejectionCode.CONFLICTING_CONFIRMATION
    report = store.report_by_backend(station.id, backend.id)
    assert report is not None
    assert report.reported_version_id == version.id
    assert report.last_rejection_code is TemplateReportRejectionCode.CONFLICTING_CONFIRMATION


def test_unknown_version_is_recorded_as_a_rejection_without_a_reported_fact() -> None:
    store, device, station, host, backend, _camera, version = build_fixture()
    binding = _bind_for_report(store, device, station, version)

    result = report_template_configuration(
        host=InferenceHostIdentity(host_id=host.id),
        station_id=station.id,
        backend_id=backend.id,
        reported_version_id=new_id(),
        reported_sha256="0" * 64,
        reported_config_revision=binding.desired_config_revision,
        now=NOW + timedelta(minutes=1),
        templates=store,
        host_gateway=HostGateway(),
    )

    assert result.rejection_code is TemplateReportRejectionCode.UNKNOWN_VERSION
    assert result.report is not None
    assert result.report.reported_version_id is None
    assert result.report.last_rejection_code is TemplateReportRejectionCode.UNKNOWN_VERSION


def test_version_from_another_station_is_rejected() -> None:
    store, device, station, host, backend, _camera, version = build_fixture()
    binding = _bind_for_report(store, device, station, version)
    foreign_template = replace(
        next(iter(store.templates.values())),
        id=new_id(),
        station_id=new_id(),
    )
    foreign_version = replace(version, id=new_id(), template_id=foreign_template.id)
    store.templates[foreign_template.id] = foreign_template
    store.versions[foreign_version.id] = foreign_version

    result = report_template_configuration(
        host=InferenceHostIdentity(host_id=host.id),
        station_id=station.id,
        backend_id=backend.id,
        reported_version_id=foreign_version.id,
        reported_sha256=foreign_version.sha256,
        reported_config_revision=binding.desired_config_revision,
        now=NOW + timedelta(minutes=1),
        templates=store,
        host_gateway=HostGateway(),
    )

    assert result.rejection_code is TemplateReportRejectionCode.VERSION_STATION_MISMATCH


def test_current_revision_with_a_non_desired_version_is_rejected() -> None:
    store, device, station, host, backend, _camera, version = build_fixture()
    binding = _bind_for_report(store, device, station, version)
    alternate = replace(version, id=new_id())
    store.versions[alternate.id] = alternate

    result = report_template_configuration(
        host=InferenceHostIdentity(host_id=host.id),
        station_id=station.id,
        backend_id=backend.id,
        reported_version_id=alternate.id,
        reported_sha256=alternate.sha256,
        reported_config_revision=binding.desired_config_revision,
        now=NOW + timedelta(minutes=1),
        templates=store,
        host_gateway=HostGateway(),
    )

    assert result.rejection_code is TemplateReportRejectionCode.VERSION_ID_MISMATCH


def test_host_ownership_and_station_binding_are_checked_before_report_persistence() -> None:
    store, device, station, host, backend, _camera, version = build_fixture()
    binding = _bind_for_report(store, device, station, version)
    denied_gateway = HostGateway(allowed=False)

    with pytest.raises(TemplateRefusedError) as denied:
        report_template_configuration(
            host=InferenceHostIdentity(host_id=host.id),
            station_id=station.id,
            backend_id=backend.id,
            reported_version_id=version.id,
            reported_sha256=version.sha256,
            reported_config_revision=binding.desired_config_revision,
            now=NOW + timedelta(minutes=1),
            templates=store,
            host_gateway=denied_gateway,
        )
    assert denied.value.code is TemplateRefusalCode.REPORT_HOST_NOT_ALLOWED
    assert store.reports == {}

    (
        unbound_store,
        _unbound_device,
        unbound_station,
        unbound_host,
        unbound_backend,
        _camera,
        unbound_version,
    ) = build_fixture()
    with pytest.raises(TemplateRefusedError) as unbound:
        report_template_configuration(
            host=InferenceHostIdentity(host_id=unbound_host.id),
            station_id=unbound_station.id,
            backend_id=unbound_backend.id,
            reported_version_id=unbound_version.id,
            reported_sha256=unbound_version.sha256,
            reported_config_revision=1,
            now=NOW + timedelta(minutes=1),
            templates=unbound_store,
            host_gateway=HostGateway(),
        )
    assert unbound.value.code is TemplateRefusalCode.REPORT_STATION_UNBOUND


def test_deactivated_backend_is_not_an_owned_reporting_target() -> None:
    _store, device, station, host, backend, _camera, _version = build_fixture()

    assert device.owns_station_backend(
        host_id=host.id,
        station_id=station.id,
        backend_id=backend.id,
    )
    device._backends.save(
        replace(backend, status=DeviceStatus.DEACTIVATED, revision=2),
        expected_revision=backend.revision,
    )

    assert not device.owns_station_backend(
        host_id=host.id,
        station_id=station.id,
        backend_id=backend.id,
    )
