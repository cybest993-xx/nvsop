from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from check_change_size import budget_violations, parse_numstat


class ChangeSizeBudgetTest(unittest.TestCase):
    def test_parses_numstat_and_treats_binary_as_zero(self) -> None:
        text = "12\t3\tapps/control-api/src/factory_sop/app.py\n-\t-\tdocs/image.png\n"
        self.assertEqual(
            {Path("apps/control-api/src/factory_sop/app.py"): 12, Path("docs/image.png"): 0},
            parse_numstat(text),
        )

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

    def test_rejects_change_at_the_budget(self) -> None:
        added = {
            Path("apps/control-api/src/factory_sop/app.py"): 500,
            Path("scripts/check_x.py"): 300,
        }
        errors = budget_violations(added)
        self.assertEqual(1, len(errors))
        self.assertIn("change adds 800 implementation lines; the budget is 800", errors[0])
        self.assertIn("apps/control-api/src/factory_sop/app.py (+500)", errors[0])

    def test_judgment_logic_has_the_tighter_budget(self) -> None:
        added = {
            Path("apps/edge-runtime/src/edge_runtime/judgment/core.py"): 100,
            Path("apps/edge-runtime/src/edge_runtime/supervisor/loop.py"): 400,
        }
        errors = budget_violations(added)
        self.assertEqual(1, len(errors))
        self.assertIn("the budget is 500 because it touches judgment logic", errors[0])


if __name__ == "__main__":
    unittest.main()
