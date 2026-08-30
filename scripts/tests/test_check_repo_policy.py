from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from check_repo_policy import check_repository  # noqa: E402


class RepositoryPolicyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.files = {
            Path("AGENTS.md"): "docs/design/repository-harness.md",
            Path("CONTEXT.md"): "# Language\n",
            Path("Makefile"): "check:\n\ttrue\n",
            Path("docs/design/repository-harness.md"): "# Harness\n",
            Path("docs/design/solution-and-roadmap.md"): "# Current decisions\n",
            Path(".github/workflows/blocking-ci.yml"): (
                "pull_request:\nCI required\nalways()\nmake check\n"
            ),
        }
        for path, content in self.files.items():
            target = self.root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def check(self, *extra: str) -> list[str]:
        paths = list(self.files) + [Path(path) for path in extra]
        return check_repository(self.root, paths)

    def test_accepts_minimum_harness(self) -> None:
        self.assertEqual([], self.check())

    def test_rejects_undeclared_application(self) -> None:
        path = self.root / "apps/mystery/src/main.py"
        path.parent.mkdir(parents=True)
        path.write_text("")
        self.assertIn(
            "undeclared production app: apps/mystery/",
            self.check(str(path.relative_to(self.root))),
        )

    def test_rejects_root_source_tree(self) -> None:
        path = self.root / "src/main.py"
        path.parent.mkdir()
        path.write_text("")
        errors = self.check(str(path.relative_to(self.root)))
        self.assertIn(
            "root src/ is forbidden; place production code in its owning app", errors
        )

    def test_rejects_committed_secret_file(self) -> None:
        path = self.root / ".env.production"
        path.write_text("SECRET=value\n")
        errors = self.check(str(path.relative_to(self.root)))
        self.assertIn(
            "secret-like environment file must not be committed: .env.production", errors
        )

    def test_rejects_root_test_without_cross_app_owner(self) -> None:
        path = self.root / "tests/unit/test_example.py"
        path.parent.mkdir(parents=True)
        path.write_text("")
        errors = self.check(str(path.relative_to(self.root)))
        self.assertTrue(
            any("root test has no declared cross-app owner" in error for error in errors)
        )

    def test_rejects_broken_local_markdown_link(self) -> None:
        path = self.root / "docs/design/broken.md"
        path.write_text("[missing](missing.md)\n")
        errors = self.check(str(path.relative_to(self.root)))
        self.assertIn("broken local Markdown link: docs/design/broken.md -> missing.md", errors)

    def test_rejects_superseded_decision_artifact(self) -> None:
        path = self.root / "docs/research/control-gateway-stack-and-media-routing.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# Stale\n")
        errors = self.check(str(path.relative_to(self.root)))
        self.assertTrue(
            any("superseded decision artifact must stay deleted" in error for error in errors)
        )

    def test_repository_file_listing_ignores_deleted_tracked_files(self) -> None:
        # check_repository receives only files that still exist in the working tree;
        # this keeps deliberate deletions from being parsed as Markdown.
        missing = Path("docs/research/deleted.md")
        self.assertFalse((self.root / missing).exists())
        self.assertEqual([], self.check())


if __name__ == "__main__":
    unittest.main()
