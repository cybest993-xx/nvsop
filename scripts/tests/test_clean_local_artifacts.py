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

    def test_daily_clean_preserves_environment_and_caches(self) -> None:
        output = self.write(".nvsop/artifacts/pytest/center-unit.xml")
        preserved = [
            self.write(".nvsop/venv/bin/python"),
            self.write(".nvsop/cache/uv/wheel"),
            self.write(".nvsop/tools/actionlint"),
            self.write("node_modules/.pnpm/state"),
            self.write("apps/control-api/src/control_api.egg-info/PKG-INFO"),
            self.write(".nvsop/dev-main/secrets/password"),
        ]
        clean(self.root)
        self.assertFalse(output.exists())
        self.assertTrue(all(path.exists() for path in preserved))

    def test_session_binding_survives_clean_and_purge(self) -> None:
        binding = self.write(".nvsop/session-binding.json", '{"version": 1}')

        clean(self.root)
        self.assertTrue(binding.exists())
        clean(self.root, purge=True)
        self.assertTrue(binding.exists())

    def test_purge_unlinks_legacy_cache_without_deleting_shared_content(self) -> None:
        shared = self.write("shared-cache/wheel", "keep")
        cache = self.root / ".nvsop/cache"
        cache.parent.mkdir()
        cache.symlink_to(shared.parent, target_is_directory=True)
        clean(self.root, purge=True)
        self.assertFalse(cache.is_symlink())
        self.assertEqual("keep", shared.read_text())

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

        removed = clean(self.root, purge=True)

        self.assertTrue(removed)
        for path in disposable:
            self.assertFalse((self.root / path).exists(), path)
        for path in preserved:
            self.assertEqual("keep", (self.root / path).read_text(), path)
