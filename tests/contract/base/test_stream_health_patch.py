"""§5.9 第二族：登记补丁及其依赖的基座前提。

S010 用 stream epoch barrier 统一 source error、PTS reset、chunk emission 与 internal-vLLM
frame wait。契约机械检查登记 diff 可逆、替换点受限且与工作树同步。
"""

from __future__ import annotations

import ast
import subprocess
import unittest
from pathlib import Path

from base_harness import DETECTOR, INFERENCE_ROOT, REPO_ROOT, function_source, read

PROCESS = DETECTOR / "ds_sop_process.py"
PATCH = REPO_ROOT / "docs/base/patches/0001-stream-health-events.patch"
HOOK_ENTRYPOINT = "from edge_runtime.stream_health import STREAM_HEALTH_KEY, note_pipeline_message"
ORDERING_QUEUE = "self._chunk_queue"
FUTURE_QUEUE = "self._vlm_response_future_queue"
OUTPUT_QUEUE = "self._vlm_response_queue"


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


class RecordedPatchStaysWithinRegisteredSeamsTest(unittest.TestCase):
    """机械核验登记补丁只替换批准的 chunk/frame owner 行。"""

    def setUp(self) -> None:
        self.patch = read(PATCH)

    def test_replaced_base_lines_are_limited_to_registered_owner_operations(self) -> None:
        allowed = {
            "self.decoded_frame_queue.put("
            "(timestamp, wall_clock_entry, torch_tensor), block=block)",
            "dropped_timestamp, _, _ = dropped_frame",
            "self.decoded_frame_queue.put("
            "(timestamp, wall_clock_entry, torch_tensor), block=False)",
            "self._chunk_queue.put("
            "self._make_chunk_info(chunk_idx, clip_start, end, 1.0, tm.elapsed_time))",
            "self._chunk_queue.put("
            "self._make_chunk_info(chunk_idx, clip_start, last_ts, 1.0, tm.elapsed_time))",
            "self._chunk_queue.put(chunk_info)",
            "frame = decoded_frame_queue.get(block=True)",
            "timestamp, wall_clock, tensor = frame",
        }
        removed = {line.strip() for line in removed_lines(self.patch)}
        self.assertEqual(allowed, removed)

    def test_the_recorded_patch_matches_what_is_in_the_tree(self) -> None:
        source = read(PROCESS)
        for block in added_blocks(self.patch):
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

    def test_source_transitions_open_barrier_before_health_enqueue(self) -> None:
        callback = function_source(PROCESS, "on_message")
        self.assertIn("PipelineState.INVALID, PipelineState.PLAYING", callback)
        self.assertIn("self._stream_barrier_open = True", callback)
        self.assertLess(
            callback.index("self._advance_stream_epoch_locked()"),
            callback.index("note_pipeline_message("),
        )

    def test_pts_reset_advances_epoch_before_recovered_frame_enqueue(self) -> None:
        source = function_source(PROCESS, "consume")
        marker = "self._sop_video_processor._advance_stream_epoch(source_anchor=wall_clock_entry)"
        self.assertIn(marker, source)
        self.assertLess(
            source.index(marker),
            source.index(
                "self.decoded_frame_queue.put("
                "(frame_epoch, timestamp, wall_clock_entry, torch_tensor), block=block)"
            ),
        )

    def test_chunk_emissions_are_epoch_guarded(self) -> None:
        uniform = function_source(PROCESS, "uniform_clip_post_process")
        ddm = function_source(PROCESS, "clip_post_process")
        helper = function_source(PROCESS, "_put_chunk_if_current")
        current_timestamp = function_source(PROCESS, "has_current_timestamp")
        self.assertIn("_stream_barrier_open", helper)
        self.assertIn("stream_epoch != self._stream_epoch", helper)
        self.assertIn("_put_chunk_if_current", uniform)
        self.assertIn("_put_chunk_if_current", ddm)
        self.assertIn("current_epoch != stream_epoch", uniform)
        self.assertIn("current_epoch != stream_epoch", ddm)
        self.assertIn(
            "_last_timestamp_epoch == self._sop_video_processor._stream_epoch", current_timestamp
        )
        self.assertIn("clip_start = last_ts if has_current_timestamp else None", uniform)

    def test_internal_vllm_retires_stale_descriptors(self) -> None:
        request = function_source(PROCESS, "vlm_inference_request_process")
        submit = function_source(PROCESS, "submit_vllm_inference")
        response = function_source(PROCESS, "vlm_inference_response_process")
        self.assertIn("chunk_epoch != self._stream_epoch", request)
        last_timestamp = function_source(PROCESS, "last_timestamp")
        self.assertIn('chunk_info["_stream_stale"] = True', submit)
        self.assertIn("stream_epoch != self._stream_epoch", submit)
        self.assertIn("frame_epoch, timestamp, wall_clock, tensor = frame", submit)
        self.assertIn("if frame_epoch != stream_epoch:", submit)
        self.assertIn(
            "_last_timestamp_epoch != self._sop_video_processor._stream_epoch", last_timestamp
        )
        self.assertIn('chunk_info.get("_stream_epoch", self._stream_epoch)', response)
        self.assertLess(
            response.index("response_future.result()"),
            response.rindex('chunk_info.get("_stream_epoch", self._stream_epoch)'),
        )

    def test_ddm_pts_observation_does_not_depend_on_the_vlm_backend(self) -> None:
        source = function_source(PROCESS, "start")
        self.assertIn("else self.clip_post_process", source)
        self.assertLess(source.index("clip_fn ="), source.index("if not DISABLE_VLM_INFERENCE:"))


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


class HookStaysAtCallbackTailTest(unittest.TestCase):
    """健康 hook 保持在基座原分支之后，并在状态迁移 barrier 内入队。"""

    def setUp(self) -> None:
        self.callback = function_source(PROCESS, "on_message")

    def test_the_hook_remains_after_base_branches(self) -> None:
        self.assertLess(
            self.callback.index("self._boundary_queue.put(None)"),
            self.callback.index("# --- 记录补丁"),
        )
        self.assertIn("self._stream_barrier_open = False", self.callback)

    def test_barrier_and_non_barrier_paths_use_the_same_hook(self) -> None:
        self.assertEqual(2, self.callback.count("note_pipeline_message("))
        self.assertEqual(2, self.callback.count(f"sink={ORDERING_QUEUE}"))

    def test_the_base_branches_are_untouched(self) -> None:
        # The two facts the base's own callback acts on. Our patch adds output beside them;
        # a subtree update that removed either would change what the pipeline does on
        # failure, which is a premise worth rereading rather than silently inheriting.
        self.assertIn("StateTransitionMessage", self.callback)
        self.assertIn("EOSMessage", self.callback)
        self.assertIn("self._boundary_queue.put(None)", self.callback)

    def test_the_hook_is_handed_the_ordering_queue(self) -> None:
        self.assertIn(f"sink={ORDERING_QUEUE}", self.callback)


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


class OrderedHealthPathTest(unittest.TestCase):
    """§5.11：健康事实与动作必须共享既有 FIFO，并只旁路不适用的 VLM 计算。"""

    def setUp(self) -> None:
        self.source = read(PROCESS)

    def test_hook_writes_health_to_the_same_chunk_fifo_as_actions(self) -> None:
        callback = function_source(PROCESS, "on_message")
        self.assertIn(f"sink={ORDERING_QUEUE}", callback)
        self.assertIn("self._stream_barrier_open = True", callback)

    def test_vlm_request_loop_forwards_health_without_running_inference(self) -> None:
        request = function_source(PROCESS, "vlm_inference_request_process")
        branch = "if STREAM_HEALTH_KEY in chunk_info:"
        self.assertIn(branch, request)
        self.assertIn("self._vlm_response_future_queue.put(chunk_info)", request)
        self.assertLess(request.index(branch), request.index('chunk_info["start_time"]'))

    def test_vlm_response_loop_forwards_health_without_action_fields(self) -> None:
        response = function_source(PROCESS, "vlm_inference_response_process")
        branch = "if STREAM_HEALTH_KEY in chunk_info:"
        self.assertIn(branch, response)
        self.assertIn("self._vlm_response_queue.put(chunk_info)", response)
        self.assertLess(
            response.index(branch), response.index('chunk_info.pop("response_future", None)')
        )

    def test_supported_vlm_modes_reach_the_same_sse_output_chain(self) -> None:
        selector = function_source(PROCESS, "inference_last_queue")
        self.assertIn(f"if DISABLE_VLM_INFERENCE:\n            return {ORDERING_QUEUE}", selector)
        self.assertIn(f"elif DISABLE_SOP_CHECKER:\n            return {OUTPUT_QUEUE}", selector)
        response = function_source(PROCESS, "vlm_inference_response_process")
        self.assertIn(f"chunk_info = {FUTURE_QUEUE}.get(block=True)", response)
        self.assertIn(f"{OUTPUT_QUEUE}.put(chunk_info)", response)

    def test_the_base_checker_and_disposal_still_default_to_off(self) -> None:
        for flag, default in (
            ('DISABLE_SOP_CHECKER", "false', "off by default, we set it true"),
            ('ENABLE_ALERT_SOUND", "false', "left at its default"),
            ('ENABLE_MESSAGING", "false', "left at its default"),
        ):
            with self.subTest(flag=flag):
                self.assertIn(f'os.getenv("{flag}")', self.source, default)

    def test_dispatch_loop_strips_private_epoch_before_output(self) -> None:
        dispatch = function_source(PROCESS, "post_dispatch_process")
        self.assertIn('chunk.pop("_stream_epoch", None)', dispatch)
        self.assertIn('chunk.pop("_stream_stale", None)', dispatch)
        self.assertIn("self._final_queue.put(chunk)", dispatch)


if __name__ == "__main__":
    unittest.main()
