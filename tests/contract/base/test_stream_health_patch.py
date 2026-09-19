"""Family two (§5.9): the recorded patch, and the base premises it stands on.

The registered inference-base modification (ADR-0007) is three appended blocks in one base
file: one import, one decoded-PTS reset observation, and one call at the end of the serving
pipeline's message callback. Everything here is about that patch staying append-only and
about the base facts that make the chosen sink and source anchor work.

Standard library only, pure CPU: `ds_sop_process.py` imports torch and pyservicemaker, so
it is read as source rather than imported. Mandatory after every `git subtree pull`, where
a red test here means the base moved under the patch.
"""

from __future__ import annotations

import ast
import subprocess
import unittest
from pathlib import Path

from base_harness import DETECTOR, INFERENCE_ROOT, REPO_ROOT, function_source, read

PROCESS = DETECTOR / "ds_sop_process.py"
PATCH = REPO_ROOT / "docs/base/patches/0001-stream-health-events.patch"
HOOK_ENTRYPOINT = "from edge_runtime.stream_health import note_pipeline_message"
SINK = "self._vlm_response_queue"


def added_blocks(patch: str) -> list[str]:
    """The contiguous runs of lines the recorded patch adds, without the diff marker.

    Runs rather than lines: asserting line by line would let a blank added line pass on any
    file, and would not notice a block that landed somewhere else entirely.
    """
    blocks: list[list[str]] = []
    run: list[str] = []
    for line in patch.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            run.append(line[1:])
            continue
        if run:
            blocks.append(run)
            run = []
    if run:
        blocks.append(run)
    return ["\n".join(block) for block in blocks]


def removed_lines(patch: str) -> list[str]:
    return [
        line[1:]
        for line in patch.splitlines()
        if line.startswith("-") and not line.startswith("---")
    ]


class RecordedPatchIsAppendOnlyTest(unittest.TestCase):
    """The patch discipline ADR-0007 rests on, checked mechanically rather than by review.

    Purely additive observation is what lets NVIDIA's performance work and CUDA/DeepStream
    version adaptation keep arriving by `subtree pull`. A patch that started deleting or rewriting
    base lines would forfeit that without anything failing, so it fails here.
    """

    def setUp(self) -> None:
        self.patch = read(PATCH)

    def test_the_patch_deletes_no_base_line(self) -> None:
        self.assertEqual(
            [],
            removed_lines(self.patch),
            "the recorded patch must only add lines; a deletion means it entered the "
            "base's control flow and ADR-0007's premise no longer holds",
        )

    def test_the_recorded_patch_matches_what_is_in_the_tree(self) -> None:
        # The ledger and the tree drifting apart is the failure this catches: the patch is
        # the artifact a reviewer reads and a `subtree pull` conflict is resolved against.
        source = read(PROCESS)
        blocks = added_blocks(self.patch)
        self.assertEqual(
            3,
            len(blocks),
            "expected three appended blocks: import, PTS re-anchor, and hook call",
        )
        for block in blocks:
            self.assertIn(
                block,
                source,
                f"the recorded patch adds a block that is not in {PROCESS.name} verbatim; "
                "regenerate docs/base/patches/0001-stream-health-events.patch",
            )

    def test_the_recorded_patch_can_be_reversed_from_the_current_tree(self) -> None:
        result = subprocess.run(
            ["git", "apply", "--reverse", "--check", str(PATCH)],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(0, result.returncode, result.stderr)

    def test_the_patch_touches_exactly_one_base_file(self) -> None:
        headers = [line for line in self.patch.splitlines() if line.startswith("+++ ")]
        self.assertEqual(1, len(headers), f"expected one patched file, got {headers}")
        self.assertIn("ds_sop_process.py", headers[0])

    def test_pts_regression_reanchors_the_source_timeline(self) -> None:
        consume = function_source(PROCESS, "consume")
        self.assertIn(
            "if self._last_timestamp > 0 and timestamp < self._last_timestamp:",
            consume,
        )
        self.assertIn(
            "self._sop_video_processor.first_timestamp = wall_clock_entry",
            consume,
        )


class HookIsTheOnlyReachIntoOurCodeTest(unittest.TestCase):
    """核验 vendor 只调用登记的入口，避免 hook 扩大对 edge_runtime 内部的依赖。

    此处直接检查未纳入本仓库 lint/type 覆盖的基座源码；包间依赖另由 import-linter 检查，
    所有权规则见 docs/engineering/architecture.md。
    """

    def base_files_importing_our_code(self) -> dict[Path, list[str]]:
        found: dict[Path, list[str]] = {}
        for path in sorted(INFERENCE_ROOT.rglob("*.py")):
            lines = [
                line.strip()
                for line in read(path).splitlines()
                if "edge_runtime" in line and not line.strip().startswith("#")
            ]
            if lines:
                found[path] = lines
        return found

    def test_one_base_file_imports_one_module_of_ours(self) -> None:
        self.assertEqual(
            {PROCESS: [HOOK_ENTRYPOINT]},
            self.base_files_importing_our_code(),
            "the hook must be one import of the designated stream-health entrypoint; "
            "anything else widens the patch surface ADR-0007 fixes at one call",
        )

    def test_the_hook_module_depends_on_nothing_of_ours(self) -> None:
        # The entrypoint is the boundary, so it cannot be a doorway: if it imported the
        # judgment core, the contract above would be satisfied while `vendor/` transitively
        # reached all of `edge_runtime`.
        module = REPO_ROOT / "apps/edge-runtime/src/edge_runtime/stream_health.py"
        tree = ast.parse(read(module))
        imported = {
            name.split(".")[0]
            for node in ast.walk(tree)
            for name in (
                [alias.name for alias in node.names]
                if isinstance(node, ast.Import)
                else [node.module or ""]
                if isinstance(node, ast.ImportFrom)
                else []
            )
        }
        self.assertNotIn("edge_runtime", imported)
        self.assertEqual(
            set(),
            imported - {"collections", "dataclasses", "enum", "time", "typing", "__future__"},
            "the hook's module runs inside the DeepStream container, so it is standard "
            "library only (§5.11)",
        )


class HookAddsNoControlFlowTest(unittest.TestCase):
    """The call is appended to the callback, not woven into it.

    The base's callback releases a waiting starter (`_started_event`) and pushes the
    end-of-stream sentinel. If our call sat before those, an exception in it would hang a
    pipeline start; last, every base branch has already run.
    """

    def setUp(self) -> None:
        self.callback = function_source(PROCESS, "on_message")

    def test_the_hook_is_the_last_statement_of_the_callback(self) -> None:
        body = ast.parse(self.callback.strip()).body[0]
        assert isinstance(body, ast.FunctionDef)
        last = body.body[-1]
        self.assertIsInstance(last, ast.Expr, "the hook call must be the callback's last statement")
        self.assertIn("note_pipeline_message", ast.unparse(last))

    def test_the_hook_is_called_once(self) -> None:
        self.assertEqual(1, self.callback.count("note_pipeline_message("))

    def test_the_base_branches_are_untouched(self) -> None:
        # The two facts the base's own callback acts on. Our patch adds output beside them;
        # a subtree update that removed either would change what the pipeline does on
        # failure, which is a premise worth rereading rather than silently inheriting.
        self.assertIn("StateTransitionMessage", self.callback)
        self.assertIn("EOSMessage", self.callback)
        self.assertIn("self._boundary_queue.put(None)", self.callback)

    def test_the_hook_is_handed_the_one_usable_sink(self) -> None:
        self.assertIn(f"sink={SINK}", self.callback.replace(" ", "").replace("\n", ""))


class MessageTypesTheHookDuckTypesTest(unittest.TestCase):
    """What the hook reads off a message, pinned from the base's side.

    `stream_health.py` cannot import `pyservicemaker` — it must stay runnable on a bare CPU
    — so it recognizes a message by its class name and its `new_state` attribute. That makes
    the base's own use of those names the contract, and this is where it is checked.
    """

    def setUp(self) -> None:
        self.source = read(PROCESS)

    def test_the_base_still_imports_the_message_types_by_those_names(self) -> None:
        self.assertIn(
            "from pyservicemaker import BufferRetriever, EOSMessage, PipelineState, "
            "StateTransitionMessage",
            self.source,
            "the hook recognizes end-of-stream by the class name 'EOSMessage'; if the base "
            "renamed it, stream health would silently stop being reported",
        )

    def test_the_base_still_reads_state_transitions_through_new_state(self) -> None:
        self.assertIn("message.new_state ==", self.source)

    def test_the_states_the_hook_classifies_are_still_the_ones_the_base_uses(self) -> None:
        for state in ("PipelineState.PLAYING", "PipelineState.INVALID"):
            with self.subTest(state=state):
                self.assertIn(state, self.source)


class SinkStaysReachableTest(unittest.TestCase):
    """§5.11: the chosen sink works only while these three facts hold.

    `_vlm_response_queue` was the only usable one of three candidates, and what makes it
    usable is that with the base checker off it *is* the last queue, and that every consumer
    on the path reads its keys defensively. A new consumer that does not check our key would
    break silently, which is why the consumer set is pinned rather than the queue alone.
    """

    def setUp(self) -> None:
        self.source = read(PROCESS)

    def test_disabling_the_base_checker_still_routes_the_queue_to_the_output(self) -> None:
        # We disable the base checker by configuration rather than by patch (ADR-0007).
        # That is what puts our synthetic chunk on the SSE: with it set, the queue we feed
        # is the queue `post_dispatch_process` forwards to the client.
        selector = function_source(PROCESS, "inference_last_queue")
        self.assertIn("if DISABLE_VLM_INFERENCE:", selector)
        self.assertIn(f"elif DISABLE_SOP_CHECKER:\n            return {SINK}", selector)

    def test_the_base_checker_and_disposal_still_default_to_off(self) -> None:
        # Not a patch, a default: E4 leaves both response surfaces as delivered.
        for flag, default in (
            ('DISABLE_SOP_CHECKER", "false', "off by default, we set it true"),
            ('ENABLE_ALERT_SOUND", "false', "left at its default"),
            ('ENABLE_MESSAGING", "false', "left at its default"),
        ):
            with self.subTest(flag=flag):
                self.assertIn(f'os.getenv("{flag}")', self.source, default)

    def test_the_registered_consumers_of_the_queue_are_unchanged(self) -> None:
        # §5.9 names this assertion: our event is distinguished by a key, so a consumer that
        # does not check for it would read the event as a chunk of work. Two `get()` call
        # sites are registered — the base checker's loop, which our configuration never
        # starts, and the dispatch loop that forwards to the SSE.
        consumers = {
            name
            for name in ("sop_checker_process", "post_dispatch_process")
            if f"{SINK}.get(" in function_source(PROCESS, name)
            or "inference_last_queue.get(" in function_source(PROCESS, name)
        }
        self.assertEqual({"sop_checker_process", "post_dispatch_process"}, consumers)
        self.assertEqual(
            2,
            self.source.count(f"{SINK}.get(") + self.source.count("inference_last_queue.get("),
            "an unregistered consumer of the queue appeared; it must be checked for the "
            "stream-health key or our synthetic chunk will be read as a chunk of work",
        )

    def test_the_dispatch_loop_still_forwards_whatever_it_receives(self) -> None:
        # It must stay a pass-through: it is what carries our event to the SSE, and it also
        # fires the base's disposal surfaces, which is why those stay off by default.
        dispatch = function_source(PROCESS, "post_dispatch_process")
        self.assertIn("self._final_queue.put(chunk)", dispatch)


if __name__ == "__main__":
    unittest.main()
