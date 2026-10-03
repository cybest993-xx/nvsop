from __future__ import annotations

import contextlib
import io
import json
import os
import re
import sys
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


class RepairDispatchTest(tq.GitTaskFixture):
    """真实临时 Git worktree + 绑定 + receipt 的派发 dryrun。"""

    SESSION_ID = "repair-owner-session"

    def setUp(self) -> None:
        super().setUp()
        self.handoff, self._posted = "f" * 32, 0
        lq.local_receipt(self.handoff, 1, self.head, "agent/a/demo")

    def _blocked(self, run: str = "9") -> None:
        entry, att = tq.active_entry(root=self.head)
        fields = ("handoff", "expected_head", "observed_head", "main", "run", "attempt")
        evidence = dict(
            zip(fields, (self.handoff, self.head, self.head, tq.BASE, run, "1"), strict=True)
        )
        state = lq.block(
            lq.State(1, (replace(entry, handoff=self.handoff),)), 1, lq.BLOCKED_CI, evidence
        )
        self.att, self._posted = att, 0
        self.backend = tq.FakeBackend(
            state=state, comments={500: att}, pr=tq.pr_state(head=self.head), main=tq.BASE
        )

    def _run(self, call: object) -> tuple[int, str]:
        """跑一次 CLI 并把其发布的请求交给控制器消费（模拟异步可信控制器）。"""
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = call()  # type: ignore[operator]
        while self._posted < len(self.backend.posted):
            cid = max(self.backend.comments, default=0) + 1
            self.backend.comments[cid] = tq.comment(cid, self.backend.posted[self._posted], pr=1)
            self._posted += 1
            result = lq._consume(self.backend, self.backend.state_obj)
            if result != self.backend.state_obj:
                self.assertTrue(self.backend.write_state(result, self.backend.revision))
        return code, buffer.getvalue()

    def _claim(self) -> str:
        code, out = self._run(lambda: dr.claim(self.backend, 1))
        self.assertEqual(0, code)
        return TOKEN.search(out).group(1)  # type: ignore[union-attr]

    def _resume(self, host: object, token: str) -> tuple[int, str]:
        for _ in range(2):
            code, out = self._run(lambda: dr.resume(self.backend, 1, host, token))
            if "pending-resume" not in out:
                break
        return code, out

    def test_claim_digest_private_then_unavailable_and_spy_resume(self) -> None:
        self._blocked()
        event = lq.repair_event(self.backend, 1)
        self.assertEqual(lq.STATE_BRANCH, event["evidence_location"]["state_branch"])
        token = self._claim()
        entry = lq.find(self.backend.state_obj, 1)
        self.assertEqual(lq.claim_digest(token), entry.evidence["repair_claim"])
        code, _ = self._resume(dr.UnavailableHostBridge(), token)
        entry = lq.find(self.backend.state_obj, 1)
        self.assertEqual(
            (0, lq.BLOCKED, "unavailable"), (code, entry.state, entry.evidence["repair_state"])
        )
        public = json.dumps(lq.state_to_json(self.backend.state_obj), sort_keys=True)
        for key in ("session", "worktree", token):
            self.assertNotIn(key, public)
        self.assertNotIn(token, json.dumps(lq.repair_event(self.backend, 1)))
        for name in ("session-binding.json", f"{self.handoff}.json"):
            self.assertTrue((self.worktree / ".nvsop").rglob(name).__next__().is_file())
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
        _, out = self._run(lambda: dr.resume(self.backend, 1, spy, token))
        self.assertEqual((True, 1), ("already-resumed" in out, len(spy.resumed)))

    def test_resuming_blocks_reenqueue_then_reblock_cycle(self) -> None:
        self._blocked()
        spy, token = SpyBridge(), self._claim()
        # 单次 resume 只发布 resuming（确认前不调 host）；期间带 token 也不得重入队。
        _, out = self._run(lambda: dr.resume(self.backend, 1, spy, token))
        self.assertEqual((True, []), ("pending-resume" in out, spy.resumed))
        entry = lq.find(self.backend.state_obj, 1)
        self.assertEqual("resuming", entry.evidence["repair_state"])
        request = lq.Request(
            "enqueue", 1, 99, self.head, self.handoff, 500, entry.attestation_digest, claim=token
        )
        with self.assertRaises(lq.QueueError):
            lq.apply_enqueue(
                self.backend.state_obj, request, lq.attestation_from_comment(self.att, tq.REPO, 1)
            )
        self._resume(spy, token)
        entry = lq.find(self.backend.state_obj, 1)
        request = lq.Request(
            "enqueue", 1, 60, self.head, self.handoff, 500, entry.attestation_digest, claim=token
        )
        attestation = lq.attestation_from_comment(self.att, tq.REPO, 1)
        self.backend.state_obj = lq.apply_enqueue(self.backend.state_obj, request, attestation)
        self.assertEqual(lq.ACTIVE, lq.find(self.backend.state_obj, 1).state)
        self._blocked(run="12")
        self._resume(spy, self._claim())
        self.assertEqual([self.SESSION_ID, self.SESSION_ID], spy.resumed)

    def test_bad_token_uncertain_and_stale_fail_closed(self) -> None:
        self._blocked()
        with self.assertRaises(dr.DispatchError):
            dr.resume(self.backend, 1, SpyBridge(), "")
        token = self._claim()
        with self.assertRaises(dr.DispatchError):
            dr.resume(self.backend, 1, SpyBridge(), "0" * 32)
        self._run(lambda: dr.resume(self.backend, 1, SpyBridge(), token))
        posted = len(self.backend.posted)
        code, _ = self._run(lambda: dr.resume(self.backend, 1, BrokenBridge(), token))
        self.assertEqual((1, posted), (code, len(self.backend.posted)))
        self.assertEqual("resuming", lq.find(self.backend.state_obj, 1).evidence["repair_state"])
        # 声明绑定的事件过期：释放为终态，绝不留下永久 claimed。
        self._blocked()
        token = self._claim()
        entry = lq.find(self.backend.state_obj, 1)
        mutated = replace(entry, evidence={**entry.evidence, "run": "10"})
        self.backend.state_obj = replace(self.backend.state_obj, entries=(mutated,))
        spy = SpyBridge()
        code, out = self._run(lambda: dr.resume(self.backend, 1, spy, token))
        self.assertEqual((0, [], True), (code, spy.resumed, "unavailable" in out))
        self.assertEqual("unavailable", lq.find(self.backend.state_obj, 1).evidence["repair_state"])

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

    def test_tokenless_enqueue_fails_closed_during_repair(self) -> None:
        self._blocked()
        self._claim()
        before = len(self.backend.posted)
        with self.assertRaises(lq.QueueError):
            lq.enqueue(self.backend, 1, self.head, 500)
        self.assertEqual(before, len(self.backend.posted))
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(0, lq.enqueue(self.backend, 1, self.head, 500, repair_claim="a" * 32))
        self.assertIn(lq.REQUEST_MARKER, self.backend.posted[-1])

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
