from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "apps" / "control-api" / "src"))
SPEC = importlib.util.spec_from_file_location("nvsop_dev_seed", ROOT / "scripts" / "dev_seed.py")
assert SPEC is not None and SPEC.loader is not None
SEED = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = SEED
SPEC.loader.exec_module(SEED)

UPLOAD_URL = "/api/v1/training-datasets/dataset-1/members/member-1/attempts/attempt-1/content"


class RetryClient:
    def __init__(self) -> None:
        self.uploaded: Path | None = None
        self.confirmed: tuple[str, str, str] | None = None

    def request_json(
        self, method: str, path: str, body: object | None = None, **_: object
    ) -> dict[str, object]:
        if path.endswith("/members?page=1&page_size=100"):
            return {
                "items": [
                    {
                        "id": "member-old",
                        "original_filename": SEED.SAMPLE_VIDEO_FILENAME,
                        "status": "failed",
                    }
                ]
            }
        if path.endswith("/retry"):
            return {
                "member": {"id": "member-retried"},
                "attempt": {"id": "attempt-retried"},
                "upload": {
                    "url": UPLOAD_URL,
                    "method": "PUT",
                },
            }
        raise AssertionError(f"unexpected request: {method} {path}")

    def upload_file(self, *, instructions: dict[str, object], path: Path) -> SimpleNamespace:
        self.uploaded = path
        self.assert_upload(instructions)
        return SimpleNamespace(status=204)

    @staticmethod
    def assert_upload(instructions: dict[str, object]) -> None:
        if instructions != {
            "url": UPLOAD_URL,
            "method": "PUT",
        }:
            raise AssertionError(instructions)

    def confirm_video_upload(
        self, *, dataset_id: str, member_id: str, attempt_id: str
    ) -> dict[str, object]:
        self.confirmed = (dataset_id, member_id, attempt_id)
        return {"job": {"id": "job-1"}}

    def wait_for_job(self, *, job_id: str) -> dict[str, object]:
        if job_id != "job-1":
            raise AssertionError(job_id)
        return {"status": "succeeded"}


class DevSeedTest(unittest.TestCase):
    def test_failed_video_retries_the_returned_upload_instructions(self) -> None:
        video = ROOT / ".nvsop" / "artifacts" / "tests" / "test-dev-seed.mp4"
        video.parent.mkdir(parents=True, exist_ok=True)
        video.write_bytes(b"synthetic")
        self.addCleanup(video.unlink, missing_ok=True)
        client = RetryClient()

        result = SEED.ensure_video(client, "dataset-1", video)

        self.assertEqual("member-retried", result)
        self.assertEqual(video, client.uploaded)
        self.assertEqual(("dataset-1", "member-retried", "attempt-retried"), client.confirmed)


if __name__ == "__main__":
    unittest.main()
