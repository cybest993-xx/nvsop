from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from check_repo_policy import check_repository


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
            Path(".python-version"): "3.12\n",
            Path("uv.lock"): "version = 1\n",
            Path("pyproject.toml"): (
                '[tool.uv.workspace]\nmembers = ["apps/control-api"]\n'
                '[tool.importlinter]\nroot_packages = ["factory_sop"]\n'
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
        self.assertIn("root src/ is forbidden; place production code in its owning app", errors)

    def test_rejects_committed_secret_file(self) -> None:
        path = self.root / ".env.production"
        path.write_text("SECRET=value\n")
        errors = self.check(str(path.relative_to(self.root)))
        self.assertIn("secret-like environment file must not be committed: .env.production", errors)

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

    def test_ignores_broken_markdown_link_inside_vendor(self) -> None:
        # `vendor/` is the NVIDIA base code and stays as delivered (ADR-0007); its own
        # docs carry a broken relative link we must not patch to satisfy our gate.
        path = self.root / "vendor/sop-monitoring-blueprints/docs/guide.md"
        path.parent.mkdir(parents=True)
        path.write_text("[missing](../nowhere/absent.md)\n")
        self.assertEqual([], self.check(str(path.relative_to(self.root))))

    def test_accepts_vendored_environment_template_with_placeholders(self) -> None:
        path = self.root / "vendor/sop-monitoring-blueprints/deployments/.env"
        path.parent.mkdir(parents=True)
        path.write_text(
            "# comment PASSWORD=real\n"
            "NGC_CLI_API_KEY='<ngc_api_key>'\n"
            "OPENAI_API_KEY=dummy\n"
            "ADAPTOR_PASSWORD=\n"
            "MODE=2d\n"
        )
        self.assertEqual([], self.check(str(path.relative_to(self.root))))

    def test_rejects_vendored_environment_template_with_real_secret(self) -> None:
        path = self.root / "vendor/sop-monitoring-blueprints/deployments/.env"
        path.parent.mkdir(parents=True)
        path.write_text("ADAPTOR_PASSWORD=hunter2\n")
        errors = self.check(str(path.relative_to(self.root)))
        self.assertTrue(any("assigns a real secret value" in error for error in errors), errors)

    def test_rejects_private_key_material_even_inside_vendor(self) -> None:
        path = self.root / "vendor/sop-monitoring-blueprints/tls/server.pem"
        path.parent.mkdir(parents=True)
        path.write_text("")
        errors = self.check(str(path.relative_to(self.root)))
        self.assertIn(
            "private key material must not be committed: "
            "vendor/sop-monitoring-blueprints/tls/server.pem",
            errors,
        )

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

    def write(self, relative: str, content: str) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return Path(relative)

    def test_rejects_missing_python_pin(self) -> None:
        (self.root / ".python-version").unlink()
        del self.files[Path(".python-version")]
        self.assertIn("missing required file: .python-version", self.check())

    def test_rejects_python_pin_that_is_not_the_anchored_version(self) -> None:
        self.write(".python-version", "3.13\n")
        errors = self.check()
        self.assertIn(".python-version must pin the center backend to 3.12, not 3.13", errors)

    def test_rejects_edge_runtime_joining_the_center_workspace(self) -> None:
        # Membership would give the judgment core a resolvable path to every center
        # dependency, leaving "standard library only" as discipline rather than a fact.
        self.write(
            "pyproject.toml",
            '[tool.uv.workspace]\nmembers = ["apps/control-api", "apps/edge-runtime"]\n',
        )
        errors = self.check()
        self.assertIn(
            "apps/edge-runtime must stay out of the uv workspace; its judgment core is "
            "standard-library-only (edge-autonomy.md §5.11)",
            errors,
        )

    def test_rejects_dependency_declared_by_edge_runtime(self) -> None:
        self.write(
            "apps/edge-runtime/pyproject.toml",
            '[project]\nname = "edge-runtime"\ndependencies = ["httpx"]\n',
        )
        errors = self.check()
        self.assertIn(
            "apps/edge-runtime/pyproject.toml declares dependencies; the inference host's "
            "package is standard-library-only (edge-autonomy.md §5.11)",
            errors,
        )

    def test_accepts_standard_library_and_own_imports_inside_edge_runtime(self) -> None:
        module = self.write(
            "apps/edge-runtime/src/edge_runtime/judgment/core.py",
            "import sqlite3\n"
            "from dataclasses import dataclass\n"
            "from edge_runtime.judgment.model import Observation\n"
            "from .reasons import ReasonCode\n",
        )
        self.assertEqual([], self.check(str(module)))

    def test_rejects_third_party_import_inside_edge_runtime(self) -> None:
        module = self.write(
            "apps/edge-runtime/src/edge_runtime/supervisor/station.py",
            "import httpx\n",
        )
        errors = self.check(str(module))
        self.assertIn(
            "apps/edge-runtime/src/edge_runtime/supervisor/station.py imports httpx, which "
            "is not in the standard library; the inference host's package is "
            "standard-library-only (edge-autonomy.md §5.11)",
            errors,
        )

    def test_rejects_center_module_with_no_import_linter_contract(self) -> None:
        # A module that no contract names is a module whose boundary is unenforced. The
        # contract lands in the same change as the module, or the gate fails.
        module = self.write("apps/control-api/src/factory_sop/auth/usecases/open_session.py", "")
        errors = self.check(str(module))
        self.assertIn(
            "center module auth has no import-linter contract in pyproject.toml; "
            "a module whose boundary is not named by a contract is unenforced",
            errors,
        )

    def test_accepts_center_module_named_by_an_import_linter_contract(self) -> None:
        self.write(
            "pyproject.toml",
            '[tool.uv.workspace]\nmembers = ["apps/control-api"]\n\n'
            "[[tool.importlinter.contracts]]\n"
            'name = "auth is reached only through its api"\n'
            'source_modules = ["factory_sop.auth"]\n',
        )
        module = self.write("apps/control-api/src/factory_sop/auth/usecases/open_session.py", "")
        self.assertEqual([], self.check(str(module)))


if __name__ == "__main__":
    unittest.main()
