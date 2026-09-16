"""NVIDIA VLM 读取器的最小数据契约测试。"""

# 这个替身只为隔离基座的 GPU 依赖，动态参数来自基座调用约定。
# ruff: noqa: ANN401

from __future__ import annotations

import importlib.util
import json
import sys
import types
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

BASE = Path(__file__).resolve().parents[3] / "vendor/sop-monitoring-blueprints"
MODULE_PATH = BASE / "microservices/sop-training-bp/assets/tools/cosmos_custom_dataset.py"


class _VisionConfig:
    def __init__(self, **values: Any) -> None:
        self._values = values
        for name, value in values.items():
            setattr(self, name, value)

    def model_dump(self, *, exclude_none: bool = False) -> dict[str, Any]:
        return {
            name: value
            for name, value in self._values.items()
            if not exclude_none or value is not None
        }


class _PydanticBaseModel:
    def __init__(self, **values: Any) -> None:
        for name in type(self).__annotations__:
            value = values.get(name, getattr(type(self), name, None))
            setattr(self, name, value)


class _Dataset:
    pass


def _module_stubs(captured: dict[str, Any]) -> dict[str, types.ModuleType]:
    pydantic = types.ModuleType("pydantic")
    pydantic.BaseModel = _PydanticBaseModel
    pydantic.Field = lambda default=None, **_kwargs: default
    text = types.ModuleType("cosmos_reason2_utils.text")

    def create_conversation(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return kwargs

    text.create_conversation = create_conversation  # type: ignore[attr-defined]
    vision = types.ModuleType("cosmos_reason2_utils.vision")
    vision.PIXELS_PER_TOKEN = 1
    vision.VisionConfig = _VisionConfig
    cosmos_reason = types.ModuleType("cosmos_reason2_utils")
    cosmos_reason.text = text
    cosmos_reason.vision = vision

    launcher_worker = types.ModuleType("cosmos_rl.launcher.worker_entry")
    launcher_worker.main = lambda **_: None
    launcher = types.ModuleType("cosmos_rl.launcher")
    launcher.worker_entry = launcher_worker
    policy_config = types.ModuleType("cosmos_rl.policy.config")
    policy_config.Config = object
    policy = types.ModuleType("cosmos_rl.policy")
    policy.config = policy_config
    logging_module = types.ModuleType("cosmos_rl.utils.logging")
    logging_module.logger = types.SimpleNamespace(info=lambda *_args, **_kwargs: None)
    utils = types.ModuleType("cosmos_rl.utils")
    utils.logging = logging_module
    cosmos_rl = types.ModuleType("cosmos_rl")
    cosmos_rl.launcher = launcher
    cosmos_rl.policy = policy
    cosmos_rl.utils = utils

    torch_data = types.ModuleType("torch.utils.data")
    torch_data.Dataset = _Dataset
    torch = types.ModuleType("torch")
    torch_utils = types.ModuleType("torch.utils")
    torch_utils.data = torch_data
    torch.utils = torch_utils

    toml = types.ModuleType("toml")
    toml.load = lambda *_args, **_kwargs: {}
    toml.dumps = lambda value: str(value)
    return {
        "pydantic": pydantic,
        "cosmos_reason2_utils": cosmos_reason,
        "cosmos_reason2_utils.text": text,
        "cosmos_reason2_utils.vision": vision,
        "cosmos_rl": cosmos_rl,
        "cosmos_rl.launcher": launcher,
        "cosmos_rl.launcher.worker_entry": launcher_worker,
        "cosmos_rl.policy": policy,
        "cosmos_rl.policy.config": policy_config,
        "cosmos_rl.utils": utils,
        "cosmos_rl.utils.logging": logging_module,
        "torch": torch,
        "torch.utils": torch_utils,
        "torch.utils.data": torch_data,
        "toml": toml,
    }


class VlmReaderContractTest(unittest.TestCase):
    def test_base_reader_resolves_registered_video_by_basename_and_strips_tag(self) -> None:
        captured: dict[str, Any] = {}
        stubs = _module_stubs(captured)
        previous = {name: sys.modules.get(name) for name in stubs}
        sys.modules.update(stubs)
        try:
            with TemporaryDirectory() as directory:
                root = Path(directory)
                video = root / "video-a.mp4"
                video.write_bytes(b"synthetic")
                annotation = root / "annotations.json"
                annotation.write_text(
                    json.dumps(
                        [
                            {
                                "conversations": [
                                    {"from": "human", "value": "<video>\n问题"},
                                    {"from": "gpt", "value": "回答"},
                                ],
                                "video": "registered/video-a.mp4",
                            }
                        ]
                    ),
                    encoding="utf-8",
                )
                spec = importlib.util.spec_from_file_location("nvsop_base_vlm_reader", MODULE_PATH)
                self.assertIsNotNone(spec)
                assert spec is not None and spec.loader is not None
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                config = types.SimpleNamespace(
                    train=types.SimpleNamespace(
                        train_policy=types.SimpleNamespace(
                            dataset=types.SimpleNamespace(name=[str(annotation)], split=["train"])
                        )
                    )
                )
                dataset = module.CosmosSFTDataset(
                    config=config,
                    custom_config=module.CustomConfig(),
                )
                self.assertEqual(len(dataset), 1)
                dataset[0]
                self.assertEqual(captured["user_prompt"], "问题")
                self.assertEqual(captured["response"], "回答")
                self.assertEqual(captured["videos"], [str(video)])
        finally:
            for name, value in previous.items():
                if value is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = value


if __name__ == "__main__":
    unittest.main()
