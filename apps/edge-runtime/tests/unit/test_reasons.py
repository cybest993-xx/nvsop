"""Reason codes classify into exactly the three verdicts, and the set stays complete.

These assertions exist because the core is the reason codes' single producer: the
OpenAPI enumeration and the front end's hint table are derived from it. A code added
without a verdict, or a code silently deleted, would move ground those derivations
stand on.
"""

from __future__ import annotations

import unittest

from edge_runtime.judgment import (
    INDETERMINATE_REASONS,
    VIOLATION_REASONS,
    ReasonCode,
    Verdict,
)


class ReasonCodeClassificationTest(unittest.TestCase):
    def test_every_code_belongs_to_exactly_one_class(self) -> None:
        self.assertEqual(frozenset(ReasonCode), INDETERMINATE_REASONS | VIOLATION_REASONS)
        self.assertEqual(frozenset(), INDETERMINATE_REASONS & VIOLATION_REASONS)

    def test_no_code_classifies_as_pass(self) -> None:
        # A passing instance carries no reason code: there is no reason to explain.
        self.assertEqual([], [code for code in ReasonCode if code.verdict is Verdict.PASS])

    def test_the_violation_kinds_are_the_four_named_in_the_glossary(self) -> None:
        self.assertEqual(
            {
                ReasonCode.WRONG_STEP,
                ReasonCode.MISSED_STEP,
                ReasonCode.OUT_OF_ORDER,
                ReasonCode.DEADLINE_EXCEEDED,
            },
            VIOLATION_REASONS,
            "the glossary defines exactly four violation kinds; a fifth failing code "
            "would mean a violation kind exists that CONTEXT.md does not name",
        )

    def test_the_first_batch_of_indeterminate_codes_is_present(self) -> None:
        self.assertEqual(
            {
                ReasonCode.STREAM_LOST,
                ReasonCode.INFERENCE_BACKEND_UNREACHABLE,
                ReasonCode.INFERENCE_TIMEOUT,
                ReasonCode.TIMESTAMP_DISCONTINUITY,
                ReasonCode.CHUNK_BACKLOG_EXCEEDED,
                ReasonCode.ACTION_ID_UNKNOWN,
                ReasonCode.INFERENCE_HOST_DOWN,
                ReasonCode.RUN_INTERRUPTED,
                ReasonCode.IO_SIGNAL_LOST,
                ReasonCode.IO_TIME_UNALIGNED,
            },
            INDETERMINATE_REASONS,
            "adding an indeterminate code is compatible and may update this set; "
            "removing one is breaking (ADR-0003)",
        )

    def test_the_wire_value_is_the_code_name(self) -> None:
        # Clients match on the wire string. Keeping it identical to the member name
        # means a rename cannot silently change the contract.
        for code in ReasonCode:
            self.assertEqual(code.name, code.value)

    def test_every_code_carries_a_non_empty_human_hint(self) -> None:
        # hint 是 OpenAPI 元数据与 Web 常量的唯一来源。缺提示的码会从两者中静默消失。
        for code in ReasonCode:
            self.assertTrue(
                isinstance(code.hint, str) and code.hint.strip(),
                f"{code.value} must carry a non-empty hint",
            )

    def test_from_wire_resolves_known_codes_and_rejects_unknown_ones(self) -> None:
        # 跨进程 wire 边界不得用 from_wire。它把未知码渲染为原始码而不是抛错。
        self.assertIs(ReasonCode.STREAM_LOST, ReasonCode.from_wire("STREAM_LOST"))
        with self.assertRaises(KeyError):
            ReasonCode.from_wire("FUTURE_REASON")


if __name__ == "__main__":
    unittest.main()
