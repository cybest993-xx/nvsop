"""Assertions about NVIDIA base behavior we depend on but do not change.

Family one of the two suites required after every subtree update (§5.9). These
detect a failed premise: each one pins a fact that a design decision rests on,
so a red test here means a decision needs rereading, not that the base is wrong.

Standard library only, pure CPU, no GPU or container. Modules that import
pydantic, torch, or DeepStream are inspected as source rather than imported;
only the four standard-library-only modules are imported and executed.
"""

from __future__ import annotations

import ast
import contextlib
import difflib
import importlib
import inspect
import io
import json
import logging
import os
import re
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
BASE_ROOT = REPO_ROOT / "vendor/sop-monitoring-blueprints"
INFERENCE_ROOT = BASE_ROOT / "microservices/sop-inference-bp"
DETECTOR = INFERENCE_ROOT / "nvds_action_detector"
AGENTIC_REFERENCES = BASE_ROOT / "agentic/ds-sop-skills/deepstream-sop/references"
DDM_DATASET = (
    BASE_ROOT
    / "microservices/sop-training-bp/microservices/evaluation-ms"
    / "ddm/DDM-Net/datasets/ddm_dataset.py"
)


def read(path: Path) -> str:
    if not path.is_file():
        raise AssertionError(
            f"base file is missing: {path.relative_to(REPO_ROOT)}. "
            "Update docs/base/verified-commits.md if the base moved it."
        )
    return path.read_text(encoding="utf-8")


def import_detector(module: str):
    """Import a standard-library-only module from the base package.

    The base is noisy on import: it reads `LOG_LEVEL` for its records and prints a
    one-time banner per logger unconditionally. Both are quieted here so the gate's
    output stays readable, rather than by patching `vendor/`.
    """
    if str(INFERENCE_ROOT) not in sys.path:
        sys.path.insert(0, str(INFERENCE_ROOT))
    os.environ.setdefault("LOG_LEVEL", "ERROR")
    with contextlib.redirect_stdout(io.StringIO()):
        imported = importlib.import_module(f"nvds_action_detector.{module}")
    logging.getLogger("DS_ACTION_DETECTOR").setLevel(logging.ERROR)
    return imported


def function_source(path: Path, name: str) -> str:
    tree = ast.parse(read(path))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_source_segment(read(path), node) or ""
    raise AssertionError(f"{path.name} no longer defines {name}()")


class ActionNumberExtractionTest(unittest.TestCase):
    """§5.3: our generated actions.json must keep matching the base's regex.

    Tested through behavior rather than a source grep, because what we depend on
    is the extraction result. A silently failing checker is the failure mode.
    """

    def setUp(self) -> None:
        checker = import_detector("sop_step_checker")
        self.cache = checker.SopCheckerCache()
        self.request_type = checker.SopCheckerRequest

    def run_sequence(self, actions: list[str], observations: list[str]) -> list[dict]:
        config = json.dumps({"actions": actions})
        results: list[dict] = []
        checker_id = "*"
        for observation in observations:
            response = self.cache.process_sop_check(
                "req",
                self.request_type(
                    action_json=config,
                    vlm_output=observation,
                    keep_alive=True,
                    checker_id=checker_id,
                    cycle_completion_threshold=0.6,
                    cycle_boundary_threshold_low=0.3,
                    cycle_boundary_threshold_high=0.8,
                ),
            ).asdict()
            checker_id = response["checker_id"]
            results.append(response)
        return results

    def test_numbered_action_with_trailing_text_is_extracted(self) -> None:
        actions = [f"({index}) step {index}" for index in range(1, 4)]
        results = self.run_sequence(actions, ["(1) step 1", "(2) step 2", "(3) step 3"])
        self.assertTrue(
            results[-1]["cycle_completed"],
            "a complete in-order sequence must close the base's cycle; if it does not, "
            "action-number extraction changed and our actions.json format is stale",
        )

    def test_pattern_still_requires_text_after_the_number(self) -> None:
        # The template generator always emits text after "(N)". Pinning the
        # requirement keeps a loosened regex from hiding a malformed generator.
        source = function_source(DETECTOR / "sop_step_checker.py", "process_sop_check")
        self.assertIn(
            r'r"^\((\d+)\).+"',
            source,
            "the action-number pattern moved or changed; recheck §5.3 before "
            "regenerating any actions.json",
        )


class SkippableActionSemanticsTest(unittest.TestCase):
    """§5.1 sets out to reuse `actions_can_be_skipped`; this pins what it means.

    Measured semantics: a declared-skippable action is removed from the expected
    set *and* its own observations are discarded. That is "not part of the SOP",
    not "optional step". The product needs a third state (optional, but counted
    when performed) that the base cannot express, so the judgment core must
    define it rather than inherit this. See docs/design/solution-and-roadmap.md §5.1.
    """

    def setUp(self) -> None:
        checker = import_detector("sop_step_checker")
        self.cache = checker.SopCheckerCache()
        self.request_type = checker.SopCheckerRequest
        self.config = json.dumps(
            {
                "actions": [f"({index}) step {index}" for index in range(1, 6)],
                "actions_can_be_skipped": ["(3) step 3"],
            }
        )

    def observe(self, observation: str, checker_id: str) -> dict:
        return self.cache.process_sop_check(
            "req",
            self.request_type(
                action_json=self.config,
                vlm_output=observation,
                keep_alive=True,
                checker_id=checker_id,
                cycle_completion_threshold=0.6,
                cycle_boundary_threshold_low=0.3,
                cycle_boundary_threshold_high=0.8,
            ),
        ).asdict()

    def test_skippable_action_is_dropped_from_the_expected_set(self) -> None:
        checker_id = "*"
        results = []
        for number in (1, 2, 4, 5):
            response = self.observe(f"({number}) step {number}", checker_id)
            checker_id = response["checker_id"]
            results.append(response)
        self.assertTrue(
            results[-1]["cycle_completed"],
            "with step 3 declared skippable the remaining four actions must count as "
            "the whole cycle",
        )
        self.assertEqual([], results[-1]["missing_detected"])

    def test_performing_a_skippable_action_is_discarded(self) -> None:
        first = self.observe("(1) step 1", "*")
        response = self.observe("(3) step 3", first["checker_id"])
        self.assertEqual([], response["missing_detected"])
        self.assertEqual([], response["misordered_detected"])
        self.assertFalse(
            response["cycle_completed"],
            "the base discards a skippable action's observation entirely; an optional "
            "step that must still be ordered has to be modelled by the judgment core",
        )


class CheckerRequestSignatureTest(unittest.TestCase):
    """The dual-run comparison in family two constructs this type directly."""

    def test_constructor_parameters_are_unchanged(self) -> None:
        checker = import_detector("sop_step_checker")
        parameters = inspect.signature(checker.SopCheckerRequest).parameters
        for name in (
            "action_json",
            "vlm_output",
            "keep_alive",
            "checker_id",
            "cycle_completion_threshold",
            "cycle_boundary_threshold_low",
            "cycle_boundary_threshold_high",
        ):
            self.assertIn(
                name,
                parameters,
                "SopCheckerRequest lost a field the dual-run comparison passes",
            )


class ChunkMetadataTest(unittest.TestCase):
    """§2.10: the fields our judgment core and latency accounting read."""

    def test_chunk_info_carries_the_fields_we_consume(self) -> None:
        source = read(DETECTOR / "ds_sop_process.py")
        tree = ast.parse(source)
        keys: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "_make_chunk_info":
                for inner in ast.walk(node):
                    if isinstance(inner, ast.Dict):
                        keys |= {
                            key.value
                            for key in inner.keys
                            if isinstance(key, ast.Constant) and isinstance(key.value, str)
                        }
        self.assertTrue(keys, "ds_sop_process no longer builds chunk info as a dict literal")
        for field in ("chunk_idx", "start_time", "end_time"):
            self.assertIn(field, keys, f"chunk info no longer carries {field}")

    def test_checker_result_is_attached_to_the_chunk(self) -> None:
        source = read(DETECTOR / "ds_sop_process.py")
        self.assertIn(
            '"checker_result"',
            source,
            "the checker result is no longer attached to the chunk that reaches SSE",
        )


class WarmupLoadsDdmTest(unittest.TestCase):
    """§2.7: no DDM checkpoint means no service, and `uniform` cannot avoid it."""

    def test_dummy_pipeline_attaches_an_inference_element(self) -> None:
        source = function_source(DETECTOR / "ds_3d_action_pipeline.py", "create_dummy_pipeline")
        self.assertTrue(
            "nvinferserver" in source or "nvinfer" in source,
            "the warmup pipeline no longer attaches an inference element",
        )
        self.assertIn("INFERENCE_CONFIG", source)

    def test_warmup_is_not_conditional_on_the_chunking_algorithm(self) -> None:
        tree = ast.parse(read(DETECTOR / "ds_sop_process.py"))
        calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "create_dummy_pipeline"
        ]
        self.assertTrue(calls, "ds_sop_process no longer builds the warmup pipeline")
        guarded = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.If)
            and any(
                isinstance(inner, ast.Call)
                and isinstance(inner.func, ast.Name)
                and inner.func.id == "create_dummy_pipeline"
                for inner in ast.walk(node)
            )
            and "algorithm" in ast.dump(node.test)
        ]
        self.assertEqual(
            [],
            guarded,
            "the warmup pipeline became conditional on the chunking algorithm; "
            "'uniform bypasses DDM' may now be reachable and §2.7 needs rereading",
        )


class DdmAnnotationFormatTest(unittest.TestCase):
    """§2.7: the annotation artifact `dataset` generates must stay loadable."""

    def test_segment_fields_are_unchanged(self) -> None:
        source = read(DDM_DATASET)
        for field in ("start_timestamp", "end_timestamp", "description"):
            self.assertIn(field, source, f"DDM annotation field {field} changed")

    def test_boundary_is_still_the_midpoint_between_segments(self) -> None:
        source = read(DDM_DATASET)
        self.assertTrue(
            re.search(r"\(\s*s_time\s*\+\s*e_time\s*\)\s*/\s*2\s*\*\s*fps", source),
            "the segment-boundary formula changed; the generated annotation.json "
            "would land on different frames",
        )

    def test_final_segment_is_still_filtered(self) -> None:
        self.assertIn('"final segment"', read(DDM_DATASET).lower())


class RtspReconnectTest(unittest.TestCase):
    """§2.4: the only reconnect behavior the base configures."""

    def test_initial_error_reconnect_interval_is_still_set(self) -> None:
        self.assertIn(
            "init-rtsp-reconnect-interval",
            read(DETECTOR / "ds_3d_action_pipeline.py"),
            "the RTSP reconnect setting disappeared; stream-health assumptions in "
            "§2.4 and §5.7 need rereading",
        )


class AgenticReferenceCopiesTest(unittest.TestCase):
    """The base ships near-copies of the inference modules under `agentic/`.

    Registered in docs/base/verified-commits.md as a known base defect. The
    contract suite asserts the real modules; this test pins how far each copy has
    drifted, so further drift becomes a signal rather than a silent staleness.

    When a subtree update changes a real module, this test is expected to fail.
    Reread the diff, then record the new expectation together with the new
    verified commit in the same change.
    """

    # Real module -> reference copy, with the content lines known to differ.
    # An empty tuple means the copy is currently byte-identical.
    EXPECTED_DRIFT = {
        "missing_number_detector.py": ("missing_number_detector_reference.py", ()),
        "sop_step_checker.py": (
            "sop_step_checker_reference.py",
            (
                "-            self.save_checker(checker, actions_can_be_skipped_numbers)",
                "+            # checker is not yet cached; "
                "it will be saved at keep_alive time below",
            ),
        ),
    }

    def test_reference_copy_drift_matches_the_recorded_expectation(self) -> None:
        for real_name, (copy_name, expected) in self.EXPECTED_DRIFT.items():
            with self.subTest(module=real_name):
                real = read(DETECTOR / real_name).splitlines()
                copy = read(AGENTIC_REFERENCES / copy_name).splitlines()
                drift = tuple(
                    line
                    for line in difflib.unified_diff(real, copy, n=0, lineterm="")
                    if line[:1] in {"-", "+"} and not line.startswith(("---", "+++"))
                )
                self.assertEqual(
                    expected,
                    drift,
                    f"{copy_name} drifted from {real_name} beyond the recorded "
                    "expectation. Decide whether any contract assertion depends on the "
                    "copy, then update EXPECTED_DRIFT alongside the new verified commit "
                    "in docs/base/verified-commits.md.",
                )


if __name__ == "__main__":
    unittest.main()
