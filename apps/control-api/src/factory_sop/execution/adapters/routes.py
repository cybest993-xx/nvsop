"""强制改绑双人确认的 HTTP adapter。

薄适配层：授权、权限实时复核、条件更新都在 `execution.usecases` 内完成，这里只解析外部
输入、取请求事务里的 owner seam、把结果投影成 wire 形状。风险原文由 `execution.model`
唯一权威提供，前端不自拟第二段；读取/确认都回带原文与已冻结的记录内容。

跨 owner 依赖只经 `auth.api` 这一个公开出口（`Authorized`/`needs`/`handover_authority`），
不直接导入 `auth.adapters`。

已知拒绝经 `app.py` 的 `ExecutionRefusedError` handler 变成 `problem+json`。工位/旧机/目标机
是真实外键：引用不存在的记录时数据库以 SQLSTATE 23503 拒绝，这里只翻译这一类外键违约，
其他数据库错误原样上抛，不掩盖服务器故障。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError

from factory_sop.auth.api import (
    Authorized,
    HandoverAuthority,
    Permission,
    handover_authority,
    needs,
)
from factory_sop.execution.adapters import dependencies
from factory_sop.execution.errors import ExecutionRefusalCode, ExecutionRefusedError
from factory_sop.execution.model import HANDOVER_RISK_STATEMENT, HandoverConfirmation
from factory_sop.execution.repository import HandoverRepository
from factory_sop.execution.usecases import (
    confirm_handover,
    create_handover,
    read_handover,
    read_handover_risk,
)
from factory_sop.problem import problem_openapi_response

router = APIRouter(prefix="/execution", tags=["execution"])

ProblemResponses = dict[int | str, dict[str, Any]]

# PostgreSQL 的 foreign_key_violation。只有这一类的 IntegrityError 表示外部引用了不存在的
# 真实记录；其他完整性错误是服务器故障，必须原样上抛。
_FOREIGN_KEY_VIOLATION = "23503"

_UNAUTHORIZED: ProblemResponses = {
    401: problem_openapi_response("Authentication required or session invalid"),
    403: problem_openapi_response("Permission denied or CSRF token invalid"),
}
_NOT_FOUND_RESPONSES: ProblemResponses = {
    404: problem_openapi_response("Execution record not found")
}
_CONFLICT_RESPONSES: ProblemResponses = {409: problem_openapi_response("Execution state conflict")}
_UNPROCESSABLE_RESPONSES: ProblemResponses = {422: problem_openapi_response("Request invalid")}


class HandoverRiskView(BaseModel):
    """服务器权威的强制改绑风险原文。"""

    risk_statement: str


class HandoverView(BaseModel):
    """一条强制改绑请求的冻结内容与两人确认事实；`risk_statement` 为服务器原文。"""

    handover_id: UUID
    station_id: UUID
    from_host_id: UUID
    to_host_id: UUID
    operator_id: UUID
    operator_confirmed_at: datetime
    operator_risk_shown: bool
    second_operator_id: UUID | None
    second_confirmed_at: datetime | None
    second_risk_shown: bool | None
    risk_statement: str


class HandoverCreation(BaseModel):
    """建立强制改绑请求的输入；内容建立后冻结，改内容必须新建。"""

    station_id: UUID
    from_host_id: UUID
    to_host_id: UUID
    risk_acknowledgement: str


class HandoverConfirmationInput(BaseModel):
    """第二确认输入；必须回带被展示的同一冻结内容。"""

    station_id: UUID
    from_host_id: UUID
    to_host_id: UUID
    risk_acknowledgement: str


def _view(record: HandoverConfirmation) -> HandoverView:
    return HandoverView(
        handover_id=record.handover_id,
        station_id=record.station_id,
        from_host_id=record.from_host_id,
        to_host_id=record.to_host_id,
        operator_id=record.operator_id,
        operator_confirmed_at=record.operator_confirmed_at,
        operator_risk_shown=record.operator_risk_shown,
        second_operator_id=record.second_operator_id,
        second_confirmed_at=record.second_confirmed_at,
        second_risk_shown=record.second_risk_shown,
        risk_statement=HANDOVER_RISK_STATEMENT,
    )


def _foreign_key_violation(error: IntegrityError) -> bool:
    return getattr(error.orig, "sqlstate", None) == _FOREIGN_KEY_VIOLATION


@router.get(
    "/handovers/risk",
    operation_id="readHandoverRisk",
    openapi_extra=needs(Permission.HANDOVER_EDIT),
    responses=_UNAUTHORIZED,
)
def read_risk(caller: Authorized) -> HandoverRiskView:
    """只读返回服务器权威风险原文。"""
    return HandoverRiskView(risk_statement=read_handover_risk(caller=caller))


@router.post(
    "/handovers",
    status_code=status.HTTP_201_CREATED,
    operation_id="createHandover",
    openapi_extra=needs(Permission.HANDOVER_EDIT),
    responses=_UNAUTHORIZED | _NOT_FOUND_RESPONSES | _UNPROCESSABLE_RESPONSES,
)
def create(
    submission: HandoverCreation,
    caller: Authorized,
    handover_store: Annotated[HandoverRepository, Depends(dependencies.handovers)],
    authority: Annotated[HandoverAuthority, Depends(handover_authority)],
) -> HandoverView:
    """建立强制改绑请求，操作者同时完成第一确认。"""
    try:
        record = create_handover(
            caller=caller,
            station_id=submission.station_id,
            from_host_id=submission.from_host_id,
            to_host_id=submission.to_host_id,
            risk_acknowledgement=submission.risk_acknowledgement,
            now=datetime.now(UTC),
            handovers=handover_store,
            authority=authority,
        )
    except IntegrityError as error:
        if not _foreign_key_violation(error):
            raise
        # 工位/旧机/目标机外键指向不存在的记录：外部输入错误，不是服务器故障。
        raise ExecutionRefusedError(ExecutionRefusalCode.HANDOVER_TARGET_NOT_FOUND) from error
    return _view(record)


@router.get(
    "/handovers/{handover_id}",
    operation_id="readHandover",
    openapi_extra=needs(Permission.HANDOVER_EDIT),
    responses=_UNAUTHORIZED | _NOT_FOUND_RESPONSES,
)
def read(
    handover_id: UUID,
    caller: Authorized,
    handover_store: Annotated[HandoverRepository, Depends(dependencies.handovers)],
) -> HandoverView:
    """按请求 id 读取冻结内容与风险原文，供第二名用户查看。"""
    return _view(read_handover(caller=caller, handover_id=handover_id, handovers=handover_store))


@router.post(
    "/handovers/{handover_id}/confirmation",
    operation_id="confirmHandover",
    openapi_extra=needs(Permission.HANDOVER_EDIT),
    responses=_UNAUTHORIZED | _NOT_FOUND_RESPONSES | _CONFLICT_RESPONSES | _UNPROCESSABLE_RESPONSES,
)
def confirm(
    handover_id: UUID,
    submission: HandoverConfirmationInput,
    caller: Authorized,
    handover_store: Annotated[HandoverRepository, Depends(dependencies.handovers)],
    authority: Annotated[HandoverAuthority, Depends(handover_authority)],
) -> HandoverView:
    """由另一名当前有权的用户确认被展示的同一请求内容。"""
    record = confirm_handover(
        caller=caller,
        handover_id=handover_id,
        station_id=submission.station_id,
        from_host_id=submission.from_host_id,
        to_host_id=submission.to_host_id,
        risk_acknowledgement=submission.risk_acknowledgement,
        now=datetime.now(UTC),
        handovers=handover_store,
        authority=authority,
    )
    return _view(record)


__all__ = [
    "HandoverConfirmationInput",
    "HandoverCreation",
    "HandoverRiskView",
    "HandoverView",
    "router",
]
