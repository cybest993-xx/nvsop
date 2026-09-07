"""点位配置、连接器能力与绑定前校验的用例契约。"""

from __future__ import annotations

from dataclasses import replace

import pytest
from auth_fakes import caller_holding
from device_fakes import (
    FAKE_NOW,
    FakeConnectors,
    FakeInferenceHosts,
    FakeInferenceStations,
    FakePoints,
)

from factory_sop.auth.api import Permission
from factory_sop.device.model import (
    BindingReason,
    BindingReasonCode,
    BindingValidation,
    DeviceStatus,
    Point,
    PointDirection,
)
from factory_sop.device.usecases.connectors import update_connector_capability
from factory_sop.device.usecases.points import (
    create_point,
    delete_point,
    edit_point,
    list_points,
    point_by_identifier,
    set_point_status,
    validate_binding,
)
from nvsop_contracts import (
    EdgePreservation,
    Measured,
    PointRole,
    Polled,
    Sequencing,
    TimestampSource,
    Unverified,
)

CALLER = caller_holding(
    Permission.POINT_VIEW,
    Permission.POINT_EDIT,
    Permission.POINT_DELETE,
    Permission.CONNECTOR_EDIT,
)


def measured(
    *,
    delay: float = 0.08,
    sequencing: Sequencing = Sequencing.SEQUENCED,
    edges: EdgePreservation = EdgePreservation.PRESERVED,
) -> Measured:
    return Measured(
        delivery=Polled(interval=0.05),
        max_delivery_delay=delay,
        sequencing=sequencing,
        edges=edges,
        timestamps=TimestampSource.HOST_RECEIPT,
    )


def topology() -> tuple[FakeInferenceStations, FakeConnectors, FakePoints]:
    stations = FakeInferenceStations()
    hosts = FakeInferenceHosts()
    connectors = FakeConnectors()
    points = FakePoints()
    station = stations.register(code="A-001", name="装配一号工位")
    host = hosts.register(name="推理机-1")
    connectors.register(station_id=station.id, host_id=host.id)
    return stations, connectors, points


def test_authorized_point_crud_status_and_listing_preserve_the_whole_record() -> None:
    stations, connectors, points = topology()
    station = next(iter(stations.rows.values()))
    connector = next(iter(connectors.rows.values()))

    created = create_point(
        station_id=station.id,
        connector_id=connector.id,
        direction=PointDirection.INPUT,
        identifier="DI-01",
        semantic_label="工件到位",
        caller=CALLER,
        now=FAKE_NOW,
        stations=stations,
        connectors=connectors,
        points=points,
    )

    assert created == Point(
        id=created.id,
        station_id=station.id,
        connector_id=connector.id,
        direction=PointDirection.INPUT,
        identifier="DI-01",
        semantic_label="工件到位",
        status=DeviceStatus.ACTIVE,
        revision=1,
        created_by=CALLER.user.id,
        updated_by=CALLER.user.id,
        created_at=FAKE_NOW,
        updated_at=FAKE_NOW,
    )
    edited = edit_point(
        point_id=created.id,
        station_id=station.id,
        connector_id=connector.id,
        direction=PointDirection.INPUT,
        identifier="DI-001",
        semantic_label="上料工件到位",
        expected_revision=1,
        caller=CALLER,
        now=FAKE_NOW,
        stations=stations,
        connectors=connectors,
        points=points,
    )
    assert edited == replace(
        created,
        identifier="DI-001",
        semantic_label="上料工件到位",
        revision=2,
        updated_at=FAKE_NOW,
    )
    deactivated = set_point_status(
        point_id=created.id,
        requested_status=DeviceStatus.DEACTIVATED,
        expected_revision=2,
        caller=CALLER,
        now=FAKE_NOW,
        points=points,
    )
    assert deactivated.status is DeviceStatus.DEACTIVATED
    assert deactivated.revision == 3
    restored = set_point_status(
        point_id=created.id,
        requested_status=DeviceStatus.ACTIVE,
        expected_revision=3,
        caller=CALLER,
        now=FAKE_NOW,
        points=points,
    )
    assert restored == replace(edited, revision=4, status=DeviceStatus.ACTIVE)
    assert point_by_identifier(point_id=created.id, caller=CALLER, points=points) == restored
    assert list_points(
        caller=CALLER,
        points=points,
        page=1,
        page_size=10,
        station_id=station.id,
        connector_id=connector.id,
    ) == ([restored], 1)

    delete_point(
        point_id=created.id,
        expected_revision=4,
        caller=CALLER,
        points=points,
    )
    assert points.by_id(created.id) is None


def test_connector_capability_starts_unverified_and_updates_under_connector_authority() -> None:
    _, connectors, _ = topology()
    connector = next(iter(connectors.rows.values()))
    capability = measured()

    assert connector.capability == Unverified()
    updated = update_connector_capability(
        connector_id=connector.id,
        capability=capability,
        expected_revision=1,
        caller=CALLER,
        now=FAKE_NOW,
        connectors=connectors,
    )

    assert updated == replace(
        connector,
        capability=capability,
        revision=2,
        updated_by=CALLER.user.id,
        updated_at=FAKE_NOW,
    )


def test_action_source_needs_no_point_or_connector_and_empty_station_remains_valid() -> None:
    stations = FakeInferenceStations()
    station = stations.register(code="EMPTY", name="无外部信号工位")

    result = validate_binding(
        station_id=station.id,
        point_id=None,
        role=PointRole.START_SIGNAL,
        budget=0.2,
        caller=CALLER,
        stations=stations,
        points=FakePoints(),
        connectors=FakeConnectors(),
    )

    assert result == BindingValidation(accepted=True, reasons=())


def test_safety_output_requires_an_output_point_but_does_not_require_a_lookup_to_say_so() -> None:
    stations = FakeInferenceStations()
    station = stations.register(code="EMPTY", name="无外部信号工位")

    result = validate_binding(
        station_id=station.id,
        point_id=None,
        role=PointRole.SAFETY_OUTPUT,
        budget=0.2,
        caller=CALLER,
        stations=stations,
        points=FakePoints(),
        connectors=FakeConnectors(),
    )

    assert result == BindingValidation(
        accepted=False,
        reasons=(
            BindingReason(
                code=BindingReasonCode.POINT_REQUIRED,
                field="point_id",
                message="安全输出必须选择一个输出点位",
            ),
        ),
    )


def test_wrong_direction_cross_station_and_inactive_records_are_concrete_refusals() -> None:
    stations, connectors, points = topology()
    station = next(iter(stations.rows.values()))
    other_station = stations.register(code="B-001", name="装配二号工位")
    connector = next(iter(connectors.rows.values()))
    point = points.register(
        station_id=station.id,
        connector_id=connector.id,
        direction=PointDirection.INPUT,
        identifier="DI-01",
        semantic_label="工件到位",
        status=DeviceStatus.DEACTIVATED,
    )

    cross_station = validate_binding(
        station_id=other_station.id,
        point_id=point.id,
        role=PointRole.START_SIGNAL,
        budget=0.2,
        caller=CALLER,
        stations=stations,
        points=points,
        connectors=connectors,
    )
    assert cross_station.reasons[0] == BindingReason(
        code=BindingReasonCode.POINT_STATION_MISMATCH,
        field="station_id",
        message="所选点位属于其他工位",
    )

    points.rows[point.id] = replace(point, status=DeviceStatus.ACTIVE)
    wrong_direction = validate_binding(
        station_id=station.id,
        point_id=point.id,
        role=PointRole.SAFETY_OUTPUT,
        budget=0.2,
        caller=CALLER,
        stations=stations,
        points=points,
        connectors=connectors,
    )
    assert wrong_direction.reasons == (
        BindingReason(
            code=BindingReasonCode.WRONG_DIRECTION,
            field="direction",
            message="安全输出需要输出点位，当前点位为输入",
        ),
    )

    points.rows[point.id] = point
    inactive_point = validate_binding(
        station_id=station.id,
        point_id=point.id,
        role=PointRole.START_SIGNAL,
        budget=0.2,
        caller=CALLER,
        stations=stations,
        points=points,
        connectors=connectors,
    )
    assert inactive_point.reasons == (
        BindingReason(
            code=BindingReasonCode.POINT_DEACTIVATED,
            field="status",
            message="所选点位已停用",
        ),
    )

    points.rows[point.id] = replace(point, status=DeviceStatus.ACTIVE)
    connectors.rows[connector.id] = replace(connector, status=DeviceStatus.DEACTIVATED)
    inactive_connector = validate_binding(
        station_id=station.id,
        point_id=point.id,
        role=PointRole.START_SIGNAL,
        budget=0.2,
        caller=CALLER,
        stations=stations,
        points=points,
        connectors=connectors,
    )
    assert inactive_connector.reasons == (
        BindingReason(
            code=BindingReasonCode.CONNECTOR_DEACTIVATED,
            field="connector_id",
            message="点位所属连接器已停用",
        ),
    )


def test_unverified_and_every_insufficient_capability_reason_keep_shared_order() -> None:
    stations, connectors, points = topology()
    station = next(iter(stations.rows.values()))
    connector = next(iter(connectors.rows.values()))
    point = points.register(
        station_id=station.id,
        connector_id=connector.id,
        direction=PointDirection.INPUT,
        identifier="DI-01",
        semantic_label="工件到位",
    )

    unverified = validate_binding(
        station_id=station.id,
        point_id=point.id,
        role=PointRole.START_SIGNAL,
        budget=0.2,
        caller=CALLER,
        stations=stations,
        points=points,
        connectors=connectors,
    )
    assert [reason.code for reason in unverified.reasons] == [
        BindingReasonCode.CAPABILITY_UNVERIFIED
    ]
    assert unverified.reasons[0].field == "capability.verification"

    connectors.rows[connector.id] = replace(
        connector,
        capability=measured(
            delay=0.3,
            sequencing=Sequencing.UNSEQUENCED,
            edges=EdgePreservation.MAY_DROP,
        ),
    )
    insufficient = validate_binding(
        station_id=station.id,
        point_id=point.id,
        role=PointRole.ORDERED_STEP,
        budget=0.2,
        caller=CALLER,
        stations=stations,
        points=points,
        connectors=connectors,
    )
    assert [reason.code for reason in insufficient.reasons] == [
        BindingReasonCode.MAY_DROP_EDGES,
        BindingReasonCode.NOT_SEQUENCED,
        BindingReasonCode.DELIVERY_TOO_SLOW,
    ]
    assert insufficient.reasons[-1] == BindingReason(
        code=BindingReasonCode.DELIVERY_TOO_SLOW,
        field="capability.max_delivery_delay_seconds",
        message="连接器最大投递延迟 0.3 秒超出角色预算 0.2 秒",
    )


@pytest.mark.parametrize(
    ("role", "direction"),
    [
        (PointRole.START_SIGNAL, PointDirection.INPUT),
        (PointRole.END_SIGNAL, PointDirection.INPUT),
        (PointRole.SAFETY_OUTPUT, PointDirection.OUTPUT),
    ],
)
def test_valid_measured_bindings_pass_for_required_roles(
    role: PointRole, direction: PointDirection
) -> None:
    stations, connectors, points = topology()
    station = next(iter(stations.rows.values()))
    connector = next(iter(connectors.rows.values()))
    connectors.rows[connector.id] = replace(connector, capability=measured())
    point = points.register(
        station_id=station.id,
        connector_id=connector.id,
        direction=direction,
        identifier=f"{direction.value}-01",
        semantic_label=f"{role.value}-signal",
    )

    result = validate_binding(
        station_id=station.id,
        point_id=point.id,
        role=role,
        budget=0.2,
        caller=CALLER,
        stations=stations,
        points=points,
        connectors=connectors,
    )

    assert result == BindingValidation(accepted=True, reasons=())
