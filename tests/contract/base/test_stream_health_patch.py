"""§5.9 第二族：登记补丁及其依赖的基座前提。

ADR-0007 的推理补丁仍只触及一个基座文件，登记 import、健康事件有序旁路、internal-vLLM
旧帧清理和 uniform/DDM 时间轴处理；契约机械检查补丁只追加、可逆且与工作树同步。

`ds_sop_process.py` 依赖 torch/pyservicemaker，因此测试只读源码、不导入基座运行时，保持
纯 CPU；每次 `git subtree pull` 后必须重跑。
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


class RecordedPatchIsAppendOnlyTest(unittest.TestCase):
    """机械核验 ADR-0007 的登记补丁纪律，而不是依赖人工评审记忆。

    补丁允许为合成健康事件增加显式旁路，但不得删除或改写基座原行；否则 subtree 更新时
    冲突面已超出登记边界，必须显式失败。
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
            8,
            len(blocks),
            "expected eight added runs: import, queue hygiene, hook, ordered bypasses, uniform state/init, and DDM reset",
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

    def test_internal_vllm_queue_is_cleared_before_the_reset_frame_is_enqueued(self) -> None:
        source = function_source(PROCESS, "consume")
        self.assertIn("timeline_reset = self._last_timestamp > 0 and timestamp < self._last_timestamp", source)
        self.assertIn("self.decoded_frame_queue.get(block=False)", source)
        self.assertLess(
            source.index("if timeline_reset:"),
            source.index("self.decoded_frame_queue.put((timestamp, wall_clock_entry, torch_tensor), block=block)"),
        )

    def test_uniform_pts_regression_reanchors_and_resets_chunk_state(self) -> None:
        source = function_source(PROCESS, "uniform_clip_post_process")
        self.assertIn("if previous_ts is not None and last_ts < previous_ts:", source)
        self.assertIn("self.first_timestamp = self._tm_e2e.now()", source)
        self.assertIn("clip_start = last_ts", source)

    def test_ddm_pts_regression_reanchors_and_resets_boundary_state(self) -> None:
        source = function_source(PROCESS, "clip_post_process")
        self.assertIn("if self._clip_cur_sec > 0 and pts < self._clip_cur_sec:", source)
        self.assertIn("self.first_timestamp = self._tm_e2e.now()", source)
        self.assertIn("self._clip_start_sec = pts", source)
        self.assertIn("boundaries.clear()", source)
        self.assertIn("delayed_frames = 0", source)
        self.assertIn("need_check_delayed = False", source)
        self.assertIn("is_ready = False", source)

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
        self.assertLess(response.index(branch), response.index('chunk_info.pop("response_future", None)'))

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

    def test_dispatch_loop_still_forwards_the_selected_output(self) -> None:
        dispatch = function_source(PROCESS, "post_dispatch_process")
        self.assertIn("self._final_queue.put(chunk)", dispatch)


if __name__ == "__main__":
    unittest.main()
