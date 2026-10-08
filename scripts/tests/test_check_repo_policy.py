from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from check_repo_policy import (
    center_boundary_violations,
    check_repository,
    check_retention_duration_literals,
)


class RepositoryPolicyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.files = {
            Path(".gitignore"): ".nvsop/\n.env\n.env.*\n*.key\n*.pem\n",
            Path("AGENTS.md"): (
                "[Docs](docs/README.md)\n[Architecture](docs/engineering/architecture.md)\n"
            ),
            Path("CONTEXT.md"): "# Language\n",
            Path("Makefile"): "check:\n\ttrue\n",
            Path("docs/README.md"): (
                "[Language](../CONTEXT.md)\n[Workflow](engineering/workflow.md)\n"
                "[Documentation](engineering/documentation.md)\n"
                "[Design](design/solution-and-roadmap.md)\n"
            ),
            Path("docs/engineering/architecture.md"): "# Architecture\n",
            Path("docs/engineering/workflow.md"): "# Workflow\n",
            Path("docs/engineering/documentation.md"): "# Documentation\n",
            Path("docs/design/solution-and-roadmap.md"): "# Current decisions\n",
            Path(".github/workflows/blocking-ci.yml"): (
                "workflow_call:\nCI required\nalways()\nmake check\n"
                "ref: ${{ inputs.head_sha }}\n"
                "if: needs.scope.outputs.integration == 'true'\n"
            ),
            Path(".github/workflows/landing-queue.yml"): (
                "issue_comment:\nworkflow_dispatch:\ncancel-in-progress: false\n"
                "guard:\nvars.LANDING_APP_ID != ''\nvars.LANDING_QUEUE_ISSUE != ''\n"
                "needs.guard.outputs.enabled == 'true'\n"
                "environment: landing\n"
                "actions/create-github-app-token@"
                "fee1f7d63c2ff003460e3d139729b119787bc349\n"
                "scripts/landing_queue.py prepare\n"
                "scripts/landing_queue.py finalize\n"
                "uses: ./.github/workflows/blocking-ci.yml\n"
                "ref: ${{ github.workflow_sha }}\n"
            ),
            Path("scripts/landing_queue.py"): "# landing queue owner\n",
            Path("scripts/ci_scope.py"): (
                'INTEGRATION_PREFIXES = ("apps/edge-runtime/", "tests/system/")\n'
            ),
            Path(".python-version"): "3.12\n",
            Path("uv.lock"): "version = 1\n",
            Path("pyproject.toml"): (
                '[tool.uv.workspace]\nmembers = ["apps/control-api"]\n'
                '[tool.nvsop]\ncenter_modules = ["auth"]\n'
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

    def center_boundaries(self, *paths: Path) -> list[str]:
        return center_boundary_violations(self.root, list(paths))

    def test_rejects_top_level_tests_in_unittest_owners(self) -> None:
        for directory in (
            "apps/edge-runtime/tests/unit",
            "apps/edge-runtime/tests/integration",
            "packages/contracts/tests/unit",
            "scripts/tests",
            "tests/contract/base",
            "tests/contract/capability",
        ):
            for definition in ("def", "async def"):
                with self.subTest(directory=directory, definition=definition):
                    path = self.write(
                        f"{directory}/test_discovery.py", f"{definition} test_lost():\n    pass\n"
                    )
                    self.assertIn(
                        f"unittest cannot collect top-level test: {path}:1: "
                        "test_lost; use TestCase",
                        self.check(str(path)),
                    )

    def test_accepts_unittest_methods_helpers_and_pytest_owner(self) -> None:
        unittest_path = self.write(
            "apps/edge-runtime/tests/unit/test_discovery.py",
            "import unittest\ndef fixture():\n    pass\n"
            "class Example(unittest.TestCase):\n    def test_collected(self):\n        pass\n",
        )
        pytest_path = self.write(
            "apps/control-api/tests/unit/test_discovery.py", "def test_collected():\n    pass\n"
        )
        self.assertEqual([], self.check(str(unittest_path), str(pytest_path)))

    def test_accepts_minimum_harness(self) -> None:
        self.assertEqual([], self.check())

    def test_rejects_blocking_ci_legacy_triggers_and_caller_expressions(self) -> None:
        # queue-only：legacy 触发器与 caller 表达式都必须拒绝，候选头只能来自控制器输入。
        self.write(
            ".github/workflows/blocking-ci.yml",
            "pull_request:\npush:\nworkflow_call:\nCI required\nalways()\nmake check\n"
            "ref: ${{ inputs.head_sha }}\n"
            "if: needs.scope.outputs.integration == 'true'\n",
        )
        errors = self.check()
        self.assertIn(
            "blocking-ci.yml must be reusable-only; remove the legacy pull_request: trigger",
            errors,
        )
        self.assertIn(
            "blocking-ci.yml must be reusable-only; remove the legacy push: trigger", errors
        )

        self.write(
            ".github/workflows/blocking-ci.yml",
            "workflow_call:\nCI required\nalways()\nmake check\n"
            "ref: ${{ inputs.head_sha || github.sha }}\n"
            "if: needs.scope.outputs.integration == 'true'\n",
        )
        self.assertIn(
            "blocking-ci.yml must resolve head/base from workflow_call inputs only", self.check()
        )

    def test_rejects_a_shared_selector_that_omits_system_tests(self) -> None:
        system_test = self.write("tests/system/test_case.py", "")
        self.write(
            "scripts/ci_scope.py",
            'INTEGRATION_PREFIXES = ("apps/control-api/", "apps/edge-runtime/")\n',
        )
        self.assertIn(
            "ci_scope.py integration selection must include tests/system/ and apps/edge-runtime/; "
            "path filtering is an optimization, not an exemption",
            self.check(str(system_test)),
        )

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

    def test_requires_repository_local_state_ignore_rule(self) -> None:
        (self.root / ".gitignore").write_text(".env\n")
        self.assertIn(".gitignore must ignore .nvsop/ repository-local state", self.check())

    def test_rejects_committed_repository_local_state(self) -> None:
        path = self.write(".nvsop/cache/tool/output.txt", "generated")
        self.assertIn(
            "repository-local state must not be committed: .nvsop/cache/tool/output.txt",
            self.check(str(path)),
        )

    def test_rejects_root_test_without_cross_app_owner(self) -> None:
        path = self.root / "tests/unit/test_example.py"
        path.parent.mkdir(parents=True)
        path.write_text("")
        errors = self.check(str(path.relative_to(self.root)))
        self.assertTrue(
            any("root test has no declared cross-app owner" in error for error in errors)
        )

    def test_rejects_document_without_a_reader_entry(self) -> None:
        path = self.write("docs/design/orphan.md", "# Orphan\n")
        self.assertIn(
            "unindexed Markdown document: docs/design/orphan.md; link from a reachable owner",
            self.check(str(path)),
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

    def test_rejects_lfs_filter_inside_vendor_subtree(self) -> None:
        attributes = self.write(
            "vendor/sop-monitoring-blueprints/.gitattributes",
            "*.png filter=lfs diff=lfs merge=lfs -text\n",
        )
        self.assertIn(
            "vendor/sop-monitoring-blueprints/.gitattributes:1 enables Git-LFS inside the "
            "vendored NVIDIA subtree; subtree imports do not copy upstream LFS objects",
            self.check(str(attributes)),
        )

    def test_rejects_lfs_pointer_inside_vendor_subtree(self) -> None:
        pointer = self.write(
            "vendor/sop-monitoring-blueprints/assets/diagram.png",
            "version https://git-lfs.github.com/spec/v1\n"
            "oid sha256:0000000000000000000000000000000000000000000000000000000000000000\n"
            "size 123\n",
        )
        self.assertIn(
            "vendored NVIDIA file is a Git-LFS pointer: "
            "vendor/sop-monitoring-blueprints/assets/diagram.png; "
            "exclude the upstream LFS-only asset or vendor real bytes",
            self.check(str(pointer)),
        )

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

    def write_web_toolchain(self) -> None:
        self.write(".nvmrc", "22\n")
        self.write("pnpm-lock.yaml", "lockfileVersion: '9.0'\n")
        self.write("pnpm-workspace.yaml", "packages: []\n")
        self.write("package.json", '{"packageManager":"pnpm@11.0.0"}\n')

    def test_rejects_missing_python_pin(self) -> None:
        (self.root / ".python-version").unlink()
        del self.files[Path(".python-version")]
        self.assertIn("missing required file: .python-version", self.check())

    def test_rejects_python_pin_that_is_not_the_anchored_version(self) -> None:
        self.write(".python-version", "3.13\n")
        errors = self.check()
        self.assertIn(".python-version must pin the center backend to 3.12, not 3.13", errors)

    def test_accepts_standard_library_and_own_imports_inside_edge_runtime(self) -> None:
        module = self.write(
            "apps/edge-runtime/src/edge_runtime/judgment/core.py",
            "import sqlite3\n"
            "from dataclasses import dataclass\n"
            "from edge_runtime.judgment.model import Observation\n"
            "from .reasons import ReasonCode\n",
        )
        self.assertEqual([], self.check(str(module)))

    def test_accepts_dependency_declared_by_edge_manifest(self) -> None:
        self.write(
            "apps/edge-runtime/pyproject.toml",
            '[project]\nname = "edge-runtime"\ndependencies = ["httpx2>=2.12.0"]\n',
        )
        module = self.write(
            "apps/edge-runtime/src/edge_runtime/center_client.py",
            "import httpx2\n",
        )
        self.assertEqual([], self.check(str(module)))

    def test_rejects_undeclared_third_party_import_inside_edge_runtime(self) -> None:
        # 中心依赖在共享工作区里可以导入; 只有清单声明才算准入。
        module = self.write(
            "apps/edge-runtime/src/edge_runtime/supervisor/station.py",
            "import sqlalchemy\n",
        )
        errors = self.check(str(module))
        self.assertIn(
            "apps/edge-runtime/src/edge_runtime/supervisor/station.py imports sqlalchemy, which "
            "apps/edge-runtime/pyproject.toml does not declare; add a lightweight dependency "
            "there (docs/engineering/maintenance.md) or use the standard library",
            errors,
        )

    def test_rejects_declared_dependency_inside_standard_library_boundaries(self) -> None:
        self.write(
            "apps/edge-runtime/pyproject.toml",
            '[project]\nname = "edge-runtime"\ndependencies = ["httpx2>=2.12.0"]\n',
        )
        for relative in (
            "apps/edge-runtime/src/edge_runtime/judgment/core.py",
            "apps/edge-runtime/src/edge_runtime/stream_health.py",
        ):
            with self.subTest(path=relative):
                module = self.write(relative, "import httpx2\n")
                self.assertIn(
                    f"{relative} imports httpx2; the judgment core and vendor-hook boundary are "
                    "standard-library-only (ADR-0005, edge-autonomy.md §5.11)",
                    self.check(str(module)),
                )

    def test_rejects_stream_health_importing_any_edge_sibling(self) -> None:
        module = self.write(
            "apps/edge-runtime/src/edge_runtime/stream_health.py",
            "from edge_runtime.future_adapter import EventSink\n",
        )
        errors = self.check(str(module))
        self.assertIn(
            "apps/edge-runtime/src/edge_runtime/stream_health.py:1 imports "
            "edge_runtime.future_adapter; stream_health is the vendor-hook boundary and must "
            "import no edge_runtime sibling",
            errors,
        )

    def test_rejects_stream_health_relative_sibling_import(self) -> None:
        module = self.write(
            "apps/edge-runtime/src/edge_runtime/stream_health.py",
            "from .judgment import Decision\n",
        )
        errors = self.check(str(module))
        self.assertIn(
            "apps/edge-runtime/src/edge_runtime/stream_health.py:1 imports edge_runtime.judgment; "
            "stream_health is the vendor-hook boundary and must import no edge_runtime sibling",
            errors,
        )

    def test_rejects_stream_health_package_importing_edge_sibling(self) -> None:
        module = self.write(
            "apps/edge-runtime/src/edge_runtime/stream_health/adapter.py",
            "from edge_runtime.judgment import Decision\n",
        )
        errors = self.check(str(module))
        self.assertIn(
            "apps/edge-runtime/src/edge_runtime/stream_health/adapter.py:1 imports "
            "edge_runtime.judgment; stream_health is the vendor-hook boundary and must import "
            "no edge_runtime sibling",
            errors,
        )

    def test_accepts_stream_health_package_internal_import(self) -> None:
        module = self.write(
            "apps/edge-runtime/src/edge_runtime/stream_health/adapter.py",
            "from .model import WireEvent\n",
        )
        self.assertEqual([], self.check(str(module)))

    def test_rejects_edge_module_importing_local_state_persistence_implementation(self) -> None:
        module = self.write(
            "apps/edge-runtime/src/edge_runtime/reporting.py",
            "from edge_runtime.local_state.store import LocalState\n",
        )
        errors = self.check(str(module))
        self.assertIn(
            "apps/edge-runtime/src/edge_runtime/reporting.py:1 imports "
            "edge_runtime.local_state.store; LocalState persistence implementation must stay "
            "behind edge_runtime.local_state",
            errors,
        )

    def test_rejects_edge_module_importing_private_local_state_module_alias(self) -> None:
        module = self.write(
            "apps/edge-runtime/src/edge_runtime/reporting.py",
            "from edge_runtime.local_state import store\n",
        )
        errors = self.check(str(module))
        self.assertIn(
            "apps/edge-runtime/src/edge_runtime/reporting.py:1 imports "
            "edge_runtime.local_state.store; LocalState persistence implementation must stay "
            "behind edge_runtime.local_state",
            errors,
        )

    def test_rejects_private_local_state_access_through_public_package(self) -> None:
        module = self.write(
            "apps/edge-runtime/src/edge_runtime/reporting.py",
            "from edge_runtime import local_state\nstore_type = local_state.store.LocalState\n",
        )
        errors = self.check(str(module))
        self.assertIn(
            "apps/edge-runtime/src/edge_runtime/reporting.py:2 imports "
            "edge_runtime.local_state.store; LocalState persistence implementation must stay "
            "behind edge_runtime.local_state",
            errors,
        )

    def test_rejects_private_local_state_access_through_edge_runtime_alias(self) -> None:
        module = self.write(
            "apps/edge-runtime/src/edge_runtime/reporting.py",
            "import edge_runtime as edge\n"
            "import edge_runtime.local_state\n"
            "store_type = edge.local_state.store.LocalState\n",
        )
        errors = self.check(str(module))
        self.assertIn(
            "apps/edge-runtime/src/edge_runtime/reporting.py:3 imports "
            "edge_runtime.local_state.store; LocalState persistence implementation must stay "
            "behind edge_runtime.local_state",
            errors,
        )

    def test_rejects_connector_importing_unowned_edge_state(self) -> None:
        module = self.write(
            "apps/edge-runtime/src/edge_runtime/connectors/future.py",
            "from edge_runtime.local_state.queues import StationQueues\n",
        )
        errors = self.check(str(module))
        self.assertIn(
            "apps/edge-runtime/src/edge_runtime/connectors/future.py:1 imports "
            "edge_runtime.local_state.queues; LocalState persistence implementation must stay "
            "behind edge_runtime.local_state",
            errors,
        )

    def test_rejects_write_ledger_importing_local_state(self) -> None:
        module = self.write(
            "apps/edge-runtime/src/edge_runtime/connectors/writes.py",
            "from edge_runtime.local_state.disposal import LocalDisposalLedger\n",
        )
        errors = self.check(str(module))
        self.assertIn(
            "apps/edge-runtime/src/edge_runtime/connectors/writes.py:1 imports "
            "edge_runtime.local_state.disposal; connectors may depend only on judgment and "
            "supervisor input vocabulary outside their own package",
            errors,
        )

    def test_rejects_connector_importing_supervisor_implementation(self) -> None:
        module = self.write(
            "apps/edge-runtime/src/edge_runtime/connectors/future.py",
            "from edge_runtime.supervisor.station import StationSupervisor\n",
        )
        errors = self.check(str(module))
        self.assertIn(
            "apps/edge-runtime/src/edge_runtime/connectors/future.py:1 imports "
            "edge_runtime.supervisor.station; connectors may depend only on judgment and "
            "supervisor input vocabulary outside their own package",
            errors,
        )

    def test_rejects_connector_importing_allowed_prefix_lookalike(self) -> None:
        module = self.write(
            "apps/edge-runtime/src/edge_runtime/connectors/future.py",
            "from edge_runtime.judgment_adapter import Decision\n",
        )
        errors = self.check(str(module))
        self.assertIn(
            "apps/edge-runtime/src/edge_runtime/connectors/future.py:1 imports "
            "edge_runtime.judgment_adapter; connectors may depend only on judgment and "
            "supervisor input vocabulary outside their own package",
            errors,
        )

    def test_rejects_non_root_importing_every_edge_runtime_package(self) -> None:
        module = self.write(
            "apps/edge-runtime/src/edge_runtime/future_runtime.py",
            "from edge_runtime import connectors, judgment, local_state, stream_health, "
            "supervisor\n",
        )
        errors = self.check(str(module))
        self.assertIn(
            "apps/edge-runtime/src/edge_runtime/future_runtime.py imports all Edge runtime "
            "packages; only edge_runtime/runtime.py may be the composition root",
            errors,
        )

    def test_accepts_the_approved_standard_library_contract_inside_edge_runtime(self) -> None:
        contract = self.write(
            "packages/contracts/src/nvsop_contracts/capability.py",
            "from dataclasses import dataclass\n",
        )
        module = self.write(
            "apps/edge-runtime/src/edge_runtime/connectors/port.py",
            "from nvsop_contracts import Capability\n",
        )
        self.assertEqual([], self.check(str(contract), str(module)))

    def test_rejects_third_party_import_inside_shared_contracts(self) -> None:
        contract = self.write(
            "packages/contracts/src/nvsop_contracts/capability.py",
            "import pydantic\n",
        )
        errors = self.check(str(contract))
        self.assertIn(
            "packages/contracts/src/nvsop_contracts/capability.py imports pydantic, which is "
            "not in the standard library; shared contracts imported by the inference host "
            "must stay standard-library-only",
            errors,
        )

    def test_rejects_center_module_with_no_import_linter_contract(self) -> None:
        # 文件规模仅提示；模块边界仍须由契约强制。
        module = self.write(
            "apps/control-api/src/factory_sop/auth/usecases/open_session.py",
            "session = None\n" * 1_000,
        )
        errors = self.check(str(module))
        self.assertEqual(
            [
                "center module auth has no import-linter contract in pyproject.toml; "
                "a module whose boundary is not named by a contract is unenforced; "
                "add a forbidden or layers contract that constrains factory_sop.auth "
                "(docs/engineering/architecture.md#enforcement)"
            ],
            errors,
        )

    def test_unregistered_shared_namespace_is_not_a_product_module_by_directory(self) -> None:
        infrastructure = self.write(
            "apps/control-api/src/factory_sop/observability/logger.py",
            "value = 1\n",
        )
        self.assertEqual([], self.check(str(infrastructure)))

    def test_accepts_center_module_named_by_an_import_linter_contract(self) -> None:
        self.write(
            "pyproject.toml",
            '[tool.uv.workspace]\nmembers = ["apps/control-api"]\n\n'
            '[tool.nvsop]\ncenter_modules = ["auth"]\n\n'
            "[[tool.importlinter.contracts]]\n"
            'name = "auth is reached only through its api"\n'
            'type = "forbidden"\n'
            'source_modules = ["factory_sop.auth"]\n',
        )
        module = self.write("apps/control-api/src/factory_sop/auth/usecases/open_session.py", "")
        self.assertEqual([], self.check(str(module)))

    def test_center_module_contract_requires_a_constraining_type_and_field(self) -> None:
        # 只有真实 type 的受约束字段能证明模块边界；描述、被禁目标、例外和近似前缀都不算。
        module = "apps/control-api/src/factory_sop/auth/usecases/open_session.py"
        base = (
            '[tool.uv.workspace]\nmembers = ["apps/control-api"]\n\n'
            '[tool.nvsop]\ncenter_modules = ["auth"]\n\n'
        )
        unenforced = (
            "center module auth has no import-linter contract in pyproject.toml; "
            "a module whose boundary is not named by a contract is unenforced; "
            "add a forbidden or layers contract that constrains factory_sop.auth "
            "(docs/engineering/architecture.md#enforcement)"
        )
        rejected = {
            "name only": 'type = "forbidden"\nname = "factory_sop.auth boundary"\n',
            "forbidden_modules only": (
                'type = "forbidden"\nforbidden_modules = ["factory_sop.auth"]\n'
            ),
            "ignore_imports only": (
                'type = "forbidden"\nignore_imports = ["factory_sop.auth -> factory_sop.beta"]\n'
            ),
            "prefix collision": 'type = "forbidden"\nsource_modules = ["factory_sop.auth_extra"]\n',
            "no type": 'source_modules = ["factory_sop.auth"]\n',
            "unsupported type": 'type = "mystery"\nsource_modules = ["factory_sop.auth"]\n',
        }
        for label, contract in rejected.items():
            with self.subTest(contract=label):
                self.write(
                    "pyproject.toml",
                    base + "[[tool.importlinter.contracts]]\n" + contract,
                )
                path = self.write(module, "")
                self.assertEqual([unenforced], self.check(str(path)))

    def test_center_module_contract_accepts_real_constraining_fields(self) -> None:
        module = "apps/control-api/src/factory_sop/auth/usecases/open_session.py"
        base = (
            '[tool.uv.workspace]\nmembers = ["apps/control-api"]\n\n'
            '[tool.nvsop]\ncenter_modules = ["auth"]\n\n'
        )
        accepted = {
            "forbidden submodule": (
                'type = "forbidden"\nsource_modules = ["factory_sop.auth.model"]\n'
            ),
            "independence modules": 'type = "independence"\nmodules = ["factory_sop.auth"]\n',
            "layers entry": 'type = "layers"\nlayers = ["factory_sop.auth"]\n',
            "layers independent pair": (
                'type = "layers"\nlayers = ["factory_sop.settings | factory_sop.auth"]\n'
            ),
            "layers non-independent pair": (
                'type = "layers"\nlayers = ["factory_sop.settings : factory_sop.auth"]\n'
            ),
        }
        for label, contract in accepted.items():
            with self.subTest(contract=label):
                self.write(
                    "pyproject.toml",
                    base + "[[tool.importlinter.contracts]]\n" + contract,
                )
                path = self.write(module, "")
                self.assertEqual([], self.check(str(path)))

    def test_center_boundary_evaluator_rejects_cross_owner_internal_imports(self) -> None:
        self.write(
            "pyproject.toml",
            '[tool.nvsop]\ncenter_modules = ["alpha", "beta"]\n',
        )
        module = self.write(
            "apps/control-api/src/factory_sop/alpha/usecases.py",
            "from factory_sop.beta.model import Thing\nfrom ..beta.repository import Repository\n",
        )
        self.assertEqual(
            [
                "apps/control-api/src/factory_sop/alpha/usecases.py:1 registered center module "
                "alpha imports factory_sop.beta.model; cross-owner production imports must use "
                "factory_sop.beta.api",
                "apps/control-api/src/factory_sop/alpha/usecases.py:2 registered center module "
                "alpha imports factory_sop.beta.repository; cross-owner production imports must "
                "use factory_sop.beta.api",
            ],
            self.center_boundaries(module),
        )

    def test_repository_policy_blocks_cross_owner_internal_imports(self) -> None:
        self.write(
            "pyproject.toml",
            '[tool.uv.workspace]\nmembers = ["apps/control-api"]\n\n'
            '[tool.nvsop]\ncenter_modules = ["alpha", "beta"]\n\n'
            "[[tool.importlinter.contracts]]\n"
            'name = "registered owners"\n'
            'type = "forbidden"\n'
            'source_modules = ["factory_sop.alpha", "factory_sop.beta"]\n',
        )
        module = self.write(
            "apps/control-api/src/factory_sop/alpha/usecases.py",
            "from factory_sop.beta.model import Thing\n",
        )
        self.assertIn(
            "apps/control-api/src/factory_sop/alpha/usecases.py:1 registered center module "
            "alpha imports factory_sop.beta.model; cross-owner production imports must use "
            "factory_sop.beta.api",
            self.check(str(module)),
        )

    def test_center_boundary_evaluator_accepts_public_api_and_composition_root(self) -> None:
        self.write(
            "pyproject.toml",
            '[tool.nvsop]\ncenter_modules = ["alpha", "beta"]\n',
        )
        module = self.write(
            "apps/control-api/src/factory_sop/alpha/usecases.py",
            "from factory_sop.beta.api import Summary\n",
        )
        composition = self.write(
            "apps/control-api/src/factory_sop/app.py",
            "from factory_sop.beta.adapters.routes import router\n",
        )
        infrastructure = self.write(
            "apps/control-api/src/factory_sop/observability/__init__.py",
            "value = 1\n",
        )
        self.assertEqual([], self.center_boundaries(module, composition, infrastructure))

    def test_center_boundary_evaluator_rejects_hidden_owner_shape(self) -> None:
        self.write(
            "pyproject.toml",
            '[tool.nvsop]\ncenter_modules = ["alpha", "beta"]\n',
        )
        hidden = self.write(
            "apps/control-api/src/factory_sop/gamma/usecases.py",
            "def assemble() -> None:\n    pass\n",
        )
        self.assertEqual(
            [
                "apps/control-api/src/factory_sop/gamma/usecases.py gives unregistered center "
                "namespace gamma a product-owner shape; ownership must be resolved explicitly "
                "rather than inferred outside [tool.nvsop].center_modules"
            ],
            self.center_boundaries(hidden),
        )

    def test_rejects_literal_authorization_header_in_json(self) -> None:
        manifest = self.write(
            ".mcp.json",
            '{"headers": {"Authorization": "Bearer osr_2SqXwDyR9qsZ4SRAk2rcgr"}}\n',
        )
        self.assertIn(
            ".mcp.json:1 commits a literal Authorization header; reference the "
            'credential as "${VAR}" and supply it from the environment',
            self.check(str(manifest)),
        )

    def test_accepts_authorization_header_that_references_the_environment(self) -> None:
        manifest = self.write(
            ".mcp.json", '{"headers": {"Authorization": "Bearer ${ONE_SEARCH_TOKEN}"}}\n'
        )
        self.assertEqual([], self.check(str(manifest)))


class RetentionDurationLiteralTest(unittest.TestCase):
    """切片/回收业务路径的保留/窗口时长不得写死（§5.19）。"""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def write(self, relative: str, content: str) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return Path(relative)

    def test_rejects_a_literal_duration_in_a_designated_slicing_path(self) -> None:
        path = self.write(
            "apps/edge-runtime/src/edge_runtime/media_retention.py",
            "retention_seconds = 7 * 24 * 60 * 60\n",
        )
        errors = check_retention_duration_literals(self.root, [path])
        self.assertEqual(1, len(errors))
        self.assertIn("hardcodes a retention/window duration", errors[0])

    def test_accepts_config_values_defaults_and_paths_outside_the_set(self) -> None:
        config = self.write(
            "apps/edge-runtime/src/edge_runtime/media.py",
            "window = configuration.recording_window_seconds\n"
            "port = 8080\n"
            "DEFAULT_RETENTION_SECONDS = 604800\n",
        )
        other = self.write("apps/edge-runtime/src/edge_runtime/other.py", "retention = 604800\n")
        self.assertEqual([], check_retention_duration_literals(self.root, [config, other]))


if __name__ == "__main__":
    unittest.main()
