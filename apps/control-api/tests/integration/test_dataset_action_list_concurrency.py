"""Concurrent action-list editing resolves as a dataset domain conflict."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Barrier
from uuid import UUID

from _integration_support import caller, cleanup_dataset
from sqlalchemy import Engine
from sqlalchemy.orm import Session as DatabaseSession

from factory_sop.auth.authorization import Caller
from factory_sop.auth.permissions import Permission
from factory_sop.dataset.adapters.repository import PostgresDatasetRepository
from factory_sop.dataset.errors import DatasetRefusalCode
from factory_sop.dataset.model import ActionListRevision, TrainingDataset
from factory_sop.dataset.usecases.annotation import AnnotationRefusedError, register_action_list

DATASET_ID = UUID("019937d8-0d10-7b31-8d2d-4e60c8f4fa01")
NOW = datetime(2026, 9, 30, tzinfo=UTC)


class RacingDatasetRepository(PostgresDatasetRepository):
    def __init__(self, session: DatabaseSession, barrier: Barrier) -> None:
        super().__init__(session)
        self._barrier = barrier

    def latest_action_list(self, dataset_id: UUID) -> ActionListRevision | None:
        value = super().latest_action_list(dataset_id)
        self._barrier.wait(timeout=5)
        return value


def test_concurrent_action_list_revision_returns_domain_conflict(engine: Engine) -> None:
    editor = Caller(
        user=caller().user,
        granted=frozenset({Permission.DATASET_EDIT}),
    )
    with DatabaseSession(engine) as setup, setup.begin():
        PostgresDatasetRepository(setup).add_dataset(
            TrainingDataset(
                id=DATASET_ID,
                name="动作清单并发测试集",
                created_by=editor.user.id,
                updated_by=editor.user.id,
                created_at=NOW,
                updated_at=NOW,
            )
        )

    barrier = Barrier(2)

    def edit(actions: tuple[str, ...]) -> tuple[str, int | None]:
        with DatabaseSession(engine) as session:
            try:
                with session.begin():
                    value = register_action_list(
                        dataset_id=DATASET_ID,
                        actions=actions,
                        caller=editor,
                        now=NOW,
                        datasets=RacingDatasetRepository(session, barrier),
                    )
                return "created", value.revision
            except AnnotationRefusedError as error:
                return error.code.value, None

    try:
        with ThreadPoolExecutor(max_workers=2) as workers:
            results = list(
                workers.map(
                    edit,
                    (("(1) 取料",), ("(1) 取料", "(2) 安装")),
                )
            )

        assert {result[0] for result in results} == {
            "created",
            DatasetRefusalCode.STATE_CONFLICT.value,
        }
        with DatabaseSession(engine) as verify:
            latest = PostgresDatasetRepository(verify).latest_action_list(DATASET_ID)
            assert latest is not None
            assert latest.revision == 1
            assert latest.actions in {("(1) 取料",), ("(1) 取料", "(2) 安装")}
    finally:
        cleanup_dataset(engine, DATASET_ID)
