"""既有 job worker 的独立组合根。"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from factory_sop.auth.adapters.current_caller import current_caller_resolver
from factory_sop.dataset.adapters.dependencies import (
    annotation_runtime,
    artifact_executor,
    ddm_executor,
    usage_runtime,
    validation_runtime,
)
from factory_sop.evidence.adapters.repository import (
    PostgresEvidenceDeletionClaimRepository,
    PostgresEvidenceRecordProtection,
)
from factory_sop.job.adapters.worker import run_worker as run_job_worker
from factory_sop.monitor.sweep import RepositoryMonitorRetirement
from factory_sop.retention.adapters.repository import (
    PostgresRetentionPolicyRepository,
    PostgresStationRetentionOverrideRepository,
)
from factory_sop.retention.adapters.sweep_repository import PostgresRetentionSweepRepository
from factory_sop.retention.api import run_retention_sweep
from factory_sop.settings import Settings


def retention_sweep_runner(
    settings: Settings, factory: Callable[[], Session]
) -> Callable[[], None]:
    """一次 UoW 冻结权威、回收各 owner 的数据并结案账本；失败分段用 savepoint 隔离。"""

    def run() -> None:
        interval = settings.retention_sweep_interval_seconds
        if interval is None:
            return
        with factory() as session:
            PostgresEvidenceDeletionClaimRepository(session).lock_authority()
            run_retention_sweep(
                policies=PostgresRetentionPolicyRepository(session),
                records=PostgresStationRetentionOverrideRepository(session),
                ledger=PostgresRetentionSweepRepository(session),
                executor=RepositoryMonitorRetirement(
                    session=session,
                    evidence_protected=PostgresEvidenceRecordProtection(session).retained,
                ),
                now=datetime.now(UTC),
                interval_seconds=interval,
                batch_limit=settings.retention_sweep_batch_limit,
            )
            session.commit()

    return run


def run_worker(environment: Mapping[str, str]) -> None:
    """在同一 worker 进程装配数据集任务和保留回收。"""
    run_job_worker(
        environment,
        artifact_executor_factory=artifact_executor,
        runtime_factory=validation_runtime,
        usage_runtime_factory=usage_runtime,
        annotation_runtime_factory=annotation_runtime,
        ddm_executor_factory=ddm_executor,
        current_caller_resolver_factory=current_caller_resolver,
        retention_sweep_factory=retention_sweep_runner,
    )


if __name__ == "__main__":
    run_worker(os.environ)
