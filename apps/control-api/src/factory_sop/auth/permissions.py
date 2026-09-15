"""权限的封闭集合，以及字符串如何转换为权限。

§5.15 规定名称由三个片段组成，即 `module.resource.action`，动作粒度为
`view` / `edit` / `delete`，并要求权限必须作为枚举成员存在，不能只使用裸字符串。
本模块定义这个枚举。

整个后端共用一个枚举，而不是每个模块各自定义：角色可以包含任意模块的权限（Q5/Q14），
因此校验角色内容需要一个统一的封闭集合。后续模块在这里登记自己的成员，例如
`device.camera.edit`、`template.version.edit`；首个片段说明由哪个模块执行该权限。
这里保存的是共享词汇，不保存授权行为；实际调用 `authorize` 的规则仍在各模块的
`usecases/` 中（§5.15）。

本模块只使用标准库，不能向上导入：纯用例读取权限集合时不应连带引入 SQLAlchemy 或
FastAPI。
"""

from __future__ import annotations

from enum import StrEnum

# §5.15 的动作粒度单独保存为集合，便于机械校验，而不是依赖阅读枚举成员。
# 发布有意折叠到 `edit`（Q18）；增加第四种动作需要重新作出决策，而不是新增成员。
# `dataset.dataset.import` 是控制面规格明确登记的唯一专门动作；其他未登记的 import
# 仍然会被 `parse_permission` 拒绝。
ACTIONS = frozenset({"view", "edit", "delete", "import"})


class Permission(StrEnum):
    """后端执行的全部权限；角色编辑只能使用这个封闭集合。

    角色写入不属于本枚举的权限名时会被拒绝，避免未登记字符串成为无人检查的权限。
    新模块落地时在这里登记成员，并由该模块自己的 `auth` 前缀迁移写入
    `auth_permission` 注册行。
    """

    # 读取账户列表和单个账户；不足以修改任何内容。
    USER_VIEW = "auth.user.view"
    # 创建、编辑、停用和恢复账户，以及为账户分配角色。
    # 停用和恢复属于 `edit`，不单独定义动作：它们是可变配置对象的可逆状态（§5.15），
    # 可以编辑账户的操作者也可以将其暂时停用。
    USER_EDIT = "auth.user.edit"
    # 彻底删除账户。它与 `edit` 分开，因为这是不可逆操作；§5.15 要求删除使用独立权限，
    # 这样管理员可以获得日常账户管理权而不必获得删除权。
    USER_DELETE = "auth.user.delete"

    ROLE_VIEW = "auth.role.view"
    ROLE_EDIT = "auth.role.edit"
    ROLE_DELETE = "auth.role.delete"

    # 读取设备拓扑：推理机、后端以及连接测试观测结果。
    INFERENCE_HOST_VIEW = "device.inference_host.view"
    # 创建和编辑推理机，以及可逆的停用/恢复；停用同样按 §5.15 折叠到 `edit`。
    INFERENCE_HOST_EDIT = "device.inference_host.edit"
    # 彻底删除推理机。它单独使用权限，因为停用正是为了避免不可逆删除。
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

    # 只读镜像边缘判定和主机健康；它不授予执行或配置权，因此本切片只需要这一项
    # monitor 权限。
    MONITOR_VIEW = "monitor.report.view"


class UnregisteredPermissionError(Exception):
    """表示不属于 `Permission` 成员的字符串。

    值进入领域时（例如编辑角色或从数据库读回角色）应抛出此异常，而不是容忍一个
    实际不会授予任何能力的权限。否则 `auth.user.approve` 可能看似已授予，却永远不会
    被任何检查使用。
    """

    def __init__(self, value: str) -> None:
        super().__init__(f"unregistered permission: {value}")
        self.value = value


def parse_permission(value: str) -> Permission:
    """返回 `value` 对应的 `Permission`，否则抛出 `UnregisteredPermissionError`。

    字符串只能从这里进入权限集合。格式正确不代表已经登记；`device.camera.edit` 即使
    形状正确，在 `device` 注册前也不是有效权限。
    """
    try:
        return Permission(value)
    except ValueError:
        raise UnregisteredPermissionError(value) from None
