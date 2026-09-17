from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "ci_scope.py"


class CiScopeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.git("init", "--quiet", "--initial-branch=main")
        self.git("config", "user.name", "CI scope test")
        self.git("config", "user.email", "ci-scope@example.invalid")
        self.git("config", "commit.gpgsign", "false")
        self.git("config", "core.hooksPath", str(self.root / "no-hooks"))
        self.base = self.commit("base")

    def git(self, *args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=self.root, check=True, capture_output=True, text=True
        ).stdout

    def commit(self, message: str) -> str:
        self.git("add", ".")
        self.git("commit", "--quiet", "--allow-empty", "-m", message)
        return self.git("rev-parse", "HEAD").strip()

    def write(self, name: str) -> None:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("synthetic content\n", encoding="utf-8")

    def scope(self, *refs: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPT), *refs],
            cwd=self.root,
            check=False,
            capture_output=True,
            text=True,
        )

    @staticmethod
    def outputs(result: subprocess.CompletedProcess[str]) -> dict[str, str]:
        return dict(line.split("=", 1) for line in result.stdout.splitlines())

    def test_edge_and_shared_runtime_inputs_select_real_system_evidence(self) -> None:
        for name in (
            "apps/edge-runtime/src/edge_runtime/runtime.py",
            "tests/fixtures/runtime.json",
            "deploy/media/compose.yaml",
        ):
            with self.subTest(path=name):
                base = self.git("rev-parse", "HEAD").strip()
                self.write(name)
                result = self.scope(base, self.commit("runtime input"))
                self.assertEqual(0, result.returncode, result.stderr)
                values = self.outputs(result)
                is_media = name.startswith("deploy/media/")
                self.assertEqual("true", values.get("integration"), values)
                self.assertEqual("true" if is_media else "false", values.get("browser"), values)
                self.assertEqual("true" if is_media else "false", values.get("media"), values)

    def test_runtime_version_pins_select_their_affected_evidence(self) -> None:
        for name, integration, browser in (
            (".python-version", "true", "false"),
            (".nvmrc", "false", "true"),
        ):
            with self.subTest(path=name):
                base = self.git("rev-parse", "HEAD").strip()
                self.write(name)
                result = self.scope(base, self.commit("runtime pin"))
                self.assertEqual(0, result.returncode, result.stderr)
                values = self.outputs(result)
                self.assertEqual(integration, values.get("integration"), values)
                self.assertEqual(browser, values.get("browser"), values)
                self.assertEqual("false", values.get("media"), values)

    def test_web_inputs_select_browser_without_center_integration(self) -> None:
        self.write("apps/control-web/src/main.ts")
        result = self.scope(self.base, self.commit("web input"))
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(
            {
                "docs_only": "false",
                "force_all": "false",
                "integration": "false",
                "browser": "true",
                "media": "false",
            },
            self.outputs(result),
        )

    def test_documentation_only_uses_the_fast_lane(self) -> None:
        for name in (
            "AGENTS.md",
            "CLAUDE.md",
            "CONTEXT.md",
            "README.md",
            "docs/guide.md",
            "docs/design/guide.md",
        ):
            self.write(name)
        head = self.commit("documentation")

        result = self.scope(self.base, head)

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(
            "docs_only=true\nforce_all=false\nintegration=false\nbrowser=false\nmedia=false\n",
            result.stdout,
        )

    def test_mixed_or_unlisted_paths_use_the_code_gate(self) -> None:
        for name in (
            "apps/control-api/src/source.py",
            "apps/control-web/README.md",
            "vendor/README.md",
            "scripts/README.md",
            "OTHER.md",
            "docs/image.png",
            "docs/guide.MD",
            "docs/example.md/source.py",
            ".nvmrc",
            ".claude/settings.json",
        ):
            with self.subTest(path=name):
                base = self.git("rev-parse", "HEAD").strip()
                self.write("docs/guide.md")
                self.write(name)
                result = self.scope(base, self.commit("mixed change"))

                self.assertEqual(0, result.returncode, result.stderr)
                values = self.outputs(result)
                self.assertEqual("false", values.get("docs_only"), values)
                self.assertEqual("false", values.get("force_all"), values)

    def test_renaming_code_to_markdown_keeps_code_checks(self) -> None:
        self.git("config", "diff.renames", "true")
        self.write("source.py")
        base = self.commit("existing code")
        (self.root / "docs").mkdir()
        self.git("mv", "source.py", "docs/source.md")

        result = self.scope(base, self.commit("rename code to markdown"))

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(
            "docs_only=false\nforce_all=false\nintegration=false\nbrowser=false\nmedia=false\n",
            result.stdout,
        )

    def test_ci_configuration_and_selector_changes_force_all_gates(self) -> None:
        for name in (
            ".github/workflows/ci.yml",
            ".github/actions/check/action.yml",
            "Makefile",
            "scripts/ci_scope.py",
            "scripts/install_actionlint.py",
        ):
            with self.subTest(path=name):
                base = self.git("rev-parse", "HEAD").strip()
                self.write(name)

                result = self.scope(base, self.commit("CI changes"))

                self.assertEqual(0, result.returncode, result.stderr)
                self.assertEqual(
                    "docs_only=false\nforce_all=true\nintegration=true\nbrowser=true\nmedia=true\n",
                    result.stdout,
                )

    def test_unusable_baseline_forces_all_gates(self) -> None:
        self.write("README.md")
        head = self.commit("documentation")
        for base in ("", "0" * 40, "missing-ref", "--stat"):
            with self.subTest(base=base):
                result = self.scope(base, head)

                self.assertEqual(0, result.returncode, result.stderr)
                self.assertEqual(
                    "docs_only=false\nforce_all=true\nintegration=true\nbrowser=true\nmedia=true\n",
                    result.stdout,
                )

    def test_invalid_target_and_usage_fail_without_success_outputs(self) -> None:
        for refs in ((), (self.base,), (self.base, "missing-head"), (self.base, "--stat")):
            with self.subTest(refs=refs):
                result = self.scope(*refs)

                self.assertNotEqual(0, result.returncode)
                self.assertEqual("", result.stdout)
                self.assertTrue(result.stderr)

    def test_scope_uses_exact_commits_and_preserves_unusual_path_names(self) -> None:
        self.write("docs/设计 \tline\nbreak.md")
        head = self.commit("documentation")
        self.write("source.py")
        self.commit("later code")
        self.write("Makefile")

        result = self.scope(self.base, head)

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(
            "docs_only=true\nforce_all=false\nintegration=false\nbrowser=false\nmedia=false\n",
            result.stdout,
        )

    def test_deleted_paths_still_determine_scope(self) -> None:
        for name, expected in (
            (
                "docs/guide.md",
                "docs_only=true\nforce_all=false\nintegration=false\nbrowser=false\nmedia=false\n",
            ),
            (
                "source.py",
                "docs_only=false\nforce_all=false\nintegration=false\nbrowser=false\nmedia=false\n",
            ),
            (
                "Makefile",
                "docs_only=false\nforce_all=true\nintegration=true\nbrowser=true\nmedia=true\n",
            ),
        ):
            with self.subTest(path=name):
                self.write(name)
                base = self.commit("existing file")
                self.git("rm", name)

                result = self.scope(base, self.commit("delete file"))

                self.assertEqual(0, result.returncode, result.stderr)
                self.assertEqual(expected, result.stdout)

    def test_empty_diff_does_not_claim_to_be_documentation(self) -> None:
        result = self.scope(self.base, self.base)

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(
            "docs_only=false\nforce_all=false\nintegration=false\nbrowser=false\nmedia=false\n",
            result.stdout,
        )


if __name__ == "__main__":
    unittest.main()
