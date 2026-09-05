from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from check_change_size import budget_violations, module_of, parse_numstat


class ChangeSizeBudgetTest(unittest.TestCase):
    def test_parses_numstat_and_treats_binary_as_zero(self) -> None:
        text = "12\t3\tapps/control-api/src/factory_sop/app.py\n-\t-\tdocs/image.png\n"
        self.assertEqual(
            {Path("apps/control-api/src/factory_sop/app.py"): 12, Path("docs/image.png"): 0},
            parse_numstat(text),
        )

    def test_module_assignment_follows_harness_section_3(self) -> None:
        cases = {
            Path("apps/control-api/src/factory_sop/auth/usecases/sessions.py"): "auth",
            Path("apps/control-api/src/factory_sop/auth/adapters/routes.py"): "auth/adapters",
            Path("apps/control-api/src/factory_sop/app.py"): "factory_sop",
            Path("apps/control-api/src/factory_sop/persistence/__init__.py"): "persistence",
            Path("apps/control-web/src/session/store.ts"): "session",
            Path("apps/control-web/src/modules/overview/Panel.vue"): "modules/overview",
            Path("apps/control-web/src/main.ts"): "control-web",
            Path("apps/edge-runtime/src/edge_runtime/judgment/core.py"): "judgment",
            Path("apps/edge-runtime/src/edge_runtime/supervisor/loop.py"): "supervisor",
            Path("scripts/check_repo_policy.py"): "scripts",
            Path("packages/other/src/lib.py"): "unassigned",
        }
        for path, module in cases.items():
            self.assertEqual(module, module_of(path), str(path))

    def test_counts_only_implementation_lines(self) -> None:
        # Tests, docs, the lockfile and the vendored base may be as large as they need
        # to be; the budget measures what a reviewer must hold in their head.
        added = {
            Path("apps/control-api/src/factory_sop/app.py"): 700,
            Path("apps/control-api/tests/unit/test_app.py"): 900,
            Path("docs/design/repository-harness.md"): 900,
            Path("uv.lock"): 900,
            Path("vendor/sop-monitoring-blueprints/x.py"): 900,
            Path("scripts/tests/test_x.py"): 900,
        }
        self.assertEqual([], budget_violations(added))

    def test_generated_client_output_does_not_consume_the_authored_budget(self) -> None:
        # The SDK is generated from the OpenAPI source contract. Its size is not reviewer-owned
        # implementation, but the clean-worktree gate still requires every generated file to be
        # committed and reproducible.
        added = {
            Path("apps/control-web/src/api/generated/sdk.gen.ts"): 2_000,
            Path("apps/control-api/src/factory_sop/app.py"): 799,
        }
        self.assertEqual([], budget_violations(added))

    def test_budget_is_per_module_not_per_change(self) -> None:
        # Several modules may grow in one pull request: what a reviewer holds is each
        # module's own growth, not a sum across modules that share no seam.
        added = {
            Path("apps/control-api/src/factory_sop/auth/usecases/sessions.py"): 700,
            Path("apps/control-web/src/session/store.ts"): 700,
            Path("scripts/check_x.py"): 700,
        }
        self.assertEqual([], budget_violations(added))

    def test_rejects_only_the_module_that_crossed_its_budget(self) -> None:
        added = {
            Path("apps/control-api/src/factory_sop/auth/usecases/sessions.py"): 500,
            Path("apps/control-api/src/factory_sop/auth/errors.py"): 300,
            Path("apps/control-web/src/session/store.ts"): 700,
        }
        errors = budget_violations(added)
        self.assertEqual(1, len(errors))
        self.assertIn("module auth adds 800 implementation lines; the budget is 800", errors[0])
        self.assertIn("auth/usecases/sessions.py (+500)", errors[0])

    def test_a_modules_adapters_are_budgeted_separately_from_its_behavior(self) -> None:
        # Harness §3 keeps adapters outside the behavior they adapt, so each side is held
        # to the budget on its own instead of charging one module for both.
        added = {
            Path("apps/control-api/src/factory_sop/auth/usecases/sessions.py"): 750,
            Path("apps/control-api/src/factory_sop/auth/adapters/routes.py"): 750,
        }
        self.assertEqual([], budget_violations(added))

    def test_undeclared_roots_accumulate_in_one_loud_bucket(self) -> None:
        # A source root missing from MODULE_ROOTS must not hide its growth: everything
        # undeclared accumulates until the root is declared.
        added = {
            Path("packages/other/src/lib.py"): 850,
            Path("packages/other/src/helper.py"): 100,
        }
        errors = budget_violations(added)
        self.assertEqual(1, len(errors))
        self.assertIn("module unassigned adds 950 implementation lines", errors[0])

    def test_judgment_module_has_the_tighter_budget_and_others_keep_the_plain_one(self) -> None:
        added = {
            Path("apps/edge-runtime/src/edge_runtime/judgment/core.py"): 500,
            Path("apps/edge-runtime/src/edge_runtime/supervisor/loop.py"): 800,
        }
        errors = budget_violations(added)
        self.assertEqual(2, len(errors))
        self.assertIn("module judgment adds 500 implementation lines; the budget is 500", errors[0])
        self.assertIn("because it touches judgment logic", errors[0])
        self.assertIn(
            "module supervisor adds 800 implementation lines; the budget is 800", errors[1]
        )

    def test_web_slices_are_budgeted_one_per_slice(self) -> None:
        added = {
            Path("apps/control-web/src/modules/overview/Panel.vue"): 850,
            Path("apps/control-web/src/modules/other/Panel.vue"): 850,
        }
        errors = budget_violations(added)
        self.assertEqual(2, len(errors))
        self.assertIn("module modules/other adds 850", errors[0])
        self.assertIn("module modules/overview adds 850", errors[1])


if __name__ == "__main__":
    unittest.main()
