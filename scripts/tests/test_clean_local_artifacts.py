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

    def test_daily_clean_preserves_landing_state_until_purge(self) -> None:
        disposable = self.write(".nvsop/artifacts/pytest/center-unit.xml")
        landing = [
            self.write(".nvsop/artifacts/landing/handoff.json"),
            self.write(".nvsop/artifacts/landing/event.claim"),
            self.write(".nvsop/artifacts/landing/event.resume"),
        ]

        clean(self.root)
        self.assertFalse(disposable.exists())
        self.assertTrue(all(path.exists() for path in landing))

        clean(self.root, purge=True)
        self.assertTrue(all(not path.exists() for path in landing))

    def test_purge_removes_precise_tool_caches_and_empties_owned_nvsop_root(self) -> None:
        # ENVIRONMENT_PATHS 曾经遗漏 apps/control-api/.mypy_cache, purge 后仍然残留。
        mypy_cache = self.write("apps/control-api/.mypy_cache/plugin.json")
        # purge 清空已声明的任务本地状态后, 空的自有 .nvsop 根仍阻碍 task-cleanup。
        (self.root / ".nvsop/cache").mkdir(parents=True)
        removed = clean(self.root, purge=True)

        self.assertFalse(mypy_cache.exists())
        self.assertIn(Path("apps/control-api/.mypy_cache"), removed)
        self.assertFalse((self.root / ".nvsop/cache").exists())
        self.assertFalse((self.root / ".nvsop").exists())

    def test_purge_removes_legacy_web_build_and_empty_parents(self) -> None:
        legacy = self.write("apps/control-web/.nvsop/artifacts/web/dist/index.html")

        removed = clean(self.root, purge=True)

        self.assertFalse(legacy.exists())
        self.assertFalse((self.root / "apps/control-web/.nvsop").exists())
        self.assertIn(Path("apps/control-web/.nvsop"), removed)

    def test_purge_preserves_unknown_legacy_web_state(self) -> None:
        legacy = self.write("apps/control-web/.nvsop/artifacts/web/dist/index.html")
        unknown = self.write("apps/control-web/.nvsop/artifacts/other/info.json", "keep")
        secret = self.write("apps/control-web/.nvsop/dev-main/secrets/password", "keep")

        clean(self.root, purge=True)

        self.assertFalse(legacy.exists())
        self.assertEqual("keep", unknown.read_text())
        self.assertEqual("keep", secret.read_text())

    def test_purge_does_not_follow_legacy_web_state_symlink(self) -> None:
        shared = self.write("shared/artifacts/web/dist/index.html", "keep")
        link = self.root / "apps/control-web/.nvsop"
        link.parent.mkdir(parents=True)
        link.symlink_to(self.root / "shared", target_is_directory=True)

        clean(self.root, purge=True)

        self.assertTrue(link.is_symlink())
        self.assertEqual("keep", shared.read_text())

    def test_purge_keeps_nonempty_or_foreign_or_linked_nvsop_root(self) -> None:
        # 未知文件仍然保留: purge 不能吞掉未声明的任务状态。
        unknown = self.write(".nvsop/unknown-state/data.bin", "keep")
        clean(self.root, purge=True)
        self.assertEqual("keep", unknown.read_text())
        self.assertTrue((self.root / ".nvsop").exists())

        # 合法绑定状态不因 purge 消失, 空目录判断不会越过它。
        binding = self.write(".nvsop/session-binding.json", '{"version": 1}')
        clean(self.root, purge=True)
        self.assertTrue(binding.exists())
        unknown.unlink()
        binding.unlink()

        # 指向共享目标的 .nvsop symlink 即使为空也不能 rmdir。
        shared = self.write("shared/state", "keep")
        link = self.root / ".nvsop"
        (link / "unknown-state").rmdir()
        link.rmdir()
        link.symlink_to(shared.parent, target_is_directory=True)
        clean(self.root, purge=True)
        self.assertTrue(link.is_symlink())
        self.assertEqual("keep", shared.read_text())

    def test_nvsop_root_symlink_protects_shared_target_contents(self) -> None:
        """沿父 symlink 访问已声明名称时不得删除共享目标内容。

        clean 路径逐项拼 root/relative; 若 .nvsop 自身是 symlink 而 shared
        目标里恰好有同名 cache/venv/artifacts, 逐项 remove_path 会沿链接
        删掉共享内容。daily 与 purge 两种模式都必须保护。
        """
        self.write("shared/cache/wheel", "keep")
        self.write("shared/venv/bin/python", "keep")
        self.write("shared/artifacts/pytest/report.xml", "keep")
        link = self.root / ".nvsop"
        link.mkdir()
        link.rmdir()
        link.symlink_to(self.root / "shared", target_is_directory=True)

        clean(self.root)
        self.assertEqual("keep", (self.root / "shared/cache/wheel").read_text())
        self.assertTrue(link.is_symlink())

        clean(self.root, purge=True)
        self.assertEqual("keep", (self.root / "shared/cache/wheel").read_text())
        self.assertEqual("keep", (self.root / "shared/venv/bin/python").read_text())
        self.assertEqual("keep", (self.root / "shared/artifacts/pytest/report.xml").read_text())
        self.assertTrue(link.is_symlink())

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
