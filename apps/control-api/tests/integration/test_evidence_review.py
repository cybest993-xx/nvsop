"""#171 人工复核结论：真实 ASGI + PostgreSQL 的登记、幂等、查询与并发边界。

`TestClient` 驱动实际 FastAPI 应用、每请求事务和容器里的真实 PostgreSQL；会话、权限与角色
来自真实登录与角色行，不覆盖 `authorized_caller`，也不使用假仓储。证据登记、PENDING 播种与
并发竞争复用 `test_evidence_access` / `test_evidence_concurrency` / `test_evidence_usecases`
的既有 helper，不另造同义造型。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session as DatabaseSession
from test_auth_http import CREDENTIALS, SESSION_PATH, an_account, settings
from test_evidence_access import _post_as_host, _registration
from test_evidence_concurrency import EVIDENCE_ID, NOW, Owner, registration
from test_runtime_observation_http import RuntimeTopology
from test_runtime_observation_http import runtime_topology as runtime_topology

from factory_sop.app import API_PREFIX, create_app
from factory_sop.auth.adapters.cookies import CSRF_COOKIE, CSRF_HEADER
from factory_sop.auth.adapters.repository import PostgresRoleRepository
from factory_sop.auth.api import Caller
from factory_sop.auth.model import Role, User, UserStatus
from factory_sop.auth.permissions import Permission
from factory_sop.evidence.adapters.repository import (
    PostgresEvidenceRepository,
    PostgresEvidenceReviewRepository,
)
from factory_sop.evidence.model import (
    EvidenceReference,
    EvidenceReferenceLock,
    EvidenceReview,
    EvidenceStatus,
    ReviewConclusion,
)
from factory_sop.evidence.reviews import begin_review, cancel_review, save_review
from factory_sop.evidence.usecases import register_evidence
from factory_sop.identifiers import new_id
from factory_sop.persistence import session_factory

ROLE_CODE = "evidence_reviewer"
REGISTRATIONS = f"{API_PREFIX}/evidence/registrations"


def _grant(engine: Engine) -> User:
    """真实账户 + 真实角色行同时授予查看与复核编辑；查询与提交都按真实权限判定。"""
    user = an_account(engine)
    with session_factory(engine)() as session:
        roles = PostgresRoleRepository(session)
        role = Role(
            new_id(),
            ROLE_CODE,
            "证据复核员",
            frozenset({Permission.EVIDENCE_VIEW, Permission.EVIDENCE_REVIEW_EDIT}),
        )
        roles.add(role)
        roles.assign(user_id=user.id, role_ids=(role.id,))
        session.commit()
    return user


def _seed_pending(engine: Engine, evidence_id: str) -> None:
    """复核 410 需要一条真实 PENDING 证据；登记路由对非 AVAILABLE 返回 422，故走仓储 seam。"""
    pending = replace(registration(sha256=None, reference=None), evidence_id=evidence_id)
    with session_factory(engine)() as session:
        assert PostgresEvidenceRepository(session).insert(
            EvidenceReference(pending, EvidenceStatus.PENDING, None, NOW)
        )
        session.commit()


def test_review_registration_is_authorized_idempotent_and_queryable(
    engine: Engine, runtime_topology: RuntimeTopology
) -> None:
    user = _grant(engine)
    host_id = str(runtime_topology.host.id)
    evidence_id, pending_id = f"{host_id}/复核 clip.mp4", f"{host_id}/复核 pending.mp4"
    app = create_app(settings(), session_factory=session_factory(engine))
    base = f"{API_PREFIX}/evidence/{evidence_id}/reviews"
    review_id = uuid4()
    submit = {"review_id": str(review_id), "generation": "original", "conclusion": "confirmed"}
    try:
        with (
            TestClient(app, base_url="https://testserver") as browser,
            TestClient(app, base_url="https://testserver") as host,
        ):
            assert browser.get(base).status_code == 401
            assert browser.post(base, json=submit).status_code == 401
            available = _post_as_host(
                host,
                runtime_topology,
                path=REGISTRATIONS,
                body=_registration(runtime_topology, evidence_id),
            )
            assert available.status_code == 200, available.text
            _seed_pending(engine, pending_id)
            assert browser.post(SESSION_PATH, json=CREDENTIALS).status_code == 201
            csrf = {CSRF_HEADER: browser.cookies[CSRF_COOKIE]}

            saved = browser.post(base, json=submit, headers=csrf)
            assert saved.status_code == 200, saved.text
            assert saved.json() | {"reviewed_at": None} == {
                "review_id": str(review_id),
                "evidence_id": evidence_id,
                "generation": "original",
                "reviewer_id": str(user.id),
                "reviewer_name": user.display_name,
                "reviewed_at": None,
                "conclusion": "confirmed",
            }
            reviewed_at = saved.json()["reviewed_at"]

            # 在审保护的可用生产接缝：同一用户可进入/退出占用，未退出时重复提交会被结案。
            holds = f"{API_PREFIX}/evidence/{evidence_id}/review-holds"
            hold_id, second_hold = uuid4(), uuid4()
            began = browser.post(
                holds,
                json={"review_id": str(hold_id), "generation": "original"},
                headers=csrf,
            )
            assert began.status_code == 200, began.text
            assert (began.json()["review_id"], began.json()["reviewer_id"]) == (
                str(hold_id),
                str(user.id),
            )
            # 同一 review_id 重放返回原 hold 与原时刻，不产生第二占用也未冲突。
            replay_hold = browser.post(
                holds,
                json={"review_id": str(hold_id), "generation": "original"},
                headers=csrf,
            )
            assert replay_hold.status_code == 200
            assert replay_hold.json() == began.json()
            # 代次不符与未登记证据同在锁定接缝上被拒绝，不产生残余占用。
            assert (
                browser.post(
                    holds, json={"review_id": str(uuid4()), "generation": "reclip"}, headers=csrf
                ).status_code
                == 409
            )
            assert (
                browser.post(
                    f"{API_PREFIX}/evidence/{host_id}/missing.mp4/review-holds",
                    json={"review_id": str(uuid4()), "generation": "original"},
                    headers=csrf,
                ).status_code
                == 404
            )
            reopened = browser.post(
                holds,
                json={"review_id": str(second_hold), "generation": "original"},
                headers=csrf,
            )
            assert reopened.status_code == 200
            cancelled = browser.post(
                holds + "/cancel", json={"review_id": str(second_hold)}, headers=csrf
            )
            assert cancelled.status_code == 204, cancelled.text
            # 重复取消已结束的占用是解释性冲突，不是静默成功。
            assert (
                browser.post(
                    holds + "/cancel", json={"review_id": str(second_hold)}, headers=csrf
                ).status_code
                == 409
            )
            # 提交新结论会结案本人仍持有的在审占用（见下方 appended 之后断言）。

            # 同 ID 同内容重放返回原记录与原时间；同 ID 不同结论 409 且不覆盖。
            replay = browser.post(base, json=submit, headers=csrf)
            assert replay.status_code == 200
            assert replay.json() == saved.json()
            conflict = browser.post(base, json=submit | {"conclusion": "rejected"}, headers=csrf)
            assert conflict.status_code == 409
            assert conflict.json() == {
                "type": "about:blank",
                "title": "复核请求与既有结论冲突",
                "status": 409,
                "error_code": "IDEMPOTENCY_CONFLICT",
            }
            assert conflict.headers["content-type"] == "application/problem+json"

            # 不同 ID 追加为第二条；查询返回该证据全部复核，保持原记录。
            second = uuid4()
            appended = browser.post(
                base,
                json={"review_id": str(second), "generation": "original", "conclusion": "rejected"},
                headers=csrf,
            )
            assert appended.status_code == 200
            rows = browser.get(base).json()
            assert [row["review_id"] for row in rows] == [str(review_id), str(second)]
            assert [row["conclusion"] for row in rows] == ["confirmed", "rejected"]
            assert rows[0]["reviewed_at"] == reviewed_at
            assert {row["reviewer_id"] for row in rows} == {str(user.id)}
            # 提交结论结案了本人仍持有的在审占用；已取消的占用不复活。
            with session_factory(engine)() as session:
                assert (
                    PostgresEvidenceReviewRepository(session).active_reviewers(
                        evidence_id=evidence_id
                    )
                    == frozenset()
                )

            # 另一作者的既有结论与机器证据行不被后续复核改写（owning 仓储 seam 全字段比较）。
            other_id = uuid4()
            with session_factory(engine)() as session:
                assert PostgresEvidenceReviewRepository(session).insert(
                    EvidenceReview(
                        review_id=other_id,
                        evidence_id=evidence_id,
                        generation="original",
                        reviewer_id=uuid4(),
                        reviewer_name="另一位复核人",
                        reviewed_at=NOW,
                        conclusion=ReviewConclusion.REJECTED,
                    )
                )
                session.commit()
            with session_factory(engine)() as session:
                evidence_before = PostgresEvidenceRepository(session).find(evidence_id)
                other_before = PostgresEvidenceReviewRepository(session).find(other_id)
            assert evidence_before is not None
            assert other_before is not None
            third = uuid4()
            appended_again = browser.post(
                base,
                json={"review_id": str(third), "generation": "original", "conclusion": "confirmed"},
                headers=csrf,
            )
            assert appended_again.status_code == 200
            with session_factory(engine)() as session:
                assert PostgresEvidenceRepository(session).find(evidence_id) == evidence_before
                assert PostgresEvidenceReviewRepository(session).find(other_id) == other_before

            # 未登记 / 代次不符 / 尚未对账：分别以 404 / 409 / 410 拒绝。
            unregistered = browser.post(
                f"{API_PREFIX}/evidence/{host_id}/missing.mp4/reviews", json=submit, headers=csrf
            )
            assert (unregistered.status_code, unregistered.json()["error_code"]) == (
                404,
                "OBJECT_NOT_FOUND",
            )
            mismatch = browser.post(base, json=submit | {"generation": "reclip"}, headers=csrf)
            assert (mismatch.status_code, mismatch.json()["error_code"]) == (
                409,
                "RESOURCE_MISMATCH",
            )
            unavailable = browser.post(
                f"{API_PREFIX}/evidence/{pending_id}/reviews", json=submit, headers=csrf
            )
            assert (unavailable.status_code, unavailable.json()["error_code"]) == (
                410,
                "STATE_CONFLICT",
            )

            # 撤权：删除真实角色分配后同一提交按权限拒绝。
            with session_factory(engine)() as session:
                PostgresRoleRepository(session).assign(user_id=user.id, role_ids=())
                session.commit()
            revoked = browser.post(base, json=submit | {"conclusion": "rejected"}, headers=csrf)
            assert revoked.status_code == 403
            assert revoked.json()["error_code"] == "PERMISSION_DENIED"
            assert browser.get(base).status_code == 403

            # 失效会话：真实会话记录置为过期后，GET 与 POST 都按未认证拒绝。
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "UPDATE auth_session SET created_at = now() - interval '40 days', "
                        "last_used_at = now() - interval '40 days'"
                    )
                )
            assert browser.get(base).status_code == 401
            assert browser.post(base, json=submit, headers=csrf).status_code == 401
    finally:
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM evidence_evidence WHERE evidence_id IN (:a, :b)"),
                {"a": evidence_id, "b": pending_id},
            )
            connection.execute(text("TRUNCATE auth_user CASCADE"))
            connection.execute(
                text("DELETE FROM auth_role WHERE code = :code"), {"code": ROLE_CODE}
            )


class _BarrierEvidence(PostgresEvidenceRepository):
    """在取引用行锁之前对齐两个真实事务：并发提交由那把行锁串行化。

    引用行锁（#206）使只有一个事务能进入写入段，另一个在锁后看到胜者并得到解释性冲突；
    因此"同一意图只有一个胜者"不再依赖插入顺序的巧合。
    """

    def __init__(self, session: DatabaseSession, barrier: Barrier) -> None:
        super().__init__(session)
        self._barrier = barrier

    def lock(self, evidence_id: str) -> EvidenceReferenceLock:
        self._barrier.wait(timeout=10)
        return super().lock(evidence_id)


def _caller(reviewer_id: UUID) -> Caller:
    return Caller(
        user=User(
            id=reviewer_id,
            login_name="reviewer",
            display_name="复核员",
            password_hash="unused",  # pragma: allowlist secret
            status=UserStatus.ACTIVE,
        ),
        granted=frozenset({Permission.EVIDENCE_VIEW, Permission.EVIDENCE_REVIEW_EDIT}),
    )


def test_competing_review_intents_cannot_overwrite_the_winner(engine: Engine) -> None:
    review_id, reviewer_id = uuid4(), uuid4()
    try:
        with DatabaseSession(engine) as setup:
            register_evidence(
                registration(sha256="a" * 64, reference="evidence/race.mp4"),
                received_at=NOW,
                evidence=PostgresEvidenceRepository(setup),
                host_gateway=Owner(),
            )
            setup.commit()
        barrier = Barrier(2)

        def compete(conclusion: ReviewConclusion) -> tuple[str, str]:
            with DatabaseSession(engine) as session:
                try:
                    record = save_review(
                        _caller(reviewer_id),
                        review_id=review_id,
                        evidence_id=EVIDENCE_ID,
                        generation="original",
                        conclusion=conclusion,
                        reviewed_at=NOW,
                        evidence=_BarrierEvidence(session, barrier),
                        reviews=PostgresEvidenceReviewRepository(session),
                    )
                    session.commit()
                    return ("saved", record.conclusion.value)
                except ValueError as error:
                    session.rollback()
                    return (getattr(error, "code", str(error)), "")

        with ThreadPoolExecutor(max_workers=2) as workers:
            results = list(
                workers.map(compete, (ReviewConclusion.CONFIRMED, ReviewConclusion.REJECTED))
            )
        assert {result[0] for result in results} == {"saved", "review_conflict"}
        winner = next(result[1] for result in results if result[0] == "saved")
        with DatabaseSession(engine) as verify:
            rows = PostgresEvidenceReviewRepository(verify).list_for_evidence(EVIDENCE_ID)
        assert [row.conclusion.value for row in rows] == [winner]
    finally:
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM evidence_evidence WHERE evidence_id = :id"),
                {"id": EVIDENCE_ID},
            )


def test_active_review_is_owned_and_cancel_does_not_release_another_review(engine: Engine) -> None:
    evidence_id = EVIDENCE_ID
    reviewer, other = uuid4(), uuid4()
    try:
        with DatabaseSession(engine) as session:
            register_evidence(
                registration(sha256="b" * 64, reference="evidence/active-review.mp4"),
                received_at=NOW,
                evidence=PostgresEvidenceRepository(session),
                host_gateway=Owner(),
            )
            session.commit()
        with DatabaseSession(engine) as session:
            evidence = PostgresEvidenceRepository(session)
            reviews = PostgresEvidenceReviewRepository(session)
            first = begin_review(
                _caller(reviewer),
                review_id=uuid4(),
                evidence_id=evidence_id,
                generation="original",
                now=NOW,
                evidence=evidence,
                reviews=reviews,
            )
            second = begin_review(
                _caller(other),
                review_id=uuid4(),
                evidence_id=evidence_id,
                generation="original",
                now=NOW,
                evidence=evidence,
                reviews=reviews,
            )
            assert first.review_id != second.review_id
            assert reviews.active_reviewers(evidence_id=evidence_id) == frozenset({reviewer, other})
            cancel_review(_caller(reviewer), review_id=first.review_id, now=NOW, reviews=reviews)
            assert reviews.active_reviewers(evidence_id=evidence_id) == frozenset({other})
            # 提交结论只结案提交者自己的在审占用；另一位 reviewer 的占用不受影响。
            reopened = begin_review(
                _caller(reviewer),
                review_id=uuid4(),
                evidence_id=evidence_id,
                generation="original",
                now=NOW,
                evidence=evidence,
                reviews=reviews,
            )
            assert reviews.active_reviewers(evidence_id=evidence_id) == frozenset({reviewer, other})
            saved = save_review(
                _caller(reviewer),
                review_id=uuid4(),
                evidence_id=evidence_id,
                generation="original",
                conclusion=ReviewConclusion.CONFIRMED,
                reviewed_at=NOW,
                evidence=evidence,
                reviews=reviews,
            )
            assert reviews.active_reviewers(evidence_id=evidence_id) == frozenset({other})
            concluded = reviews.list_for_evidence(evidence_id)
            assert [row.review_id for row in concluded] == [saved.review_id]
            assert reopened.review_id not in {row.review_id for row in concluded}
            session.commit()
    finally:
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM evidence_evidence WHERE evidence_id = :id"), {"id": evidence_id}
            )


def test_historical_unknown_review_state_still_allows_a_new_review(engine: Engine) -> None:
    """历史行 review_state_known=false 只让删除资格保守未知，不阻止用户开启新复核或保存结论。"""
    evidence_id = EVIDENCE_ID
    reviewer = uuid4()
    try:
        with DatabaseSession(engine) as session:
            register_evidence(
                registration(sha256="e" * 64, reference="evidence/historical.mp4"),
                received_at=NOW,
                evidence=PostgresEvidenceRepository(session),
                host_gateway=Owner(),
            )
            session.commit()
        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE evidence_evidence SET review_state_known = false "
                    "WHERE evidence_id = :id"
                ),
                {"id": evidence_id},
            )
        with DatabaseSession(engine) as session:
            evidence = PostgresEvidenceRepository(session)
            reviews = PostgresEvidenceReviewRepository(session)
            # 未知历史状态不让 begin 失败；新占用仍被登记，复核继续可推进。
            hold = begin_review(
                _caller(reviewer),
                review_id=uuid4(),
                evidence_id=evidence_id,
                generation="original",
                now=NOW,
                evidence=evidence,
                reviews=reviews,
            )
            assert hold.evidence_id == evidence_id
            # 未知仍表达为 None：删除资格继续保守，不能当作“无在审复核”。
            assert reviews.active_reviewers(evidence_id=evidence_id) is None
            saved = save_review(
                _caller(reviewer),
                review_id=uuid4(),
                evidence_id=evidence_id,
                generation="original",
                conclusion=ReviewConclusion.CONFIRMED,
                reviewed_at=NOW,
                evidence=evidence,
                reviews=reviews,
            )
            assert saved.evidence_id == evidence_id
            session.commit()
    finally:
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM evidence_evidence WHERE evidence_id = :id"), {"id": evidence_id}
            )
