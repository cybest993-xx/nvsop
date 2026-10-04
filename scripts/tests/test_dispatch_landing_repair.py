from __future__ import annotations

import contextlib
import io
import json
import os
import re
import sys
import threading
import unittest
from dataclasses import replace
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(HERE))

import test_landing_queue as tq  # noqa: E402

import dispatch_landing_repair as dr  # noqa: E402
import landing_queue as lq  # noqa: E402

TOKEN = re.compile(r"claim=([0-9a-f]{32})")
RESUMED = dr.ResumeOutcome.RESUMED


class SpyBridge:
    """契约替身：证明 exact session 与 handoff ack，不代表真实 live wake。"""

    def __init__(self, *, verified: bool = True, outcome: dr.ResumeOutcome = RESUMED) -> None:
        self.verified_flag, self.outcome, self.resumed, self.handoffs = verified, outcome, [], []

    def verify(self, request: dr.ResumeRequest) -> bool:
        return self.verified_flag

    def resume(self, request: dr.ResumeRequest) -> dr.ResumeOutcome:
        self.resumed.append(request.session_id)
        self.handoffs.append(dict(request.handoff))
        return self.outcome


class BrokenBridge:
    def verify(self, request: dr.ResumeRequest) -> bool:
        raise RuntimeError("host transport lost")

    def resume(self, request: dr.ResumeRequest) -> dr.ResumeOutcome:
        raise AssertionError("must not resume after failed verify")


class SyncBackend(tq.FakeBackend):
    """dispatch_wake 在返回前消费已发布请求，模拟控制器在 CLI 重新读取前完成 CAS。"""

    _consumed = 0

    def dispatch_wake(self) -> bool:
        for body in self.posted[self._consumed :]:
            cid = max(self.comments, default=0) + 1
            self.comments[cid] = tq.comment(cid, body, pr=1)
            self.write_state(lq._consume(self, self.state_obj), self.revision)
        self._consumed = len(self.posted)
        return True


class DeferredBackend(tq.FakeBackend):
    """dispatch_wake 只把请求写入 inbox、不消费：真实异步控制器与 CLI 读取分离。"""

    def dispatch_wake(self) -> bool:
        cid = max(self.comments, default=0) + 1
        self.comments[cid] = tq.comment(cid, self.posted[-1], pr=1)
        return True


class BarrierBackend(SyncBackend):
    """arm 后前 N 次 state() 读取在 barrier 同步，复现同 token 并发进入 host 阶段的竞态。"""

    _lock: threading.Lock | None = None
    _barrier: threading.Barrier | None = None
    _remaining = 0

    def arm(self, parties: int = 2) -> None:
        self._lock = threading.Lock()
        self._barrier = threading.Barrier(parties)
        self._remaining = parties

    def state(self) -> tuple[lq.State, str]:
        result = super().state()
        lock, barrier = self._lock, self._barrier
        if lock is None or barrier is None:
            return result
        with lock:
            wait = self._remaining > 0
            self._remaining = max(0, self._remaining - 1)
        if wait:
            barrier.wait(timeout=5)
        return result


class RepairDispatchTest(tq.GitTaskFixture):
    """真实临时 Git worktree + 绑定 + receipt 的派发 dryrun。"""

    SESSION_ID = "repair-owner-session"

    def setUp(self) -> None:
        super().setUp()
        self.handoff = "f" * 32
        lq.local_receipt(self.handoff, 1, self.head, "agent/a/demo")

    def _blocked(self, run: str = "9", backend_cls: type = SyncBackend) -> None:
        entry, att = tq.active_entry(root=self.head)
        fields = ("handoff", "expected_head", "observed_head", "main", "run", "attempt")
        evidence = dict(
            zip(fields, (self.handoff, self.head, self.head, tq.BASE, run, "1"), strict=True)
        )
        state = lq.block(
            lq.State(1, (replace(entry, handoff=self.handoff),)), 1, lq.BLOCKED_CI, evidence
        )
        self.att = att
        self.backend = backend_cls(
            state=state, comments={500: att}, pr=tq.pr_state(head=self.head), main=tq.BASE
        )

    def _consume(self) -> None:
        self.backend.state_obj = lq._consume(self.backend, self.backend.state_obj)

    def _run(self, call: object) -> tuple[int, str]:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = call()  # type: ignore[operator]
        return code, buffer.getvalue()

    def _claim(self) -> str:
        code, out = self._run(lambda: dr.claim(self.backend, 1))
        self.assertEqual(0, code)
        return TOKEN.search(out).group(1)  # type: ignore[union-attr]

    def _resume(self, host: object, token: str) -> tuple[int, str]:
        return self._run(lambda: dr.resume(self.backend, 1, host, token))

    def _request(self, kind: str, rid: int, token: str) -> lq.Request:
        entry = lq.find(self.backend.state_obj, 1)
        request = lq.Request(kind, 1, rid, handoff=entry.handoff, claim=lq.claim_digest(token))
        return replace(request, generation=entry.evidence["repair_generation"])

    def _enqueue_request(self, token: str, cid: int = 60) -> lq.Request:
        entry = lq.find(self.backend.state_obj, 1)
        return lq.Request(
            "enqueue", 1, cid, self.head, self.handoff, 500, entry.attestation_digest, claim=token
        )

    def _attestation(self) -> lq.Attestation:
        return lq.attestation_from_comment(self.att, tq.REPO, 1)

    def test_async_backend_pending_then_explicit_rerun_reaches_host_once(self) -> None:
        # 真实异步 GitHub.dispatch_wake 不消费请求：首次 resume 只能 pending，显式 rerun 才调 host。
        self._blocked(backend_cls=DeferredBackend)
        spy, token = SpyBridge(), self._claim()
        self._consume()
        code, out = self._resume(spy, token)
        self.assertEqual((0, [], True), (code, spy.resumed, "pending-resume" in out))
        self.assertEqual("claimed", lq.find(self.backend.state_obj, 1).evidence["repair_state"])
        self._consume()
        self.assertEqual("resuming", lq.find(self.backend.state_obj, 1).evidence["repair_state"])
        code, out = self._resume(spy, token)
        self.assertEqual((0, [self.SESSION_ID], True), (code, spy.resumed, "resumed" in out))

    def test_concurrent_same_token_resume_calls_host_at_most_once(self) -> None:
        self._blocked(backend_cls=BarrierBackend)
        token = self._claim()
        self.backend.state_obj = lq.apply_repair(
            self.backend.state_obj, self._request(lq.REPAIR_RESUMING, 90, token)
        )
        spy = SpyBridge()
        self.backend.arm(2)
        results: list[int] = []
        threads = [
            threading.Thread(target=lambda: results.append(dr.resume(self.backend, 1, spy, token)))
            for _ in range(2)
        ]
        with contextlib.redirect_stdout(io.StringIO()):
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)
        self.assertEqual([self.SESSION_ID], spy.resumed)
        self.assertEqual([0, 1], sorted(results))

    def test_uncertain_or_crashed_resume_preserves_marker(self) -> None:
        self._blocked()
        token = self._claim()
        landing = self.worktree / ".nvsop" / "artifacts" / "landing"
        code, out = self._resume(BrokenBridge(), token)
        self.assertEqual((1, True), (code, "uncertain" in out))
        self.assertEqual(1, len(list(landing.glob("*.resume"))))
        # marker 已预占：重复调用只能 in-progress，不得移除 marker 或第二次调 host。
        spy = SpyBridge()
        code, out = self._resume(spy, token)
        self.assertEqual((1, [], True), (code, spy.resumed, "in-progress" in out))
        self.assertEqual(1, len(list(landing.glob("*.resume"))))

    def test_wrong_token_in_resuming_fails_closed_without_host(self) -> None:
        self._blocked()
        token = self._claim()
        self.backend.state_obj = lq.apply_repair(
            self.backend.state_obj, self._request(lq.REPAIR_RESUMING, 90, token)
        )
        spy = SpyBridge()
        with self.assertRaises(dr.DispatchError):
            dr.resume(self.backend, 1, spy, "0" * 32)
        self.assertEqual([], spy.resumed)
        self.assertEqual(
            [], list((self.worktree / ".nvsop" / "artifacts" / "landing").glob("*.resume"))
        )

    def test_claim_digest_is_public_but_plaintext_token_is_not(self) -> None:
        self._blocked()
        event = lq.repair_event(self.backend, 1)
        self.assertEqual(lq.STATE_BRANCH, event["evidence_location"]["state_branch"])
        token = self._claim()
        self.assertNotIn(token, self.backend.posted[-1])
        self.assertIn(lq.claim_digest(token), self.backend.posted[-1])
        entry = lq.find(self.backend.state_obj, 1)
        self.assertEqual(lq.claim_digest(token), entry.evidence["repair_claim"])
        code, _ = self._resume(dr.UnavailableHostBridge(), token)
        entry = lq.find(self.backend.state_obj, 1)
        self.assertEqual(
            (0, lq.BLOCKED, "unavailable"), (code, entry.state, entry.evidence["repair_state"])
        )
        # 任何公开请求体（claim/resuming/result）都只含 digest，绝不含明文 token。
        for body in self.backend.posted:
            self.assertNotIn(token, body)
        public = json.dumps(lq.state_to_json(self.backend.state_obj), sort_keys=True)
        for key in ("session", "worktree", token):
            self.assertNotIn(key, public)
        self.assertNotIn(token, json.dumps(lq.repair_event(self.backend, 1)))
        for name in ("session-binding.json", f"{self.handoff}.json"):
            self.assertTrue((self.worktree / ".nvsop").rglob(name).__next__().is_file())

    def test_resuming_is_single_flight_and_blocks_enqueue(self) -> None:
        self._blocked()
        token = self._claim()
        resuming = lq.apply_repair(
            self.backend.state_obj, self._request(lq.REPAIR_RESUMING, 90, token)
        )
        self.backend.state_obj = resuming
        with self.assertRaises(lq.QueueError):
            lq.apply_enqueue(
                resuming, self._enqueue_request(lq.claim_digest(token), 99), self._attestation()
            )
        spy = SpyBridge()
        code, out = self._run(lambda: dr.resume(self.backend, 1, spy, token))
        # 控制器已确认 resuming：显式 rerun 的 winner 继续并调 host 一次。
        self.assertEqual((0, [self.SESSION_ID], True), (code, spy.resumed, "resumed" in out))
        code, out = self._run(lambda: dr.resume(self.backend, 1, spy, token))
        self.assertEqual((True, 1), ("already-resumed" in out, len(spy.resumed)))

    def test_spy_resume_then_reenqueue_and_reblock_cycle(self) -> None:
        self._blocked()
        spy, token = SpyBridge(), self._claim()
        code, _ = self._resume(spy, token)
        self.assertEqual((0, [self.SESSION_ID]), (code, spy.resumed))
        handoff = spy.handoffs[0]
        self.assertEqual(
            (self.handoff, 1, "BLOCKED_CI"),
            (handoff["handoff"], handoff["pr"], handoff["blocked_reason"]),
        )
        self.assertEqual(
            ("DEVELOPMENT", "repair"), (handoff["ownership"], handoff["expected_next_action"])
        )
        self.assertEqual(token, handoff["claim"])
        self.assertEqual(lq.claim_digest(token), handoff["claim_digest"])
        _, out = self._run(lambda: dr.resume(self.backend, 1, spy, token))
        self.assertEqual((True, 1), ("already-resumed" in out, len(spy.resumed)))
        self.backend.state_obj = lq.apply_enqueue(
            self.backend.state_obj,
            self._enqueue_request(lq.claim_digest(token)),
            self._attestation(),
        )
        self.assertEqual(lq.ACTIVE, lq.find(self.backend.state_obj, 1).state)
        self._blocked(run="12")
        self._resume(spy, self._claim())
        self.assertEqual([self.SESSION_ID, self.SESSION_ID], spy.resumed)
        # 每次 BLOCKED 代次只预占一个 marker；新一代次可继续 resume 同一 session。
        markers = {
            path.name
            for path in (self.worktree / ".nvsop" / "artifacts" / "landing").glob("*.resume")
        }
        self.assertEqual(2, len(markers))

    def test_bad_token_uncertain_and_stale_release(self) -> None:
        self._blocked()
        with self.assertRaises(dr.DispatchError):
            dr.resume(self.backend, 1, SpyBridge(), "")
        token = self._claim()
        with self.assertRaises(dr.DispatchError):
            dr.resume(self.backend, 1, SpyBridge(), "0" * 32)
        code, _ = self._run(lambda: dr.resume(self.backend, 1, BrokenBridge(), token))
        self.assertEqual(1, code)
        self.assertEqual("resuming", lq.find(self.backend.state_obj, 1).evidence["repair_state"])
        # 事件过期（head/main 漂移或证据变更）：释放为终态，绝不留下永久 claimed。
        for stale in ("head", "main", "evidence"):
            self._blocked()
            token = self._claim()
            if stale == "head":
                self.backend.pr = tq.pr_state(head="9" * 40)
            elif stale == "main":
                self.backend.main = "9" * 40
            else:
                entry = lq.find(self.backend.state_obj, 1)
                self.backend.state_obj = replace(
                    self.backend.state_obj,
                    entries=(replace(entry, evidence={**entry.evidence, "run": "10"}),),
                )
            spy = SpyBridge()
            code, out = self._run(lambda s=spy, t=token: dr.resume(self.backend, 1, s, t))
            self.assertEqual((0, [], True), (code, spy.resumed, "unavailable" in out))
            self.assertEqual(
                "unavailable", lq.find(self.backend.state_obj, 1).evidence["repair_state"]
            )
        requeued = lq.apply_enqueue(
            self.backend.state_obj,
            self._enqueue_request(lq.claim_digest(token), 70),
            self._attestation(),
        )
        self.assertEqual(lq.ACTIVE, lq.find(requeued, 1).state)

    def test_pending_claim_blocks_enqueue_until_terminal_completion(self) -> None:
        self._blocked()
        token = self._claim()
        before = len(self.backend.posted)
        for bad in ("", "b" * 32):
            with self.subTest(claim=bad), self.assertRaises(lq.QueueError):
                lq.enqueue(self.backend, 1, self.head, 500, repair_claim=bad)
        with self.assertRaises(lq.QueueError):
            lq.enqueue(self.backend, 1, self.head, 500, repair_claim=token)
        self.assertEqual(before, len(self.backend.posted))

        self.backend.state_obj = lq.apply_repair(
            self.backend.state_obj, self._request(lq.REPAIR_RESUMED, 90, token)
        )
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(0, lq.enqueue(self.backend, 1, self.head, 500, repair_claim=token))
        self.assertIn(lq.REQUEST_MARKER, self.backend.posted[-1])
        self.assertNotIn(token, self.backend.posted[-1])

    def test_explicit_claim_token_is_private_and_validated(self) -> None:
        self._blocked()
        token = "1" * 32
        code, out = self._run(lambda: dr.claim(self.backend, 1, token))
        self.assertEqual((0, True), (code, f"claim={token}" in out))
        self.assertNotIn(token, self.backend.posted[-1])
        self.assertIn(lq.claim_digest(token), self.backend.posted[-1])
        with self.assertRaises(dr.DispatchError):
            dr.claim(self.backend, 1, "not-a-claim-token")

    def test_preflight_checks_cwd_repository_generation_and_head(self) -> None:
        self._blocked()
        os.chdir(self.repo)
        code, out = self._run(lambda: dr.preflight(self.backend, 1, str(self.worktree)))
        self.assertEqual((0, True), (code, "preflight=ok" in out))
        with self.assertRaises(dr.DispatchError):
            dr.preflight(self.backend, 1, str(self.worktree), "0" * 64)
        self._git("remote", "set-url", "origin", "https://github.com/other/repo.git")
        with self.assertRaises(dr.DispatchError):
            dr.preflight(self.backend, 1, str(self.worktree))
        self._git("remote", "set-url", "origin", f"https://github.com/{tq.REPO}.git")
        (self.worktree / "x.txt").write_text("x\n", encoding="utf-8")
        self._git("add", "x.txt", cwd=self.worktree)
        self._git("commit", "--quiet", "-m", "x", cwd=self.worktree)
        with self.assertRaises(dr.DispatchError):
            dr.preflight(self.backend, 1, str(self.worktree))

    def test_preflight_keeps_local_head_on_authorization_root_when_remote_head_differs(
        self,
    ) -> None:
        self._blocked()
        entry = lq.find(self.backend.state_obj, 1)
        observed_head = "9" * 40
        self.backend.state_obj = replace(
            self.backend.state_obj,
            entries=(replace(entry, evidence={**entry.evidence, "observed_head": observed_head}),),
        )
        self.backend.pr = tq.pr_state(head=observed_head)

        code, out = self._run(lambda: dr.preflight(self.backend, 1, str(self.worktree)))

        self.assertEqual((0, True), (code, "preflight=ok" in out))

    def test_missing_receipt_after_resuming_reports_unavailable(self) -> None:
        # 归属解析失败必须释放为终态 unavailable，而不是抛错把 claim 搁浅在 resuming。
        self._blocked()
        token = self._claim()
        (self.worktree / ".nvsop" / "artifacts" / "landing" / f"{self.handoff}.json").unlink()
        spy = SpyBridge()
        code, _ = self._run(lambda: dr.resume(self.backend, 1, spy, token))
        entry = lq.find(self.backend.state_obj, 1)
        self.assertEqual(
            (0, [], "unavailable", lq.BLOCKED),
            (code, spy.resumed, entry.evidence["repair_state"], entry.state),
        )

    def test_missing_corrupt_or_taken_over_target_fails_closed(self) -> None:
        self._blocked()
        event = lq.repair_event(self.backend, 1)
        with self.assertRaises(dr.AgentUnavailableError):
            dr.resolve_target({**event, "branch": "agent/a/other"})
        receipt = self.worktree / ".nvsop" / "artifacts" / "landing" / f"{self.handoff}.json"
        receipt.unlink()
        with self.assertRaises(dr.AgentUnavailableError):
            dr.resolve_target(event)
        lq.local_receipt(self.handoff, 1, self.head, "agent/a/demo")
        binding = self.worktree / ".nvsop" / "session-binding.json"
        original = binding.read_bytes()
        binding.write_text("{not json", encoding="utf-8")
        with self.assertRaises(dr.AgentUnavailableError):
            dr.resolve_target(event)
        binding.write_text(
            json.dumps({**json.loads(original), "branch": "agent/a/other"}), encoding="utf-8"
        )
        with self.assertRaises(dr.AgentUnavailableError):
            dr.resolve_target(event)


if __name__ == "__main__":
    unittest.main()
