"""Center 所依赖的原 NVIDIA 增强控制面语义；计算与任务库为外部 seam。"""

from __future__ import annotations

import ast
import tempfile
import unittest
from pathlib import Path

from augmentation_http_fixture import BASE, NvidiaAugmentationHttpServer
from httpx2 import Client


class AugmentationContractTest(unittest.TestCase):
    def test_response_identity_completion_list_and_no_dispatched_cancel(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            label = root / "synthetic-label"
            clips = label / "video-1_source"
            clips.mkdir(parents=True)
            (label / "actions.json").write_text('{"actions":["(1) move"]}')
            (clips / "01_clip.mp4").write_bytes(b"synthetic input; computation is isolated")
            with (
                NvidiaAugmentationHttpServer(root) as server,
                Client(base_url=server.url, trust_env=False, timeout=5) as client,
            ):
                accepted = client.post(
                    "/api/v1/augment",
                    params={
                        "label_data_id": "synthetic-label",
                        "two_operator_mode": False,
                        "frame_drop_iterations": 0,
                    },
                )
                self.assertEqual(accepted.status_code, 200, accepted.text)
                identity = accepted.json()["dataset_id"]
                self.assertEqual(identity, "synthetic-label_augmented_0")
                observed = client.get(f"/api/v1/augmentation_status/{identity}")
                self.assertEqual(observed.json()["status"], "running")
                self.assertEqual(client.get("/api/v1/augmented_datasets").json(), {})
                self.assertEqual(list((root / identity).iterdir()), [])
                server.rows[identity].status = "completed"
                self.assertIn(identity, client.get("/api/v1/augmented_datasets").json())
                next_result = client.post(
                    "/api/v1/augment", params={"label_data_id": "synthetic-label"}
                )
                self.assertEqual(next_result.json()["dataset_id"], "synthetic-label_augmented_1")
                self.assertTrue((root / identity).is_dir())
        # 检查整份原 app，而不是夹具挑选的路由，避免把缺失的夹具接口当作基座证据。
        tree = ast.parse((BASE / "app.py").read_text())
        paths = [
            decorator.args[0].value
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            for decorator in node.decorator_list
            if isinstance(decorator, ast.Call)
            and decorator.args
            and isinstance(decorator.args[0], ast.Constant)
            and isinstance(decorator.args[0].value, str)
        ]
        self.assertFalse(any("cancel" in path for path in paths))


if __name__ == "__main__":
    unittest.main()
