from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import land_pr  # noqa: E402
import landing_queue as lq  # noqa: E402

ROOT_SHA = "a" * 40
NEW_HEAD = "b" * 40
MAIN_HEAD = "9" * 40
MERGE = "c" * 40
BASE = "d" * 40
TREE = "e" * 40
REPO = "owner/repo"


def attestation_body(
    *,
    repo: str = REPO,
    pr: int = 1,
    root: str = ROOT_SHA,
    branch: str = "agent/a/demo",
    base: str = "main",
    actions: tuple[str, ...] = ("refresh", "squash-merge"),
    review: dict[str, str] | None = None,
    confirmation: dict[str, str] | None = None,
    check: dict[str, str] | None = None,
) -> str:
    payload = {
        "version": 1,
        "repository": repo,
        "pr": pr,
        "root": root,
        "head_branch": branch,
        "base": base,
        "actions": list(actions),
        "confirmation": confirmation
        if confirmation is not None
        else {
            "source": "user message 2024-01-01",
            "quote": "approve the bounded PR lifecycle plan",
        },
        "check": check
        if check is not None
        else {
            "root": root,
            "command": "make check",
            "result": "passed",
            "evidence": "make check output",
        },
        "review": review
        if review is not None
        else {
            "spec": "passed",
            "standards": "passed",
            "root": root,
            "evidence": "independent Spec + Standards review",
        },
    }
    return f"{lq.ATTESTATION_MARKER}\n```json\n{json.dumps(payload, sort_keys=True)}\n```\n"


def comment(
    cid: int,
    body: str,
    *,
    author: str = "maintainer",
    author_type: str = "User",
    pr: int = 1,
    **kw: str,
) -> lq.Comment:
    return lq.Comment(
        cid,
        body,
        author,
        author_type,
        kw.get("created", "2024-01-01T00:00:00Z"),
        kw.get("updated", "2024-01-01T00:00:00Z"),
        f"https://api.github.com/repos/{REPO}/issues/{pr}",
    )


def request_comment(
    cid: int,
    kind: str,
    *,
    pr: int = 1,
    candidate: str = ROOT_SHA,
    attestation_digest: str = "",
    attestation_comment: int = 500,
    **kw: str,
) -> lq.Comment:
    request = lq.Request(
        kind, pr, cid, candidate, "f" * 32, attestation_comment, attestation_digest
    )
    return comment(cid, lq.request_body(request), pr=pr, **kw)


def enqueue_request(
    cid: int, pr: int, root: str, att: lq.Comment, attestation_comment: int
) -> lq.Comment:
    return request_comment(
        cid,
        "enqueue",
        pr=pr,
        candidate=root,
        attestation_digest=hashlib.sha256(att.body.encode()).hexdigest(),
        attestation_comment=attestation_comment,
    )


def pr_state(
    *,
    number: int = 1,
    state: str = "OPEN",
    draft: bool = False,
    base: str = BASE,
    head: str = ROOT_SHA,
    merge_state: str = "CLEAN",
    mergeable: str = "MERGEABLE",
    review: str = "approved",
    merge_commit: str | None = None,
    head_repository: str = REPO,
) -> land_pr.PullRequestState:
    return land_pr.PullRequestState(
        number=number,
        state=state,
        draft=draft,
        base_name="main",
        base_oid=base,
        head_name="agent/a/demo",
        head_oid=head,
        merge_state=merge_state,
        mergeable=mergeable,
        ci_required="success",
        review_decision=review,
        merge_commit=merge_commit,
        head_repository=head_repository,
    )


def rulesets(*, app: int = 123, **mutation: object) -> list[dict[str, object]]:
    def branch(
        ref: str, rid: int, rules: list[dict[str, object]], bypass: object
    ) -> dict[str, object]:
        return {
            "id": rid,
            "target": "branch",
            "enforcement": "active",
            "conditions": {"ref_name": {"include": [ref], "exclude": []}},
            "rules": rules,
            "bypass_actors": bypass,
        }

    app_bypass = [{"actor_type": "Integration", "actor_id": app, "bypass_mode": "always"}]
    evidence = branch(
        "refs/heads/main",
        1,
        [
            {
                "type": "pull_request",
                "parameters": {
                    "allowed_merge_methods": ["squash"],
                    "required_review_thread_resolution": True,
                },
            },
            {
                "type": "required_status_checks",
                "parameters": {
                    "strict_required_status_checks_policy": True,
                    "required_status_checks": [
                        {"context": "CI required", "integration_id": app},
                        {"context": "Landing gate", "integration_id": app},
                    ],
                },
            },
        ],
        [],
    )
    update = branch("refs/heads/main", 2, [{"type": "update"}], app_bypass)
    state = branch("refs/heads/landing-queue-state", 3, [{"type": "update"}], app_bypass)
    mutation_map = {
        "evidence_enforcement": lambda v: evidence.update(enforcement=v),
        "evidence_refs": lambda v: evidence["conditions"]["ref_name"].update(include=v),
        "evidence_exclude": lambda v: evidence["conditions"]["ref_name"].update(exclude=v),
        "evidence_rule": lambda v: evidence["rules"].__setitem__(0, v),
        "update_bypass": lambda v: update.update(bypass_actors=v),
        "state_bypass": lambda v: state.update(bypass_actors=v),
    }
    for key, value in mutation.items():
        mutation_map[key](value)
    return [evidence, update, state]


def run_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "head_sha": MAIN_HEAD,
        "head_branch": "main",
        "path": lq.CONTROLLER_PATH,
        # 默认形状是运行在 main 上的 workflow_dispatch；pull_request_target 的头属于触发 PR，
        # 需要单独的 referenced_workflows 来源证明，不能借用此默认。
        "event": "workflow_dispatch",
        "run_attempt": 1,
        "status": "completed",
        "repository": {"full_name": REPO},
    }
    payload.update(overrides)
    return payload


def job(name: str, conclusion: str | None, status: str = "completed") -> dict[str, object]:
    return {"name": name, "status": status, "conclusion": conclusion}


def ci_jobs(*items: dict[str, object]) -> dict[str, list[dict[str, object]]]:
    return {"9": list(items)}


class FakeBackend:
    """只在 Backend 边界注入的假实现；记录副作用顺序。"""

    def __init__(
        self,
        *,
        state: lq.State | None = None,
        revision: str = "rev-1",
        comments: dict[int, lq.Comment] | None = None,
        pr: land_pr.PullRequestState | None = None,
        prs: dict[int, land_pr.PullRequestState] | None = None,
        main: str = BASE,
        parents: dict[str, tuple[str, ...]] | None = None,
        ancestors: set[tuple[str, str]] | None = None,
        trees: dict[str, str] | None = None,
        runs: dict[str, dict[str, object]] | None = None,
        jobs: dict[str, list[dict[str, object]]] | None = None,
        rules: list[dict[str, object]] | None = None,
        permissions: dict[str, str] | None = None,
        merge_result: str = MERGE,
        merge_error: Exception | None = None,
    ) -> None:
        self.state_obj, self.revision, self.comments = (
            state if state is not None else lq.State(),
            revision,
            comments or {},
        )
        self.pr, self.main, self.parents = pr or pr_state(), main, parents or {}
        self.prs = prs or {}
        self.ancestors, self.trees, self.runs, self.jobs_map = (
            ancestors or set(),
            trees or {},
            runs or {},
            jobs or {},
        )
        self.rules = rules if rules is not None else rulesets()
        self.permissions, self.merge_result, self.merge_error = (
            permissions or {"maintainer": "write"},
            merge_result,
            merge_error,
        )
        (
            self.writes,
            self.refresh_calls,
            self.merge_calls,
            self.statuses,
            self.dispatches,
            self.posted,
            self.events,
        ) = 0, [], [], [], 0, [], []

    def repository(self) -> str:
        return REPO

    def state(self) -> tuple[lq.State, str]:
        return self.state_obj, self.revision

    def write_state(self, state: lq.State, revision: str) -> bool:
        self.writes += 1
        if revision != self.revision:
            return False
        # 经过真实持久化编解码：任何不可读字段都会在此暴露，而非静默留在内存。
        state = lq.state_from_json(json.loads(json.dumps(lq.state_to_json(state))))
        self.events.append(("write", state))
        self.state_obj, self.revision = state, f"rev-{self.writes}"
        return True

    def inbox(self) -> list[lq.Comment]:
        return [c for c in self.comments.values() if lq.REQUEST_MARKER in c.body]

    def comment(self, comment_id: int) -> lq.Comment:
        return self.comments[comment_id]

    def permission(self, login: str) -> str:
        return self.permissions.get(login, "none")

    def pull_request(self, number: int) -> land_pr.PullRequestState:
        return self.prs.get(number, self.pr)

    def main_sha(self) -> str:
        return self.main

    def commit_parents(self, sha: str) -> tuple[str, ...]:
        return self.parents.get(sha, ())

    def is_ancestor(self, ancestor: str, descendant: str) -> bool:
        return ancestor == descendant or (ancestor, descendant) in self.ancestors

    def tree_sha(self, sha: str) -> str:
        return self.trees.get(sha, TREE)

    def rulesets(self) -> list[dict[str, object]]:
        return self.rules

    def refresh(self, number: int, head: str) -> None:
        self.refresh_calls.append((number, head))

    def merge(self, number: int, head: str) -> str:
        self.merge_calls.append((number, head))
        if self.merge_error is not None:
            raise self.merge_error
        current = self.prs.get(number, self.pr)
        merged = replace(current, state="MERGED", merge_commit=self.merge_result)
        if number in self.prs:
            self.prs[number] = merged
        else:
            self.pr = merged
        self.main = self.merge_result
        return self.merge_result

    def run(self, run_id: str) -> dict[str, object]:
        return self.runs[run_id]

    def jobs(self, run_id: str) -> list[dict[str, object]]:
        return self.jobs_map.get(run_id, [])

    def publish_status(self, sha: str, context: str, state: str, description: str) -> None:
        self.statuses.append((sha, context, state))
        self.events.append(("status", (sha, context, state)))

    def post_request(self, body: str) -> int:
        self.posted.append(body)
        return len(self.posted)

    def dispatch_wake(self) -> bool:
        self.dispatches += 1
        return True


def active_entry(
    *,
    pr: int = 1,
    root: str = ROOT_SHA,
    phase: str | None = None,
    ci_run_id: str | None = None,
    ci_attempt: str | None = None,
    ci_controller: str | None = None,
    ci_head: str | None = None,
    ci_base: str | None = None,
    state: str = lq.ACTIVE,
    request_id: int = 1,
) -> tuple[lq.Entry, lq.Comment]:
    text = attestation_body(pr=pr, root=root)
    att = comment(500, text, pr=pr)
    entry = lq.Entry(
        pr=pr,
        state=state,
        phase=phase,
        request_id=request_id,
        handoff="f" * 32,
        candidate=root,
        authorization_root=root,
        attestation_comment=500,
        attestation_digest=hashlib.sha256(text.encode()).hexdigest(),
        attestation_author="maintainer",
        ci_run_id=ci_run_id,
        ci_attempt=ci_attempt,
        ci_controller=ci_controller,
        ci_head=ci_head,
        ci_base=ci_base,
    )
    return entry, att


def queued_entry(
    *, pr: int, root: str, cid: int = 500, request_id: int = 2, handoff: str = "f" * 32
) -> tuple[lq.Entry, lq.Comment]:
    text = attestation_body(pr=pr, root=root)
    att = comment(cid, text, pr=pr)
    entry = lq.Entry(
        pr=pr,
        state=lq.QUEUED,
        phase=None,
        request_id=request_id,
        handoff=handoff,
        candidate=root,
        authorization_root=root,
        attestation_comment=cid,
        attestation_digest=hashlib.sha256(text.encode()).hexdigest(),
        attestation_author="maintainer",
    )
    return entry, att


class EnvTest(unittest.TestCase):
    def setUp(self) -> None:
        self.env = mock.patch.dict(
            os.environ, {"LANDING_APP_ID": "123", "LANDING_APP_BOT": "lander[bot]"}
        )
        self.env.start()

    def tearDown(self) -> None:
        self.env.stop()


class PureStateTest(unittest.TestCase):
    def _att(self, pr: int = 1, root: str = ROOT_SHA) -> lq.Attestation:
        return lq.attestation_from_comment(
            comment(500, attestation_body(pr=pr, root=root)), REPO, pr
        )

    def _request(self, pr: int, cid: int, root: str = ROOT_SHA) -> lq.Request:
        return lq.Request("enqueue", pr, cid, root, "f" * 32, 500, "0" * 64)

    def test_fifo_dequeue_and_terminal_tail(self) -> None:
        state = lq.apply_enqueue(lq.State(), self._request(30, 1), self._att(30))
        state = lq.apply_enqueue(state, self._request(10, 2), self._att(10))
        self.assertEqual([30, 10], [e.pr for e in lq.ordered(state)])
        self.assertEqual(30, lq.active(state).pr)
        state = lq.block(state, 30, lq.BLOCKED_CI, {"run": "9"})
        self.assertEqual(
            {"run": "9", "reason": lq.BLOCKED_CI, "category": "candidate"},
            lq.find(state, 30).evidence,
        )
        self.assertEqual(10, lq.active(state).pr)
        state = lq.apply_enqueue(state, self._request(30, 3, NEW_HEAD), self._att(30, NEW_HEAD))
        self.assertEqual(
            ([10, 30], lq.QUEUED), ([e.pr for e in lq.ordered(state)], lq.find(state, 30).state)
        )
        state = lq.apply_dequeue(state, lq.Request("dequeue", 10, 4))
        self.assertEqual(
            (lq.CANCELLED, lq.ACTIVE), (lq.find(state, 10).state, lq.find(state, 30).state)
        )

    def test_queued_mutation_and_serialization(self) -> None:
        state = lq.apply_enqueue(lq.State(), self._request(99, 1), self._att(99))
        state = lq.apply_enqueue(state, self._request(1, 2), self._att())
        state = lq.apply_enqueue(state, self._request(1, 3, NEW_HEAD), self._att(root=NEW_HEAD))
        self.assertEqual(lq.BLOCKED_MUTATION, lq.find(state, 1).blocked_reason)
        entry, _ = active_entry()
        encoded = json.loads(json.dumps(lq.state_to_json(lq.State(7, (entry,)))))
        self.assertEqual(lq.State(7, (entry,)), lq.state_from_json(encoded))
        self.assertEqual(set(lq.ENTRY_KEYS), set(encoded["entries"][0]))
        self.assertNotIn("session", json.dumps(encoded))

    def test_dequeue_preserves_merging_intent(self) -> None:
        entry, _ = active_entry(phase=lq.MERGING, ci_head=ROOT_SHA)
        state = lq.State(1, (entry,))
        self.assertEqual(state, lq.apply_dequeue(state, lq.Request("dequeue", 1, 4)))
        self.assertEqual(lq.MERGING, lq.find(state, 1).phase)

    def test_active_authorization_root_cannot_be_replaced(self) -> None:
        # 新入队不得绕过 ACTIVE 所有权冲突：不同授权根必须显式阻塞，不得静默替换。
        state = lq.apply_enqueue(lq.State(), self._request(1, 1), self._att())
        with self.assertRaises(lq.QueueError):
            lq.apply_enqueue(state, self._request(1, 2, NEW_HEAD), self._att(root=NEW_HEAD))


class AttestationTest(unittest.TestCase):
    def test_attestation_and_trust_edges(self) -> None:
        lq.attestation_from_comment(comment(1, attestation_body()), REPO, 1)
        self.assertTrue(
            lq.attestation_from_comment(
                comment(1, attestation_body(review={"not-applicable": "n/a"})), REPO, 1
            ).root
        )
        for body in (
            attestation_body(actions=("squash-merge",)),
            attestation_body(repo="other/repo"),
            attestation_body(base="dev"),
            attestation_body(pr=2),
            # 佐证必须引用真实的人工确认与精确根证据，不能只写布尔/空对象。
            attestation_body(confirmation={}),
            attestation_body(confirmation={"source": "user", "quote": "  "}),
            attestation_body(check={"root": ROOT_SHA, "command": "make check", "result": "passed"}),
            attestation_body(
                check={
                    "root": NEW_HEAD,
                    "command": "make check",
                    "result": "passed",
                    "evidence": "e",
                }
            ),
            attestation_body(review={"spec": "passed", "standards": "passed"}),
            attestation_body(review={"not-applicable": "  "}),
        ):
            with self.subTest(body=body[:32]), self.assertRaises(lq.QueueError):
                lq.attestation_from_comment(comment(1, body), REPO, 1)
        self.assertFalse(lq.comment_is_trusted(comment(1, "x"), "read"))
        self.assertFalse(lq.comment_is_trusted(comment(1, "x", author_type="Bot"), "write"))
        self.assertFalse(
            lq.comment_is_trusted(comment(1, "x", updated="2024-02-01T00:00:00Z"), "write")
        )
        self.assertTrue(lq.comment_is_trusted(comment(1, "x"), "maintain"))


class ProtectionsTest(unittest.TestCase):
    def test_realistic_rulesets_and_negative_mutations(self) -> None:
        lq.verify_protections(rulesets(), "123")
        cases = {
            "disabled": rulesets(evidence_enforcement="disabled"),
            "wrong target": rulesets(evidence_refs=["refs/heads/develop"]),
            # 通配/~ALL/排除都不能被当作精确覆盖 main 的生效规则。
            "wildcard include": rulesets(evidence_refs=["refs/heads/*"]),
            "all include": rulesets(evidence_refs=["~ALL"]),
            "excluded main": rulesets(evidence_exclude=["refs/heads/*"]),
            "extra main ruleset": [
                *rulesets(),
                {
                    "id": 4,
                    "target": "branch",
                    "enforcement": "active",
                    "conditions": {"ref_name": {"include": ["refs/heads/main"], "exclude": []}},
                    "rules": [{"type": "deletion"}],
                    "bypass_actors": [],
                },
            ],
            "foreign update bypass": rulesets(
                update_bypass=[
                    {"actor_type": "Integration", "actor_id": 999, "bypass_mode": "always"}
                ]
            ),
            "foreign state bypass": rulesets(
                state_bypass=[
                    {"actor_type": "Integration", "actor_id": 999, "bypass_mode": "always"}
                ]
            ),
            "no squash": rulesets(
                evidence_rule={
                    "type": "pull_request",
                    "parameters": {
                        "allowed_merge_methods": ["merge"],
                        "required_review_thread_resolution": True,
                    },
                }
            ),
        }
        for label, value in cases.items():
            with self.subTest(case=label), self.assertRaises(lq.ProtectionError):
                lq.verify_protections(value, "123")


class PrepareTest(EnvTest):
    def _prepare(self, backend: FakeBackend, event: lq.RefreshEvent | None = None) -> str:
        with mock.patch("builtins.print") as printed:
            lq.prepare(backend, "9", "1", MAIN_HEAD, event)
        return printed.call_args_list[0].args[0]

    def _entry(self, **kwargs: object) -> FakeBackend:
        entry, att = active_entry(**kwargs)  # type: ignore[arg-type]
        return FakeBackend(state=lq.State(1, (entry,)), comments={500: att})

    def test_disabled_rules_rejected_before_refresh(self) -> None:
        backend = self._entry()
        backend.rules, backend.pr = (
            rulesets(evidence_enforcement="disabled"),
            pr_state(merge_state="BEHIND"),
        )
        with self.assertRaises(lq.ProtectionError):
            self._prepare(backend)
        self.assertEqual(([], 0), (backend.refresh_calls, backend.writes))

    def test_behind_records_intent_then_one_refresh(self) -> None:
        backend = self._entry()
        backend.pr = pr_state(merge_state="BEHIND")
        self._prepare(backend)
        self.assertEqual([(1, ROOT_SHA)], backend.refresh_calls)
        self.assertEqual(lq.REFRESHING, lq.find(backend.state_obj, 1).phase)

    def test_refreshing_same_head_no_second_request(self) -> None:
        entry, att = active_entry(phase=lq.REFRESHING)
        backend = FakeBackend(
            state=lq.State(1, (replace(entry, refresh_root=ROOT_SHA, refresh_base=BASE),)),
            comments={500: att},
            pr=pr_state(merge_state="BEHIND"),
        )
        self._prepare(backend)
        self.assertEqual(([], 0), (backend.refresh_calls, backend.writes))

    def test_controlled_lineage_requires_app_event(self) -> None:
        def backend() -> FakeBackend:
            entry, att = active_entry(phase=lq.REFRESHING)
            return FakeBackend(
                state=lq.State(1, (replace(entry, refresh_root=ROOT_SHA, refresh_base=BASE),)),
                comments={500: att},
                pr=pr_state(head=NEW_HEAD, base=BASE),
                parents={NEW_HEAD: (ROOT_SHA, BASE)},
            )

        rejected = backend()
        self._prepare(rejected)
        self.assertEqual(lq.BLOCKED_MUTATION, lq.find(rejected.state_obj, 1).blocked_reason)
        accepted = backend()
        self.assertIn(
            "run_ci=true",
            self._prepare(accepted, lq.RefreshEvent(ROOT_SHA, NEW_HEAD, "lander[bot]", 1)),
        )
        frozen = lq.find(accepted.state_obj, 1)
        self.assertEqual(
            (NEW_HEAD, ROOT_SHA, lq.TESTING),
            (frozen.candidate, frozen.authorization_root, frozen.phase),
        )

    def test_main_changed_during_refresh_no_ci(self) -> None:
        entry, att = active_entry(phase=lq.REFRESHING)
        backend = FakeBackend(
            state=lq.State(1, (replace(entry, refresh_root=ROOT_SHA, refresh_base=BASE),)),
            comments={500: att},
            pr=pr_state(head=NEW_HEAD, base=BASE),
            main=NEW_HEAD,
            parents={NEW_HEAD: (ROOT_SHA, BASE)},
        )
        self._prepare(backend, lq.RefreshEvent(ROOT_SHA, NEW_HEAD, "lander[bot]", 1))
        self.assertEqual(lq.BLOCKED_SCOPE, lq.find(backend.state_obj, 1).blocked_reason)

    def test_ready_persists_testing_with_controller(self) -> None:
        backend = self._entry()
        self.assertIn("run_ci=true", self._prepare(backend))
        frozen = lq.find(backend.state_obj, 1)
        self.assertEqual(
            (lq.TESTING, "9", MAIN_HEAD, ROOT_SHA, BASE),
            (frozen.phase, frozen.ci_run_id, frozen.ci_controller, frozen.ci_head, frozen.ci_base),
        )

    def test_testing_phase_release_and_noop(self) -> None:
        def backend(status: str) -> FakeBackend:
            entry, att = active_entry(
                phase=lq.TESTING,
                ci_run_id="9",
                ci_attempt="1",
                ci_controller=MAIN_HEAD,
                ci_head=ROOT_SHA,
                ci_base=BASE,
            )
            return FakeBackend(
                state=lq.State(1, (entry,)),
                comments={500: att},
                runs={"9": run_payload(status=status)},
            )

        in_flight = backend("in_progress")
        self._prepare(in_flight)
        self.assertEqual((lq.TESTING, 0), (lq.find(in_flight.state_obj, 1).phase, in_flight.writes))
        done = backend("completed")
        self._prepare(done)
        self.assertEqual(lq.BLOCKED_INFRA, lq.find(done.state_obj, 1).blocked_reason)

    def test_revoked_authority_blocks_before_refresh(self) -> None:
        backend = self._entry()
        backend.pr = pr_state(merge_state="BEHIND")
        backend.permissions = {"maintainer": "read"}
        self._prepare(backend)
        entry = lq.find(backend.state_obj, 1)
        self.assertEqual(lq.BLOCKED_AUTHORITY, entry.blocked_reason)
        self.assertEqual([], backend.refresh_calls)
        self.assertEqual(
            {
                "handoff",
                "reason",
                "category",
                "expected_head",
                "observed_head",
                "main",
                "run",
                "attempt",
            },
            set(entry.evidence),
        )

    def test_missing_attestation_source_blocks_as_authority(self) -> None:
        entry, att = active_entry()
        backend = FakeBackend(
            state=lq.State(1, (entry,)),
            comments={500: att},
            pr=pr_state(merge_state="BEHIND"),
        )

        def missing(comment_id: int) -> lq.Comment:
            raise lq.MissingSourceError("gh: Not Found (HTTP 404)")

        backend.comment = missing  # type: ignore[method-assign]
        self._prepare(backend)
        self.assertEqual(lq.BLOCKED_AUTHORITY, lq.find(backend.state_obj, 1).blocked_reason)
        self.assertEqual([], backend.refresh_calls)

    def test_unparseable_attestation_blocks_as_authority_and_advances(self) -> None:
        for body in (
            "no marker here",
            f"{lq.ATTESTATION_MARKER}\n```json\n{{not json}}\n```\n",
        ):
            with self.subTest(body=body[:24]):
                entry_a, _ = active_entry(pr=1)
                entry_b, _ = queued_entry(pr=2, root=NEW_HEAD, cid=501)
                backend = FakeBackend(
                    state=lq.State(1, (entry_a, entry_b)),
                    comments={500: comment(500, body, pr=1)},
                    pr=pr_state(merge_state="BEHIND"),
                )
                self._prepare(backend)
                blocked = lq.find(backend.state_obj, 1)
                self.assertEqual(lq.BLOCKED_AUTHORITY, blocked.blocked_reason)
                self.assertEqual(lq.ACTIVE, lq.find(backend.state_obj, 2).state)

    def test_pre_ci_block_evidence_persists_and_next_pr_advances(self) -> None:
        entry_a, att_a = active_entry(pr=1)
        entry_b, att_b = queued_entry(pr=2, root=NEW_HEAD, cid=501)
        backend = FakeBackend(
            state=lq.State(1, (entry_a, entry_b)),
            comments={500: att_a, 501: att_b},
            pr=pr_state(merge_state="BEHIND"),
            permissions={"maintainer": "read"},
        )
        self._prepare(backend)
        blocked = lq.find(backend.state_obj, 1)
        self.assertEqual(lq.BLOCKED_AUTHORITY, blocked.blocked_reason)
        self.assertEqual(set(lq.EVIDENCE_KEYS), set(blocked.evidence))
        self.assertEqual("none", blocked.evidence["run"])
        self.assertEqual("none", blocked.evidence["attempt"])
        # write_state 已 roundtrip；再显式重读证明持久化契约可读。
        reread = lq.state_from_json(lq.state_to_json(backend.state_obj))
        self.assertEqual(blocked, lq.find(reread, 1))
        self.assertEqual(lq.ACTIVE, lq.find(backend.state_obj, 2).state)

    def test_known_refresh_failure_releases_instead_of_sticking(self) -> None:
        backend = self._entry()
        backend.pr = pr_state(merge_state="BEHIND")

        def failing(number: int, head: str) -> None:
            raise land_pr.LandingError("gh: api failed (1): Validation Failed (HTTP 422)")

        backend.refresh = failing  # type: ignore[method-assign]
        self._prepare(backend)
        entry = lq.find(backend.state_obj, 1)
        self.assertEqual((lq.BLOCKED, lq.BLOCKED_MUTATION), (entry.state, entry.blocked_reason))

    def test_ambiguous_refresh_write_holds_the_intent(self) -> None:
        for message in (
            "cannot run gh: error connecting to api.github.com",
            "gh: api failed (1): Server Error (HTTP 500)",
        ):
            with self.subTest(message=message):
                backend = self._entry()
                backend.pr = pr_state(merge_state="BEHIND")

                def failing(number: int, head: str, _message: str = message) -> None:
                    raise land_pr.LandingError(_message)

                backend.refresh = failing  # type: ignore[method-assign]
                with self.assertRaises(lq.InfrastructureError):
                    self._prepare(backend)
                entry = lq.find(backend.state_obj, 1)
                self.assertEqual((lq.ACTIVE, lq.REFRESHING), (entry.state, entry.phase))

    def test_refresh_event_must_name_the_pr(self) -> None:
        entry, att = active_entry(phase=lq.REFRESHING)
        backend = FakeBackend(
            state=lq.State(1, (replace(entry, refresh_root=ROOT_SHA, refresh_base=BASE),)),
            comments={500: att},
            pr=pr_state(head=NEW_HEAD, base=BASE),
            parents={NEW_HEAD: (ROOT_SHA, BASE)},
        )
        self._prepare(backend, lq.RefreshEvent(ROOT_SHA, NEW_HEAD, "lander[bot]", 2))
        self.assertEqual(lq.BLOCKED_MUTATION, lq.find(backend.state_obj, 1).blocked_reason)


class FinalizeTest(EnvTest):
    def _backend(self, *, result: str = "success", **overrides: object) -> FakeBackend:
        entry, att = active_entry(
            phase=lq.TESTING,
            ci_run_id="9",
            ci_attempt="1",
            ci_controller=MAIN_HEAD,
            ci_head=ROOT_SHA,
            ci_base=BASE,
        )
        defaults: dict[str, object] = {
            "state": lq.State(1, (entry,)),
            "comments": {500: att},
            "pr": pr_state(base=BASE),
            "main": BASE,
            "trees": {ROOT_SHA: TREE, MERGE: TREE},
            "runs": {"9": run_payload()},
            "rules": rulesets(),
        }
        defaults.update(overrides)
        return FakeBackend(**defaults)  # type: ignore[arg-type]

    def _finalize(self, backend: FakeBackend, result: str = "success") -> None:
        with mock.patch("builtins.print"):
            lq.finalize(backend, 1, "9", "1", result)

    def test_valid_controller_run_merges_and_proves(self) -> None:
        backend = self._backend()
        self._finalize(backend)
        self.assertEqual([(1, ROOT_SHA)], backend.merge_calls)
        self.assertEqual(
            [(ROOT_SHA, "CI required", "success"), (ROOT_SHA, "Landing gate", "success")],
            backend.statuses,
        )
        entry = lq.find(backend.state_obj, 1)
        self.assertEqual(
            (lq.MERGED, MERGE, TREE), (entry.state, entry.merge_commit, entry.merge_tree)
        )

    def test_identity_and_rules_fail_closed_before_writes(self) -> None:
        cases = {
            "forged run": (
                self._backend(runs={"9": run_payload(head_sha=NEW_HEAD)}),
                lq.QueueError,
            ),
            # 共用元数据任一项不匹配都必须在任何 status/merge 之前 fail closed。
            "foreign repository": (
                self._backend(runs={"9": run_payload(repository={"full_name": "other/repo"})}),
                lq.QueueError,
            ),
            "wrong controller path": (
                self._backend(runs={"9": run_payload(path=".github/workflows/other.yml")}),
                lq.QueueError,
            ),
            "untrusted event": (
                self._backend(runs={"9": run_payload(event="pull_request")}),
                lq.QueueError,
            ),
            "attempt mismatch": (
                self._backend(runs={"9": run_payload(run_attempt=2)}),
                lq.QueueError,
            ),
            "rules missing": (self._backend(rules=rulesets(update_bypass=[])), lq.ProtectionError),
        }
        for label, (backend, error) in cases.items():
            with self.subTest(case=label), self.assertRaises(error):
                lq.finalize(backend, 1, "9", "1", "success")
            self.assertEqual(([], []), (backend.merge_calls, backend.statuses))

    def test_run_id_mismatch_fails_closed_before_writes(self) -> None:
        # finalize 预检的 run id 不匹配必须 fail closed，绝不发布门禁或合并。
        backend = self._backend()
        with self.assertRaises(lq.QueueError):
            lq.finalize(backend, 1, "99", "1", "success")
        self.assertEqual(([], []), (backend.merge_calls, backend.statuses))

    def test_pull_request_target_source_proof_merges_the_frozen_head(self) -> None:
        # pull_request_target 运行头属于触发 PR，既不等于也不得绑定被测 ci_head；
        # 可信来源改由 referenced_workflows 证明同仓库固定 main 的 blocking-ci.yml。
        proof = [
            {
                "sha": MAIN_HEAD,
                "ref": "refs/heads/main",
                "path": f"{REPO}/.github/workflows/blocking-ci.yml@{MAIN_HEAD}",
            }
        ]
        for trigger_head in (ROOT_SHA, NEW_HEAD):
            with self.subTest(trigger_head=trigger_head):
                backend = self._backend(
                    runs={
                        "9": run_payload(
                            event="pull_request_target",
                            head_sha=trigger_head,
                            head_branch="agent/a/demo",
                            referenced_workflows=proof,
                        )
                    }
                )
                self._finalize(backend)
                # 合并与门禁都落在冻结的 ci_head 上，而不是触发头。
                self.assertEqual([(1, ROOT_SHA)], backend.merge_calls)
                self.assertEqual(
                    [
                        (ROOT_SHA, "CI required", "success"),
                        (ROOT_SHA, "Landing gate", "success"),
                    ],
                    backend.statuses,
                )

    def test_pull_request_target_source_proof_fail_closed(self) -> None:
        good = {
            "sha": MAIN_HEAD,
            "ref": "refs/heads/main",
            "path": f"{REPO}/.github/workflows/blocking-ci.yml@{MAIN_HEAD}",
        }
        cases: dict[str, object] = {
            # 头字段与 controller 相同却没有来源证据：正是本轮要修的误判，不能放行。
            "no source proof": None,
            "not a list": {"sha": MAIN_HEAD},
            "non-dict entry": ["nope"],
            "wrong sha": [{**good, "sha": NEW_HEAD}],
            "wrong ref": [{**good, "ref": "refs/heads/feature"}],
            "foreign repository": [
                {**good, "path": f"other/repo/.github/workflows/blocking-ci.yml@{MAIN_HEAD}"}
            ],
            "wrong workflow file": [
                {**good, "path": f"{REPO}/.github/workflows/landing-queue.yml@{MAIN_HEAD}"}
            ],
        }
        for label, reference in cases.items():
            with self.subTest(case=label):
                overrides: dict[str, object] = {}
                if reference is not None:
                    overrides["referenced_workflows"] = reference
                backend = self._backend(
                    runs={
                        "9": run_payload(
                            event="pull_request_target",
                            head_sha=MAIN_HEAD,
                            head_branch="main",
                            **overrides,
                        )
                    }
                )
                with self.assertRaises(lq.QueueError):
                    lq.finalize(backend, 1, "9", "1", "success")
                self.assertEqual(([], []), (backend.merge_calls, backend.statuses))

    def test_controlled_refresh_then_pull_request_target_finalize(self) -> None:
        entry, att = active_entry(phase=lq.REFRESHING)
        backend = FakeBackend(
            state=lq.State(1, (replace(entry, refresh_root=ROOT_SHA, refresh_base=BASE),)),
            comments={500: att},
            pr=pr_state(head=NEW_HEAD, base=BASE),
            main=BASE,
            parents={NEW_HEAD: (ROOT_SHA, BASE)},
            trees={NEW_HEAD: TREE, MERGE: TREE},
            runs={
                "9": run_payload(
                    event="pull_request_target",
                    head_sha=NEW_HEAD,
                    head_branch="agent/a/demo",
                    referenced_workflows=[
                        {
                            "sha": MAIN_HEAD,
                            "ref": "refs/heads/main",
                            "path": f"{REPO}/.github/workflows/blocking-ci.yml@{MAIN_HEAD}",
                        }
                    ],
                )
            },
            merge_result=MERGE,
        )
        # 受控 refresh 事件冻结新头与源码 CI controller，并持久化 TESTING intent；
        # 已有 REFRESHING intent，因此不再发起第二次 refresh。
        with mock.patch("builtins.print"):
            lq.prepare(
                backend, "9", "1", MAIN_HEAD, lq.RefreshEvent(ROOT_SHA, NEW_HEAD, "lander[bot]", 1)
            )
        frozen = lq.find(backend.state_obj, 1)
        self.assertEqual(
            (lq.TESTING, NEW_HEAD, MAIN_HEAD),
            (frozen.phase, frozen.ci_head, frozen.ci_controller),
        )
        self.assertEqual([], backend.refresh_calls)
        reread = lq.state_from_json(lq.state_to_json(backend.state_obj))
        self.assertEqual(frozen, lq.find(reread, 1))
        # PR-target 头正是新头时仍以冻结 ci_head 精确合并；不重复 refresh 或 CI。
        with mock.patch("builtins.print"):
            lq.finalize(backend, 1, "9", "1", "success")
        self.assertEqual([(1, NEW_HEAD)], backend.merge_calls)
        self.assertEqual(
            [(NEW_HEAD, "CI required", "success"), (NEW_HEAD, "Landing gate", "success")],
            backend.statuses,
        )
        merged = lq.find(backend.state_obj, 1)
        self.assertEqual((lq.MERGED, "9"), (merged.state, merged.ci_run_id))

    def test_ci_job_classification_uses_real_reusable_names(self) -> None:
        running = job("Publish gates and merge", None, status="in_progress")
        checks = "Single-flight blocking CI / Repository checks"
        cases = {
            "candidate failure": (job(checks, "failure"), lq.BLOCKED_CI),
            "matrix failure": (job(f"{checks} (policy)", "failure"), lq.BLOCKED_CI),
            "runner cancelled": (job(checks, "cancelled"), lq.BLOCKED_INFRA),
            "matrix timeout": (
                job("Single-flight blocking CI / center-integration evidence", "timed_out"),
                lq.BLOCKED_INFRA,
            ),
        }
        for label, (failing, reason) in cases.items():
            with self.subTest(case=label):
                backend = self._backend(jobs=ci_jobs(failing, running))
                self._finalize(backend, "failure")
                entry = lq.find(backend.state_obj, 1)
                self.assertEqual(reason, entry.blocked_reason)
                self.assertEqual(reason, entry.evidence["reason"])
                self.assertEqual([], backend.merge_calls)

    def test_classifications_block_with_evidence(self) -> None:
        cases = {
            "infra timeout": (self._backend(), "timed_out", lq.BLOCKED_INFRA),
            "main moved": (self._backend(main=NEW_HEAD), "success", lq.BLOCKED_SCOPE),
            "nonhead": (
                self._backend(pr=pr_state(head=NEW_HEAD, base=BASE)),
                "success",
                lq.BLOCKED_MUTATION,
            ),
            "deleted authority": (
                self._backend(permissions={"maintainer": "read"}),
                "success",
                lq.BLOCKED_AUTHORITY,
            ),
        }
        for label, (backend, result, reason) in cases.items():
            with self.subTest(case=label):
                self._finalize(backend, result)
                entry = lq.find(backend.state_obj, 1)
                self.assertEqual(reason, entry.blocked_reason)
                self.assertEqual(reason, entry.evidence["reason"])
                self.assertEqual([], backend.merge_calls)

    def test_main_move_revokes_and_dequeue_never_merges(self) -> None:
        moved = self._backend(main=NEW_HEAD)
        self._finalize(moved)
        self.assertIn((ROOT_SHA, "Landing gate", "failure"), moved.statuses)
        queued = self._backend()
        queued.comments[900] = request_comment(900, "dequeue")
        self._finalize(queued)
        self.assertEqual(
            (
                [],
                [
                    (ROOT_SHA, "CI required", "failure"),
                    (ROOT_SHA, "Landing gate", "failure"),
                ],
            ),
            (queued.merge_calls, queued.statuses),
        )

    def test_merge_failure_revokes_landing_gate(self) -> None:
        backend = self._backend(merge_error=land_pr.LandingError("boom"))
        with mock.patch("builtins.print"), self.assertRaises(lq.InfrastructureError):
            lq.finalize(backend, 1, "9", "1", "success")
        self.assertIn((ROOT_SHA, "Landing gate", "failure"), backend.statuses)

    def test_prove_rejects_wrong_head_and_recovers_from_prepare(self) -> None:
        wrong = FakeBackend(
            state=lq.State(1, (active_entry(phase=lq.MERGING, ci_head=ROOT_SHA)[0],)),
            comments={500: active_entry()[1]},
            pr=pr_state(state="MERGED", head=NEW_HEAD, merge_commit=MERGE),
            main=MERGE,
        )
        with self.assertRaises(lq.InfrastructureError):
            lq.finalize(wrong, 1, "9", "1", "success")
        crash = FakeBackend(
            state=lq.State(1, (active_entry(phase=lq.MERGING, ci_head=ROOT_SHA)[0],)),
            comments={500: active_entry()[1]},
            pr=pr_state(state="MERGED", merge_commit=MERGE),
            main=MERGE,
            trees={ROOT_SHA: TREE, MERGE: TREE},
        )
        with mock.patch("builtins.print"):
            lq.prepare(crash, "9", "1", MAIN_HEAD, None)
        self.assertEqual((lq.MERGED, []), (lq.find(crash.state_obj, 1).state, crash.merge_calls))

    def test_block_evidence_is_complete(self) -> None:
        backend = self._backend(pr=pr_state(head=NEW_HEAD, base=BASE))
        self._finalize(backend)
        entry = lq.find(backend.state_obj, 1)
        self.assertEqual(lq.BLOCKED_MUTATION, entry.blocked_reason)
        self.assertEqual(
            {
                "handoff",
                "reason",
                "category",
                "expected_head",
                "observed_head",
                "main",
                "run",
                "attempt",
            },
            set(entry.evidence),
        )
        self.assertEqual(ROOT_SHA, entry.evidence["expected_head"])
        self.assertEqual(NEW_HEAD, entry.evidence["observed_head"])
        self.assertEqual(BASE, entry.evidence["main"])
        self.assertEqual("9", entry.evidence["run"])


class FifoOrchestrationTest(EnvTest):
    def _prepare(
        self, backend: FakeBackend, run_id: str, attempt: str, event: object = None
    ) -> str:
        with mock.patch("builtins.print") as printed:
            lq.prepare(backend, run_id, attempt, MAIN_HEAD, event)
        return "\n".join(call.args[0] for call in printed.call_args_list if call.args)

    def test_two_pr_fifo_single_flight_refresh_and_merge(self) -> None:
        a_root, a_head = ROOT_SHA, NEW_HEAD
        b_root, b_head = "1" * 40, "2" * 40
        merge_a, merge_b = MERGE, "3" * 40
        base0 = BASE
        att_a = comment(500, attestation_body(pr=1, root=a_root), pr=1)
        att_b = comment(501, attestation_body(pr=2, root=b_root), pr=2)
        backend = FakeBackend(
            comments={
                500: att_a,
                501: att_b,
                1: enqueue_request(1, 1, a_root, att_a, 500),
                2: enqueue_request(2, 2, b_root, att_b, 501),
            },
            prs={
                1: pr_state(number=1, head=a_root, base=base0, merge_state="BEHIND"),
                2: pr_state(number=2, head=b_root, base=base0, merge_state="CLEAN"),
            },
            main=base0,
            parents={a_head: (a_root, base0), b_head: (b_root, merge_a)},
            trees={a_head: TREE, merge_a: TREE, b_head: TREE, merge_b: TREE},
            runs={"9": run_payload(), "11": run_payload()},
        )

        # 1. A/B 入队：A BEHIND 只 refresh A；B 保持 QUEUED，无 refresh/CI。
        first = self._prepare(backend, "7", "1")
        self.assertIn("run_ci=false", first)
        self.assertEqual([(1, a_root)], backend.refresh_calls)
        entry_a = lq.find(backend.state_obj, 1)
        self.assertEqual((lq.ACTIVE, lq.REFRESHING), (entry_a.state, entry_a.phase))
        self.assertEqual(lq.QUEUED, lq.find(backend.state_obj, 2).state)

        # 2. A 受控 refresh 后新头进入 TESTING 一次；B 仍无 CI。
        backend.prs[1] = pr_state(number=1, head=a_head, base=base0, merge_state="CLEAN")
        second = self._prepare(backend, "9", "1", lq.RefreshEvent(a_root, a_head, "lander[bot]", 1))
        self.assertIn("run_ci=true", second)
        self.assertEqual(lq.TESTING, lq.find(backend.state_obj, 1).phase)
        self.assertEqual(lq.QUEUED, lq.find(backend.state_obj, 2).state)
        self.assertEqual([(1, a_root)], backend.refresh_calls)

        # 3. 重复唤醒命中 TESTING 不重跑 CI；单飞保持到旧运行结束。
        backend.runs["9"] = run_payload(status="in_progress")
        third = self._prepare(backend, "9", "1")
        self.assertIn("run_ci=false", third)
        self.assertEqual(lq.TESTING, lq.find(backend.state_obj, 1).phase)
        self.assertEqual(lq.QUEUED, lq.find(backend.state_obj, 2).state)

        # 4. A 精确头合并并证明；B 晋升 ACTIVE。
        backend.runs["9"] = run_payload()
        backend.merge_result = merge_a
        with mock.patch("builtins.print"):
            lq.finalize(backend, 1, "9", "1", "success")
        self.assertEqual([(1, a_head)], backend.merge_calls)
        self.assertEqual(lq.MERGED, lq.find(backend.state_obj, 1).state)
        self.assertEqual(lq.ACTIVE, lq.find(backend.state_obj, 2).state)

        # 5. main 前进后 B BEHIND，只 refresh B 一次。
        backend.prs[2] = pr_state(number=2, head=b_root, base=base0, merge_state="BEHIND")
        fourth = self._prepare(backend, "10", "1")
        self.assertIn("run_ci=false", fourth)
        self.assertEqual([(1, a_root), (2, b_root)], backend.refresh_calls)
        self.assertEqual(lq.REFRESHING, lq.find(backend.state_obj, 2).phase)

        # 6. B 受控 refresh 后新头进入 TESTING 一次，再精确合并证明。
        backend.prs[2] = pr_state(number=2, head=b_head, base=merge_a, merge_state="CLEAN")
        fifth = self._prepare(backend, "11", "1", lq.RefreshEvent(b_root, b_head, "lander[bot]", 2))
        self.assertIn("run_ci=true", fifth)
        self.assertEqual(lq.TESTING, lq.find(backend.state_obj, 2).phase)
        backend.merge_result = merge_b
        with mock.patch("builtins.print"):
            lq.finalize(backend, 2, "11", "1", "success")
        self.assertEqual([(1, a_head), (2, b_head)], backend.merge_calls)
        self.assertEqual(lq.MERGED, lq.find(backend.state_obj, 2).state)


class ConsumeTest(EnvTest):
    def test_outsider_and_replay_do_not_wedge(self) -> None:
        entry, att = active_entry(state=lq.BLOCKED)
        entry = replace(entry, blocked_reason=lq.BLOCKED_CI)
        digest = hashlib.sha256(att.body.encode()).hexdigest()
        backend = FakeBackend(
            state=lq.State(1, (entry,)),
            comments={
                1: request_comment(1, "enqueue", attestation_digest=digest),
                500: att,
                2: request_comment(2, "enqueue", attestation_digest=digest, author="outsider"),
            },
            permissions={"maintainer": "write", "outsider": "read"},
        )
        with mock.patch("builtins.print"):
            lq.prepare(backend, "9", "1", MAIN_HEAD, None)
        self.assertEqual(
            (lq.BLOCKED_CI, 2),
            (lq.find(backend.state_obj, 1).blocked_reason, backend.state_obj.consumed_request_id),
        )

    def test_queued_enqueue_is_activated(self) -> None:
        att = comment(500, attestation_body())
        digest = hashlib.sha256(att.body.encode()).hexdigest()
        backend = FakeBackend(
            comments={500: att, 1: request_comment(1, "enqueue", attestation_digest=digest)}
        )
        with mock.patch("builtins.print") as printed:
            lq.prepare(backend, "9", "1", MAIN_HEAD, None)
        self.assertEqual(lq.ACTIVE, lq.find(backend.state_obj, 1).state)
        self.assertIn("run_ci=true", printed.call_args_list[0].args[0])

    def test_missing_attestation_request_skips_and_advances(self) -> None:
        _, att_b = queued_entry(pr=2, root=NEW_HEAD, cid=501)
        backend = FakeBackend(
            comments={
                1: request_comment(1, "enqueue", pr=1, attestation_digest="0" * 64),
                2: enqueue_request(2, 2, NEW_HEAD, att_b, 501),
                501: att_b,
            },
            prs={2: pr_state(number=2, head=NEW_HEAD)},
        )

        def lookup_comment(comment_id: int) -> lq.Comment:
            if comment_id == 500:
                raise lq.MissingSourceError("attestation comment 500 no longer exists")
            return backend.comments[comment_id]

        backend.comment = lookup_comment  # type: ignore[method-assign]
        result = lq._consume(backend, lq.State())
        self.assertEqual(2, result.consumed_request_id)
        self.assertEqual(lq.ACTIVE, lq.find(result, 2).state)

        transport = FakeBackend(
            comments={1: request_comment(1, "enqueue", attestation_digest="0" * 64)}
        )

        def boom(comment_id: int) -> lq.Comment:
            raise lq.InfrastructureError("gh: error connecting to api.github.com")

        transport.comment = boom  # type: ignore[method-assign]
        with self.assertRaises(lq.InfrastructureError):
            lq._consume(transport, lq.State())

    def test_dequeue_revokes_gates_before_state_write(self) -> None:
        entry, att = active_entry(
            phase=lq.TESTING,
            ci_run_id="9",
            ci_attempt="1",
            ci_controller=MAIN_HEAD,
            ci_head=ROOT_SHA,
            ci_base=BASE,
        )
        backend = FakeBackend(
            state=lq.State(1, (entry,)),
            comments={500: att, 900: request_comment(900, "dequeue")},
        )
        with mock.patch("builtins.print"):
            lq.prepare(backend, "9", "1", MAIN_HEAD, None)
        self.assertEqual(lq.CANCELLED, lq.find(backend.state_obj, 1).state)
        self.assertEqual(([], []), (backend.refresh_calls, backend.merge_calls))
        status_indexes = [i for i, e in enumerate(backend.events) if e[0] == "status"]
        write_indexes = [i for i, e in enumerate(backend.events) if e[0] == "write"]
        self.assertTrue(status_indexes and write_indexes)
        self.assertLess(max(status_indexes), min(write_indexes))
        self.assertEqual(
            [(ROOT_SHA, "CI required", "failure"), (ROOT_SHA, "Landing gate", "failure")],
            backend.statuses,
        )

    def test_queued_root_replacement_block_has_complete_evidence(self) -> None:
        entry_a, att_a = active_entry(pr=1)
        entry_b, att_b = queued_entry(pr=2, root=ROOT_SHA, cid=501)
        new_att = comment(502, attestation_body(pr=2, root=NEW_HEAD), pr=2)
        backend = FakeBackend(
            state=lq.State(1, (entry_a, entry_b)),
            comments={
                500: att_a,
                501: att_b,
                502: new_att,
                3: enqueue_request(3, 2, NEW_HEAD, new_att, 502),
            },
            prs={2: pr_state(number=2, head=NEW_HEAD)},
        )
        result = lq._consume(backend, backend.state_obj)
        blocked = lq.find(result, 2)
        self.assertEqual(lq.BLOCKED_MUTATION, blocked.blocked_reason)
        self.assertEqual(set(lq.EVIDENCE_KEYS), set(blocked.evidence))
        self.assertEqual("none", blocked.evidence["run"])


class VerifyMainTest(unittest.TestCase):
    def test_bootstrap_pending_and_arbitrary_push(self) -> None:
        with mock.patch("builtins.print"):
            self.assertEqual(0, lq.verify_main(FakeBackend(), MAIN_HEAD))
        entry, _ = active_entry()
        backend = FakeBackend(
            state=lq.State(
                1, (replace(entry, state=lq.MERGED, merge_commit=MERGE, merge_tree=TREE),)
            ),
            trees={MERGE: TREE},
        )
        with self.assertRaises(lq.InfrastructureError):
            lq.verify_main(backend, NEW_HEAD)
        with mock.patch("builtins.print"):
            self.assertEqual(0, lq.verify_main(backend, MERGE))


class GitHubAdapterTest(unittest.TestCase):
    def test_pull_request_fields_and_slurped_inbox(self) -> None:
        payload = {
            "state": "OPEN",
            "isDraft": False,
            "baseRefName": "main",
            "baseRefOid": BASE,
            "headRefName": "agent/a/demo",
            "headRefOid": ROOT_SHA,
            "headRepository": {"nameWithOwner": REPO},
            "mergeStateStatus": "CLEAN",
            "mergeable": "MERGEABLE",
            "statusCheckRollup": [
                {"name": "CI required", "status": "COMPLETED", "conclusion": "SUCCESS"}
            ],
            "reviewDecision": "APPROVED",
            "mergeCommit": None,
        }
        seen: list[list[str]] = []

        def runner(arguments: list[str]) -> subprocess.CompletedProcess[str]:
            seen.append(list(arguments))
            return subprocess.CompletedProcess(arguments, 0, json.dumps(payload), "")

        with mock.patch("land_pr.run_raw", side_effect=runner):
            state = lq.GitHub().pull_request(1)
        fields = seen[0][seen[0].index("--json") + 1]
        for field in (
            "baseRefOid",
            "mergeStateStatus",
            "mergeable",
            "mergeCommit",
            "headRepository",
        ):
            self.assertIn(field, fields)
        self.assertEqual(
            (BASE, ROOT_SHA, REPO), (state.base_oid, state.head_oid, state.head_repository)
        )

        os.environ["LANDING_QUEUE_ISSUE"] = "7"
        self.addCleanup(os.environ.pop, "LANDING_QUEUE_ISSUE", None)
        item = {
            "id": 11,
            "body": lq.request_body(lq.Request("dequeue", 1, 0)),
            "issue_url": "u",
            "created_at": "t",
            "updated_at": "t",
            "user": {"login": "m", "type": "User"},
        }
        pages = [[item, {**item, "id": 12}], [{**item, "id": 13}]]

        def page_runner(arguments: list[str]) -> subprocess.CompletedProcess[str]:
            if arguments[:2] == ["repo", "view"]:
                return subprocess.CompletedProcess(
                    arguments, 0, json.dumps({"nameWithOwner": REPO}), ""
                )
            return subprocess.CompletedProcess(arguments, 0, json.dumps(pages), "")

        self.assertEqual([11, 12, 13], [c.id for c in lq.GitHub(runner=page_runner).inbox()])

    def test_post_request_passes_body_with_explicit_field_flag(self) -> None:
        os.environ["LANDING_QUEUE_ISSUE"] = "7"
        self.addCleanup(os.environ.pop, "LANDING_QUEUE_ISSUE", None)
        seen: list[list[str]] = []

        def runner(arguments: list[str]) -> subprocess.CompletedProcess[str]:
            seen.append(list(arguments))
            if arguments[:2] == ["repo", "view"]:
                return subprocess.CompletedProcess(
                    arguments, 0, json.dumps({"nameWithOwner": REPO}), ""
                )
            return subprocess.CompletedProcess(arguments, 0, json.dumps({"id": 55}), "")

        self.assertEqual(55, lq.GitHub(runner=runner).post_request("hello"))
        api_call = next(call for call in seen if call[:1] == ["api"])
        self.assertEqual(
            ["api", "--method", "POST", f"repos/{REPO}/issues/7/comments", "-f", "body=hello"],
            api_call,
        )

    def test_missing_source_is_normalized_and_transport_stays_infrastructure(self) -> None:
        os.environ["LANDING_QUEUE_ISSUE"] = "7"
        self.addCleanup(os.environ.pop, "LANDING_QUEUE_ISSUE", None)
        stderr = [""]

        def runner(arguments: list[str]) -> subprocess.CompletedProcess[str]:
            if arguments[:2] == ["repo", "view"]:
                return subprocess.CompletedProcess(
                    arguments, 0, json.dumps({"nameWithOwner": REPO}), ""
                )
            return subprocess.CompletedProcess(arguments, 1, "", stderr[0])

        for message, error in (
            ("gh: Not Found (HTTP 404)", lq.MissingSourceError),
            ("gh: Server Error (HTTP 500)", lq.InfrastructureError),
        ):
            stderr[0] = message
            with self.subTest(stderr=message), self.assertRaises(error):
                lq.GitHub(runner=runner).comment(9)

        for message, error in (
            ("not found (HTTP 404)", lq.MissingSourceError),
            ("server error (HTTP 500)", lq.InfrastructureError),
        ):
            stderr[0] = ""
            with (
                self.subTest(message=message),
                mock.patch("land_pr.pull_request", side_effect=land_pr.LandingError(message)),
                self.assertRaises(error),
            ):
                lq.GitHub(runner=runner).pull_request(5)


class BindingEnqueueTest(EnvTest):
    def setUp(self) -> None:
        super().setUp()
        self.temp_dir = tempfile.TemporaryDirectory(prefix="landing-queue-")
        self.root = Path(self.temp_dir.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git_env = os.environ.copy()
        self.git_env.update(
            {
                "GIT_CONFIG_GLOBAL": str(self.root / "global"),
                "GIT_CONFIG_NOSYSTEM": "1",
                "HOME": str(self.root),
            }
        )
        self._git("init", "--quiet", "-b", "main")
        self._git("config", "user.name", "Landing test")
        self._git("config", "user.email", "landing@example.invalid")
        (self.repo / ".gitignore").write_text(".nvsop/\n", encoding="utf-8")
        (self.repo / "base.txt").write_text("base\n", encoding="utf-8")
        self._git("add", ".gitignore", "base.txt")
        self._git("commit", "--quiet", "-m", "base")
        self.worktree = self.root / "task"
        self._git("worktree", "add", "--quiet", "-b", "agent/a/demo", str(self.worktree))
        self.head = self._git("rev-parse", "HEAD", cwd=self.worktree)
        self._previous = Path.cwd()
        os.chdir(self.worktree)
        from bind_task_session import bind

        bind("landing-test-session")

    def tearDown(self) -> None:
        os.chdir(self._previous)
        self.temp_dir.cleanup()
        super().tearDown()

    def _git(self, *arguments: str, cwd: Path | None = None) -> str:
        result = subprocess.run(
            ["git", *arguments],
            cwd=cwd or self.repo,
            env=self.git_env,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            self.fail(result.stderr or result.stdout)
        return result.stdout.strip()

    def test_enqueue_posts_opaque_request_and_preserves_binding(self) -> None:
        text = attestation_body(pr=123, root=self.head)
        backend = FakeBackend(
            pr=pr_state(number=123, head=self.head),
            comments={500: comment(500, text, pr=123)},
            main=BASE,
        )
        binding = (self.worktree / ".nvsop" / "session-binding.json").read_bytes()
        with mock.patch("builtins.print"):
            self.assertEqual(0, lq.enqueue(backend, 123, self.head, 500))
        self.assertIn(lq.REQUEST_MARKER, backend.posted[0])
        self.assertNotIn("landing-test-session", backend.posted[0])
        self.assertNotIn(str(self.worktree), backend.posted[0])
        self.assertEqual(binding, (self.worktree / ".nvsop" / "session-binding.json").read_bytes())


class LandingWorkflowTest(unittest.TestCase):
    def test_triggers_mutex_trusted_checkout_and_commands(self) -> None:
        text = (ROOT / ".github" / "workflows" / "landing-queue.yml").read_text(encoding="utf-8")
        for trigger in ("issue_comment:", "pull_request_target:", "workflow_dispatch:", "push:"):
            self.assertIn(trigger, text)
        self.assertNotIn("pull_request_review:", text)
        self.assertNotIn("schedule:", text)
        for required in (
            "group: landing-queue",
            "cancel-in-progress: false",
            # 同提交相对路径引用可复用 CI：运行头与源码提交绑定在同一 workflow_sha。
            "uses: ./.github/workflows/blocking-ci.yml",
            "ref: ${{ github.workflow_sha }}",
            "--controller-sha",
            "actions/create-github-app-token@fee1f7d63c2ff003460e3d139729b119787bc349",
            "environment: landing",
            "scripts/landing_queue.py prepare",
            "scripts/landing_queue.py finalize",
            "scripts/landing_queue.py verify-main",
        ):
            self.assertIn(required, text)
        # 特权环境只属于控制器作业；可复用 CI 作业不继承环境或 App secret。
        ci_job = text.split("\n  ci:\n", 1)[1].split("\n  finalize:\n", 1)[0]
        self.assertNotIn("environment: landing", ci_job)
        self.assertNotIn("LANDING_APP_PRIVATE_KEY", ci_job)

    def test_app_jobs_are_guarded_until_the_landing_app_is_configured(self) -> None:
        text = (ROOT / ".github" / "workflows" / "landing-queue.yml").read_text(encoding="utf-8")
        # 部署前守卫：未配置 App 变量时整个控制器 no-op，不请求 environment/secret。
        guard = text.split("\n  guard:\n", 1)[1].split("\n  prepare:\n", 1)[0]
        self.assertIn("vars.LANDING_APP_ID != ''", guard)
        self.assertIn("vars.LANDING_QUEUE_ISSUE != ''", guard)
        self.assertIn("github.ref == 'refs/heads/main'", guard)
        self.assertNotIn("environment: landing", guard)
        self.assertIn("needs: guard", text)
        self.assertIn("needs.guard.outputs.enabled == 'true'", text)
        # prepare 与 integrity 都直接依赖 guard，配置缺失时不会落到特权作业。
        prepare = text.split("\n  prepare:\n", 1)[1].split("\n  ci:\n", 1)[0]
        integrity = text.split("\n  integrity:\n", 1)[1]
        for body in (prepare, integrity):
            self.assertIn("needs: guard", body)
            self.assertIn("needs.guard.outputs.enabled == 'true'", body)


if __name__ == "__main__":
    unittest.main()
