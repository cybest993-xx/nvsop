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
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(HERE))

import test_dispatch_landing_repair as td  # noqa: E402
import test_landing_queue as tq  # noqa: E402

import dispatch_landing_repair as dr  # noqa: E402
import landing_queue as lq  # noqa: E402
import landing_repair_service as service  # noqa: E402


class FakeAdapter:
    name = "fake"

    def __init__(self, *, available: bool = True) -> None:
        self.available = available
        self.verified: list[dr.ResumeRequest] = []
        self.resumed: list[dr.ResumeRequest] = []

    def verify(self, request: dr.ResumeRequest) -> bool:
        self.verified.append(request)
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
        fields = ("handoff", "expected_head", "observed_head", "main", "run", "attempt")
        evidence = dict(
            zip(fields, (self.handoff, self.head, self.head, tq.BASE, "9", "1"), strict=True)
        )
        state = lq.block(
            lq.State(1, (replace(entry, handoff=self.handoff),)), 1, lq.BLOCKED_CI, evidence
        )
        self.backend = td.SyncBackend(
            state=state,
            comments={500: att},
            pr=tq.pr_state(head=self.head),
            main=tq.BASE,
        )

    def _tick(self, adapter: FakeAdapter) -> int:
        bridge = service.AdapterBridge([adapter])
        dispatcher = service.RepairService(self.backend, bridge, only_pr=1)
        with contextlib.redirect_stdout(io.StringIO()):
            return dispatcher.tick()

    def test_restart_reuses_private_claim_and_resumes_exact_bound_session(self) -> None:
        first = FakeAdapter()
        self.assertEqual(1, self._tick(first))
        entry = lq.find(self.backend.state_obj, 1)
        self.assertEqual("claimed", entry.evidence["repair_state"])
        event = entry.evidence["repair_generation"]
        claim_file = service.claim_path(str(self.worktree), event)
        token = claim_file.read_text(encoding="ascii").strip()
        self.assertEqual(0o600, stat.S_IMODE(claim_file.stat().st_mode))
        self.assertNotIn(
            token, json.dumps(lq.state_to_json(self.backend.state_obj), sort_keys=True)
        )
        self.assertNotIn(token, "\n".join(self.backend.posted))

        # 模拟 service 重启：新的 bridge/adapter 必须读取同一个本地 token，而不是生成第二个 claim。
        second = FakeAdapter()
        self.assertEqual(1, self._tick(second))
        entry = lq.find(self.backend.state_obj, 1)
        self.assertEqual("resumed", entry.evidence["repair_state"])
        self.assertEqual(1, len(second.resumed))
        request = second.resumed[0]
        self.assertEqual(self.SESSION_ID, request.session_id)
        self.assertEqual(str(self.worktree.resolve()), request.handoff["worktree"])
        self.assertEqual(token, request.handoff["claim"])
        self.assertEqual(lq.claim_digest(token), request.handoff["claim_digest"])
        self.assertEqual("DEVELOPMENT", request.handoff["ownership"])
        self.assertEqual("repair", request.handoff["expected_next_action"])
        markers = list((self.worktree / ".nvsop" / "artifacts" / "landing").glob("*.resume"))
        self.assertEqual(1, len(markers))

    def test_unavailable_adapter_never_posts_public_claim(self) -> None:
        adapter = FakeAdapter(available=False)
        before = len(self.backend.posted)
        self.assertEqual(0, self._tick(adapter))
        entry = lq.find(self.backend.state_obj, 1)
        self.assertNotIn("repair_state", entry.evidence)
        self.assertEqual(before, len(self.backend.posted))
        self.assertEqual([], adapter.resumed)

    def test_private_claim_mismatch_blocks_resume(self) -> None:
        self.assertEqual(1, self._tick(FakeAdapter()))
        entry = lq.find(self.backend.state_obj, 1)
        claim_file = service.claim_path(str(self.worktree), entry.evidence["repair_generation"])
        claim_file.write_text("2" * 32 + "\n", encoding="ascii")
        os.chmod(claim_file, 0o600)
        adapter = FakeAdapter()
        self.assertEqual(0, self._tick(adapter))
        self.assertEqual([], adapter.resumed)
        self.assertEqual("claimed", lq.find(self.backend.state_obj, 1).evidence["repair_state"])

    def test_local_claim_rejects_symlink_and_broad_permissions(self) -> None:
        event = "a" * 64
        path = service.claim_path(str(self.worktree), event)
        path.parent.mkdir(parents=True, exist_ok=True)
        target = self.root / "outside-token"
        target.write_text("1" * 32 + "\n", encoding="ascii")
        path.symlink_to(target)
        with self.assertRaises(service.ServiceError):
            service.local_claim(str(self.worktree), event)
        path.unlink()
        path.write_text("1" * 32 + "\n", encoding="ascii")
        os.chmod(path, 0o644)
        with self.assertRaises(service.ServiceError):
            service.local_claim(str(self.worktree), event)


class AdapterContractTest(unittest.TestCase):
    def _request(self, worktree: str) -> dr.ResumeRequest:
        return dr.ResumeRequest(
            "session-a",
            {
                "event": "a" * 64,
                "worktree": worktree,
                "session_id": "session-a",
                "claim": "1" * 32,
            },
        )

    def test_ambiguous_adapters_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            request = self._request(directory)
            bridge = service.AdapterBridge([FakeAdapter(), FakeAdapter()])
            with self.assertRaises(service.ServiceError):
                bridge.verify(request)

    def test_command_adapter_uses_json_stdin_contract(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "adapter"
            executable.write_text(
                "#!/usr/bin/env python3\n"
                "import json, sys\n"
                "payload = json.load(sys.stdin)\n"
                "assert payload['session_id'] == 'session-a'\n"
                "if sys.argv[1] == 'probe': print(json.dumps({'available': True}))\n"
                "else: print(json.dumps({'outcome': 'resumed'}))\n",
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
            stored = root / "stored"
            task = root / "task"
            other = root / "other"
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


if __name__ == "__main__":
    unittest.main()
