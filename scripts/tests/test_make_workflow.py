from __future__ import annotations

import os
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CHECKS = [
    "policy-test",
    "policy",
    "migrations",
    "contract-base",
    "contract-capability",
    "contracts-python-format",
    "contracts-python-lint",
    "contracts-python-type",
    "contracts-python-unit",
    "boundaries",
    "secret-scan",
    "center-format",
    "center-lint",
    "center-type",
    "center-unit",
    "edge-format",
    "edge-lint",
    "edge-type",
    "edge-unit",
    "edge-integration",
    "web-format",
    "web-lint",
    "web-type",
    "web-unit",
    "web-build",
]
PREPARATION = [
    "annotation-lock-check",
    "lockfile",
    "sync",
    "hooks",
    "web-install",
    "openapi-export",
    "openapi-compat",
    "openapi-generate",
]

# 替换耗时命令，不替换 Make 的依赖图、递归执行和退出码传播。
PROBE = """\
import os
import sys
import time
from pathlib import Path

name = sys.argv[1]
trace = Path('trace')
finished = trace.read_text().splitlines() if trace.exists() else []
required = {
    'lockfile': ['annotation-lock-check'],
    'sync': ['lockfile'],
    'openapi-export': ['sync'],
    'openapi-compat': ['openapi-export'],
    'openapi-generate': ['openapi-export', 'web-install'],
}.get(name, [])
if name in os.environ['CHECKS'].split():
    required = ['sync', 'hooks', 'openapi-compat', 'openapi-generate']
if name in ('center-integration', 'center-system'):
    required = ['sync']
assert all('end ' + item in finished for item in required), (name, required, finished)
def record(event):
    with trace.open('a') as output:
        output.write(event + ' ' + name + '\\n')
record('start')
time.sleep(0.01)
if name == os.environ.get('FAIL_TARGET'):
    sys.exit(7)
record('end')
"""


class MakeWorkflowTest(unittest.TestCase):
    def run_gate(
        self, target: str, *, jobs: int = 2, failure: str = ""
    ) -> tuple[subprocess.CompletedProcess[str], list[str]]:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stubs = "\n".join(
                f"{name}:\n\t@python3 probe.py $@"
                for name in [*PREPARATION, *CHECKS, "center-integration", "center-system"]
            )
            (root / "Makefile").write_text((ROOT / "Makefile").read_text() + "\n" + stubs)
            (root / "probe.py").write_text(PROBE)
            result = subprocess.run(
                ["make", "--jobs=2", target, f"CHECK_JOBS={jobs}"],
                cwd=root,
                env=os.environ | {"CHECKS": " ".join(CHECKS), "FAIL_TARGET": failure},
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            trace = root / "trace"
            return result, trace.read_text().splitlines() if trace.exists() else []

    def test_parallel_gate_preserves_all_checks_and_preparation_order(self) -> None:
        for jobs in (1, 2):
            with self.subTest(jobs=jobs):
                result, events = self.run_gate("check", jobs=jobs)
                self.assertEqual(0, result.returncode, result.stdout + result.stderr)
                completed = [
                    line.removeprefix("end ") for line in events if line.startswith("end ")
                ]
                self.assertCountEqual(PREPARATION + CHECKS, completed)
                active = peak = 0
                for line in events:
                    if line.split()[1] in CHECKS:
                        active += 1 if line.startswith("start ") else -1
                        peak = max(peak, active)
                self.assertEqual(0, active)
                self.assertLessEqual(peak, jobs)

    def test_setup_and_child_failures_propagate_to_each_gate(self) -> None:
        for target, failure in (
            ("check", "annotation-lock-check"),
            ("check-docs", "annotation-lock-check"),
            ("check-integration", "annotation-lock-check"),
            ("check", "sync"),
            ("check", "openapi-generate"),
            ("check", "center-unit"),
            ("check-integration", "center-system"),
        ):
            with self.subTest(target=target, failure=failure):
                result, events = self.run_gate(target, failure=failure)
                self.assertNotEqual(0, result.returncode, result.stdout)
                if failure in ("annotation-lock-check", "sync", "openapi-generate"):
                    self.assertFalse(
                        any(line == f"start {name}" for name in CHECKS for line in events)
                    )
        result, events = self.run_gate("check-integration")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertCountEqual(
            ["annotation-lock-check", "lockfile", "sync", "center-integration", "center-system"],
            [line.removeprefix("end ") for line in events if line.startswith("end ")],
        )

    def test_cache_is_shared_but_environments_and_test_outputs_are_local(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            probe = root / "probe.mk"
            probe.write_text(
                textwrap.dedent("""\
                cache-probe:
                \t@printf '%s\\n' '$(UV_CACHE_DIR)' '$(UV_PROJECT_ENVIRONMENT)' '$(PYTEST)'
                """)
            )
            environment = {key: value for key, value in os.environ.items() if key != "UV_CACHE_DIR"}
            environment["XDG_CACHE_HOME"] = str(root / "shared")
            for task in ("first", "second"):
                worktree = root / task
                worktree.mkdir()
                command = [
                    "make",
                    "--no-print-directory",
                    "-s",
                    "-f",
                    str(ROOT / "Makefile"),
                    "-f",
                    str(probe),
                    "cache-probe",
                ]
                result = subprocess.run(
                    command,
                    cwd=worktree,
                    env=environment,
                    capture_output=True,
                    text=True,
                    check=True,
                ).stdout.splitlines()
                self.assertEqual(str(root / "shared/uv"), result[0])
                self.assertEqual(str(worktree / ".nvsop/venv"), result[1])
                self.assertIn(str(worktree / ".nvsop/cache/pytest/cache-probe"), result[2])
                self.assertIn(str(worktree / ".nvsop/artifacts/pytest/cache-probe.xml"), result[2])
                overridden = subprocess.run(
                    command,
                    cwd=worktree,
                    env=environment | {"UV_CACHE_DIR": str(root / "override")},
                    capture_output=True,
                    text=True,
                    check=True,
                ).stdout.splitlines()
                self.assertEqual(str(root / "override"), overridden[0])

    def test_web_unit_uses_test_mode_and_build_keeps_environment(self) -> None:
        # 父进程 NODE_ENV=production 时，web-unit 必须显式选择 test，web-build 仍继承 production。
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bin_dir = root / "bin"
            bin_dir.mkdir()
            fake_pnpm = bin_dir / "pnpm"
            fake_pnpm.write_text(
                "#!/bin/sh\nprintf 'pnpm NODE_ENV=%s\\n' \"${NODE_ENV-<unset>}\"\n"
            )
            fake_pnpm.chmod(0o755)
            environment = os.environ | {
                "NODE_ENV": "production",
                "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            }
            unit = subprocess.run(
                ["make", "--no-print-directory", "-f", str(ROOT / "Makefile"), "web-unit"],
                cwd=root,
                env=environment,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            build = subprocess.run(
                ["make", "--no-print-directory", "-f", str(ROOT / "Makefile"), "web-build"],
                cwd=root,
                env=environment,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            self.assertEqual(0, unit.returncode, unit.stdout + unit.stderr)
            self.assertIn("pnpm NODE_ENV=test", unit.stdout)
            self.assertEqual(0, build.returncode, build.stdout + build.stderr)
            self.assertIn("pnpm NODE_ENV=production", build.stdout)
