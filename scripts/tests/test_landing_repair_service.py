from __future__ import annotations

import contextlib
import io
import json
import os
import stat
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path[:0] = [str(ROOT / "scripts"), str(HERE)]

import test_dispatch_landing_repair as td  # noqa: E402
import test_landing_queue as tq  # noqa: E402

import dispatch_landing_repair as dr  # noqa: E402
import landing_queue as lq  # noqa: E402
import landing_repair_service as service  # noqa: E402


class FakeAdapter:
    def __init__(self, *, available: bool = True) -> None:
        self.available = available
        self.resumed: list[dr.ResumeRequest] = []

    def verify(self, request: dr.ResumeRequest) -> bool:
        return self.available

    def resume(self, request: dr.ResumeRequest) -> dr.ResumeOutcome:
        self.resumed.append(request)
        return dr.ResumeOutcome.RESUMED


class RepairServiceTest(tq.GitTaskFixture):
    SESSION_ID = "repair-service-owner"

    def setUp(self) -> None:
        super().setUp()
        self.handoff = "f" * 32
        lq.local_receipt(self.handoff, 1, self.head, "agent/a/demo")
        entry, att = tq.active_entry(root=self.head)
        evidence = dict(
            zip(
                ("handoff", "expected_head", "observed_head", "main", "run", "attempt"),
                (self.handoff, self.head, self.head, tq.BASE, "9", "1"),
                strict=True,
            )
        )
        state = lq.block(
            lq.State(1, (replace(entry, handoff=self.handoff),)), 1, lq.BLOCKED_CI, evidence
        )
        self.backend = td.SyncBackend(
            state=state, comments={500: att}, pr=tq.pr_state(head=self.head), main=tq.BASE
        )

    def _tick(self, adapter: FakeAdapter) -> int:
        dispatcher = service.RepairService(
            self.backend, service.AdapterBridge([adapter]), only_pr=1
        )
        with contextlib.redirect_stdout(io.StringIO()):
            return dispatcher.tick()

    def test_restart_reuses_private_claim_and_resumes_exact_bound_session(self) -> None:
        self.assertEqual(1, self._tick(FakeAdapter()))
        entry = lq.find(self.backend.state_obj, 1)
        event = entry.evidence["repair_generation"]
        claim_file = service.claim_path(str(self.worktree), event)
        token = claim_file.read_text(encoding="ascii").strip()
        self.assertEqual(
            ("claimed", 0o600),
            (entry.evidence["repair_state"], stat.S_IMODE(claim_file.stat().st_mode)),
        )
        self.assertNotIn(token, json.dumps(lq.state_to_json(self.backend.state_obj)))
        self.assertNotIn(token, "\n".join(self.backend.posted))

        resumed = FakeAdapter()
        self.assertEqual(1, self._tick(resumed))
        entry = lq.find(self.backend.state_obj, 1)
        request = resumed.resumed[0]
        self.assertEqual(("resumed", 1), (entry.evidence["repair_state"], len(resumed.resumed)))
        self.assertEqual(
            (
                self.SESSION_ID,
                str(self.worktree.resolve()),
                token,
                lq.claim_digest(token),
                "DEVELOPMENT",
                "repair",
            ),
            (
                request.session_id,
                request.handoff["worktree"],
                request.handoff["claim"],
                request.handoff["claim_digest"],
                request.handoff["ownership"],
                request.handoff["expected_next_action"],
            ),
        )
        self.assertEqual(
            1, len(list((self.worktree / ".nvsop" / "artifacts" / "landing").glob("*.resume")))
        )

    def test_unavailable_adapter_never_posts_public_claim(self) -> None:
        adapter = FakeAdapter(available=False)
        before = len(self.backend.posted)
        self.assertEqual(0, self._tick(adapter))
        self.assertNotIn("repair_state", lq.find(self.backend.state_obj, 1).evidence)
        self.assertEqual((before, []), (len(self.backend.posted), adapter.resumed))

    def test_private_claim_mismatch_blocks_resume(self) -> None:
        self.assertEqual(1, self._tick(FakeAdapter()))
        entry = lq.find(self.backend.state_obj, 1)
        claim_file = service.claim_path(str(self.worktree), entry.evidence["repair_generation"])
        claim_file.write_text("2" * 32 + "\n", encoding="ascii")
        os.chmod(claim_file, 0o600)
        adapter = FakeAdapter()
        self.assertEqual((0, []), (self._tick(adapter), adapter.resumed))
        self.assertEqual("claimed", lq.find(self.backend.state_obj, 1).evidence["repair_state"])

    def test_local_claim_rejects_symlink_and_broad_permissions(self) -> None:
        path = service.claim_path(str(self.worktree), "a" * 64)
        path.parent.mkdir(parents=True, exist_ok=True)
        target = self.root / "outside-token"
        target.write_text("1" * 32 + "\n", encoding="ascii")
        path.symlink_to(target)
        with self.assertRaises(service.ServiceError):
            service.local_claim(str(self.worktree), "a" * 64)
        path.unlink()
        path.write_text("1" * 32 + "\n", encoding="ascii")
        os.chmod(path, 0o644)
        with self.assertRaises(service.ServiceError):
            service.local_claim(str(self.worktree), "a" * 64)


class AdapterContractTest(unittest.TestCase):
    @staticmethod
    def _request(worktree: str) -> dr.ResumeRequest:
        return dr.ResumeRequest(
            "session-a",
            {"event": "a" * 64, "worktree": worktree, "session_id": "session-a", "claim": "1" * 32},
        )

    def test_ambiguous_adapters_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory, self.assertRaises(service.ServiceError):
            service.AdapterBridge([FakeAdapter(), FakeAdapter()]).verify(self._request(directory))

    def test_command_adapter_uses_json_stdin_contract(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "adapter"
            executable.write_text(
                "#!/usr/bin/env python3\nimport json, sys\n"
                "payload=json.load(sys.stdin); assert payload['session_id']=='session-a'\n"
                "print(json.dumps({'available': True} if sys.argv[1]=='probe' "
                "else {'outcome':'resumed'}))\n",
                encoding="utf-8",
            )
            os.chmod(executable, 0o700)
            adapter = service.CommandAdapter(str(executable))
            request = self._request(directory)
            self.assertTrue(adapter.verify(request))
            self.assertEqual(dr.ResumeOutcome.RESUMED, adapter.resume(request))

    def test_pi_writer_detection_is_conservative_by_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stored, task, other = (root / name for name in ("stored", "task", "other"))
            for path in (stored, task, other):
                path.mkdir()
            header = {"cwd": str(stored)}
            self.assertFalse(
                service.pi_session_writer_may_be_alive(header, str(task), [str(other)])
            )
            self.assertTrue(
                service.pi_session_writer_may_be_alive(header, str(task), [str(stored)])
            )
            self.assertTrue(service.pi_session_writer_may_be_alive(header, str(task), [str(task)]))
