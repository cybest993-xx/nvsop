from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from clean_local_artifacts import clean


class CleanLocalArtifactsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write(self, relative: str, content: str = "generated") -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        return path

    def test_removes_only_declared_reproducible_artifacts(self) -> None:
        disposable = [
            ".nvsop/cache/ruff/cache",
            ".nvsop/venv/bin/python",
            ".nvsop/tools/actionlint",
            ".nvsop/artifacts/web/dist/index.html",
            "node_modules/.pnpm/state",
            "apps/control-web/node_modules/.bin/vite",
            "apps/control-web/dist/index.html",
            "apps/control-web/test-results/trace.zip",
            ".venv/bin/python",
            ".import_linter_cache/cache.json",
            ".husky/_/husky.sh",
            "--version/_/husky.sh",
            "apps/control-api/src/control_api.egg-info/PKG-INFO",
            "apps/edge-runtime/src/edge_runtime/__pycache__/model.pyc",
        ]
        for path in disposable:
            self.write(path)

        preserved = [
            ".nvsop/dev-main/secrets/bootstrap-password",
            ".env.production",
            "private.key",
            ".tmp/task-handoff.md",
            "unknown-local-state/data.bin",
        ]
        for path in preserved:
            self.write(path, "keep")

        removed = clean(self.root)

        self.assertTrue(removed)
        for path in disposable:
            self.assertFalse((self.root / path).exists(), path)
        for path in preserved:
            self.assertEqual("keep", (self.root / path).read_text(), path)
