"""The closed set of permissions, and how a string becomes one.

§5.15 fixes the naming as three segments — `module.resource.action` — with the granularity
`view` / `edit` / `delete`, and requires that a permission exist in code as an enum member
rather than as a bare string. This module is that enum.

Why a single enum for the whole backend rather than one per module: a role is a set of
permissions drawn from anywhere (Q5/Q14), so validating a role's contents needs one closed set
to check against. Each later module adds its own members here — `device.camera.edit`,
`template.version.edit` — and the first segment says which module enforces it. That is a
shared vocabulary, not shared behavior: this file holds no rule about who gets what, and the
`authorize` call that consults it lives in each module's `usecases/` (§5.15).

Standard library only, and no import of anything above it: the permission set has to be
readable by the pure use cases without pulling SQLAlchemy or FastAPI in behind it.
"""

from __future__ import annotations

from enum import StrEnum

# §5.15's granularity, as a set so the shape can be asserted mechanically rather than by
# reading the members. 发布 is folded into `edit` on purpose (Q18) — a fourth verb here would
# be a decision to reread that, not a new member.
# `dataset.dataset.import` 是控制面规格明确登记的唯一专门动作；其他未登记的 import
# 仍然会被 `parse_permission` 拒绝。
ACTIONS = frozenset({"view", "edit", "delete", "import"})


class Permission(StrEnum):
    """Every permission the backend enforces. One closed set, checked against by role editing.

    A role naming a permission that is not a member of this enum is refused when it is
    written, which is what stops an unregistered string from becoming a permission nothing
    ever checks. When a module lands, it registers its members here and its registry rows in
    `auth_permission` land with the module's own `auth`-prefixed migration.
    """

    # Reading the account list, and reading one account. Not enough to change anything.
    USER_VIEW = "auth.user.view"
    # Creating, editing, deactivating and reactivating an account, and assigning its roles.
    # 停用 and 恢复 are `edit` rather than a verb of their own: they are the reversible state of
    # a mutable configuration object (§5.15), and the operator who may edit an account is the
    # one who may take it out of service.
    USER_EDIT = "auth.user.edit"
    # Deleting an account outright. Separate from `edit` because it is the irreversible one:
    # §5.15 makes 删除 an operation opened by its own permission, precisely so that an
    # administrator can be given day-to-day account management without it.
    USER_DELETE = "auth.user.delete"

    ROLE_VIEW = "auth.role.view"
    ROLE_EDIT = "auth.role.edit"
    ROLE_DELETE = "auth.role.delete"

    # Reading the device topology: hosts, backends, and what connection tests observed.
    INFERENCE_HOST_VIEW = "device.inference_host.view"
    # Creating and editing hosts, and the reversible 停用/恢复 of one — the same folding of
    # 停用 into `edit` the account permissions use (§5.15).
    INFERENCE_HOST_EDIT = "device.inference_host.edit"
    # Deleting a host outright. Separate, because it is the irreversible operation 停用 exists
    # to avoid.
    INFERENCE_HOST_DELETE = "device.inference_host.delete"

    INFERENCE_BACKEND_VIEW = "device.inference_backend.view"
    INFERENCE_BACKEND_EDIT = "device.inference_backend.edit"
    INFERENCE_BACKEND_DELETE = "device.inference_backend.delete"

    STATION_VIEW = "device.station.view"
    STATION_EDIT = "device.station.edit"
    STATION_DELETE = "device.station.delete"
    CAMERA_VIEW = "device.camera.view"
    CAMERA_EDIT = "device.camera.edit"
    CAMERA_DELETE = "device.camera.delete"
    CONNECTOR_VIEW = "device.connector.view"
    CONNECTOR_EDIT = "device.connector.edit"
    CONNECTOR_DELETE = "device.connector.delete"
    POINT_VIEW = "device.point.view"
    POINT_EDIT = "device.point.edit"
    POINT_DELETE = "device.point.delete"

    # 模板草稿的导入、读取和编辑；版本发布是后续独立的模板能力。
    TEMPLATE_DRAFT_VIEW = "template.draft.view"
    TEMPLATE_DRAFT_EDIT = "template.draft.edit"

    # 训练数据集的读取、标注编辑与逐视频导入。导入不隐式授予查看或编辑。
    DATASET_VIEW = "dataset.dataset.view"
    DATASET_EDIT = "dataset.dataset.edit"
    DATASET_IMPORT = "dataset.dataset.import"


class UnregisteredPermissionError(Exception):
    """A string that is not a member of `Permission`.

    Raised where a value crosses into the domain — editing a role, reading one back out of the
    database — rather than being tolerated as a permission that grants nothing. A role holding
    `auth.user.approve` would otherwise sit there looking granted and never be checked.
    """

    def __init__(self, value: str) -> None:
        super().__init__(f"unregistered permission: {value}")
        self.value = value


def parse_permission(value: str) -> Permission:
    """Return the `Permission` `value` names, or raise `UnregisteredPermissionError`.

    The one door a string comes through. Shape is not membership: `device.camera.edit` is
    correctly shaped and is not a permission until `device` registers it.
    """
    try:
        return Permission(value)
    except ValueError:
        raise UnregisteredPermissionError(value) from None
