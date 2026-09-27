from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from factory_sop.dataset.adapters import artifact_execution as artifact_module
from factory_sop.dataset.adapters.artifact_execution import PostgresDatasetArtifactExecutor
from factory_sop.dataset.api import ArtifactExecutionOutcome


class _Session:
    def __init__(self) -> None:
        self.commits = 0
        self.rollbacks = 0

    def __enter__(self) -> _Session:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


class _Factory:
    def __init__(self) -> None:
        self.sessions: list[_Session] = []

    def __call__(self) -> _Session:
        session = _Session()
        self.sessions.append(session)
        return session


def test_cleanup_pending_commit_obeys_execution_fence(monkeypatch: pytest.MonkeyPatch) -> None:
    factory = _Factory()
    executor = PostgresDatasetArtifactExecutor(
        factory=factory,  # type: ignore[arg-type]
        storage_factory=object,  # type: ignore[arg-type]
        generate=lambda path, name: b"",
    )
    job = SimpleNamespace(id=uuid4())
    artifact = SimpleNamespace(id=uuid4(), dataset_id=uuid4(), object_key="candidate")
    target = SimpleNamespace(job=job, artifact=artifact)
    monkeypatch.setattr(artifact_module, "begin_artifact_generation", lambda **kwargs: target)
    monkeypatch.setattr(
        artifact_module,
        "render_ddm_artifact_with_base",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("render failed")),
    )
    monkeypatch.setattr(artifact_module, "mark_artifact_cleanup_pending", lambda **kwargs: True)
    monkeypatch.setattr(executor, "_discard_candidate", lambda **kwargs: False)

    commits = 0

    def commit_transaction(session: object) -> bool:
        nonlocal commits
        commits += 1
        if commits == 1:
            session.commit()  # type: ignore[attr-defined]
            return True
        session.rollback()  # type: ignore[attr-defined]
        return False

    result = executor.execute(
        job=job,  # type: ignore[arg-type]
        finish_job=lambda session, outcome: True,
        commit_transaction=commit_transaction,
    )

    assert result.outcome is ArtifactExecutionOutcome.LEASE_LOST
    assert commits == 2
    assert factory.sessions[-1].commits == 0
    assert factory.sessions[-1].rollbacks == 1
