"""NVIDIA DDM 读取器的采样与帧读取契约测试。"""

# 这些名称和动态参数只是兼容基座依赖的最小替身，不是产品接口。
# ruff: noqa: ANN401, N802, UP037

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
MODULE_PATH = (
    BASE / "microservices/sop-training-bp/microservices/ddm-training-ms/ddm/"
    "DDM-Net/datasets/ddm_dataset.py"
)
UTILITY_PATH = (
    BASE / "microservices/sop-training-bp/microservices/ddm-training-ms/utils/dataset_utils.py"
)


class _NumArray(list[int]):
    def __add__(self, other: int) -> "_NumArray":
        return _NumArray(value + other for value in self)

    def __radd__(self, other: int) -> "_NumArray":
        return self + other


class _LabelArray:
    def __init__(self, size: int) -> None:
        self.values = [0] * size

    def __setitem__(self, key: slice | int, value: int) -> None:
        self.values[key] = (
            [value] * len(range(*key.indices(len(self.values))))
            if isinstance(key, slice)
            else value
        )

    def __getitem__(self, key: slice | int) -> Any:
        return self.values[key]


class _BoolArray(list[bool]):
    def __eq__(self, other: object) -> list[bool]:  # type: ignore[override]
        return [value == other for value in self]


class _Tensor:
    def __init__(self, values: Any) -> None:
        self.values = values

    def numpy(self) -> _BoolArray:
        return _BoolArray(self.values)

    def __getitem__(self, key: Any) -> Any:
        return self.values[key]


class _Transforms:
    @staticmethod
    def Compose(_items: Any) -> Any:
        return lambda value: value

    @staticmethod
    def Resize(_value: Any) -> Any:
        return lambda value: value

    @staticmethod
    def ToDtype(_dtype: Any, *, scale: bool) -> Any:
        return lambda value: value

    @staticmethod
    def Normalize(**_kwargs: Any) -> Any:
        return lambda value: value


def _numpy_module() -> types.ModuleType:
    module = types.ModuleType("numpy")
    module.ndarray = _NumArray
    module.arange = lambda start, stop=None, step=1: _NumArray(
        range(start, stop if stop is not None else start, step)
        if stop is not None
        else range(start)
    )
    module.clip = lambda values, lower, upper: _NumArray(
        max(lower, min(upper, value)) for value in values
    )
    module.zeros = lambda size: _LabelArray(size)
    module.array = lambda values, dtype=None: list(values)
    module.where = lambda values: ([index for index, value in enumerate(values) if value],)
    module.object = object
    return module


def _torch_modules() -> dict[str, types.ModuleType]:
    data = types.ModuleType("torch.utils.data")
    data.Dataset = object
    dataloader = types.ModuleType("torch.utils.data.dataloader")
    dataloader.default_collate = lambda values: values
    utils = types.ModuleType("torch.utils")
    utils.data = data
    torch = types.ModuleType("torch")
    torch.utils = utils
    torch.uint8 = object()
    torch.float32 = object()
    torch.LongTensor = lambda values: _Tensor(list(values))
    torch.stack = lambda values, dim=0: _Tensor(list(values))
    torch.zeros = lambda *args, **kwargs: _Tensor([])
    torch.from_numpy = lambda value: _Tensor(value)
    torch.tensor = lambda value: _Tensor(value)
    return {
        "torch": torch,
        "torch.utils": utils,
        "torch.utils.data": data,
        "torch.utils.data.dataloader": dataloader,
    }


def _stubs() -> dict[str, types.ModuleType]:
    torchvision = types.ModuleType("torchvision")
    transforms = types.ModuleType("torchvision.transforms")
    transforms.Compose = _Transforms.Compose
    transforms.Resize = _Transforms.Resize
    transforms.ToDtype = _Transforms.ToDtype
    transforms.Normalize = _Transforms.Normalize
    torchvision.transforms = transforms
    tqdm = types.ModuleType("tqdm")
    tqdm.tqdm = lambda values: values
    qwen = types.ModuleType("qwen_vl_utils")
    qwen.fetch_image = lambda value: value
    transformers = types.ModuleType("transformers")
    transformers.AutoProcessor = object
    values = {
        "numpy": _numpy_module(),
        "torchvision": torchvision,
        "torchvision.transforms": transforms,
        "tqdm": tqdm,
        "qwen_vl_utils": qwen,
        "transformers": transformers,
    }
    values.update(_torch_modules())
    return values


class DdmReaderContractTest(unittest.TestCase):
    def test_base_aggregator_preserves_concurrent_annotation_entries(self) -> None:
        spec = importlib.util.spec_from_file_location("nvsop_base_ddm_utils", UTILITY_PATH)
        self.assertIsNotNone(spec)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "video-a.mp4").write_bytes(b"synthetic")
            annotation_directory = root / "video-a"
            annotation_directory.mkdir()
            concurrent = [
                {
                    "start_timestamp": 0.0,
                    "end_timestamp": 0.5,
                    "descriptions": ["(1) 取料", "(2) 安装"],
                    "is_concurrent": True,
                }
            ]
            (annotation_directory / "video-a_annotation.json").write_text(
                json.dumps(concurrent, ensure_ascii=False), encoding="utf-8"
            )

            output = module.generate_ddm_annotation(str(root), "annotation.json")

            self.assertEqual(
                {"video-a": concurrent}, json.loads(Path(output).read_text(encoding="utf-8"))
            )

    def test_base_reader_decodes_frames_and_keeps_both_sample_classes(self) -> None:
        stubs = _stubs()
        previous = {name: sys.modules.get(name) for name in stubs}
        sys.modules.update(stubs)
        try:
            with TemporaryDirectory() as directory:
                root = Path(directory)
                (root / "video-a.mp4").write_bytes(b"synthetic")
                annotation = root / "annotation.json"
                annotation.write_text(
                    json.dumps(
                        {
                            "video-a": [
                                {
                                    "start_timestamp": 0.0,
                                    "end_timestamp": 0.4,
                                    "description": "(1) 取料",
                                },
                                {
                                    "start_timestamp": 0.6,
                                    "end_timestamp": 1.0,
                                    "description": "(2) 安装",
                                },
                            ]
                        }
                    ),
                    encoding="utf-8",
                )
                spec = importlib.util.spec_from_file_location("nvsop_base_ddm_reader", MODULE_PATH)
                self.assertIsNotNone(spec)
                assert spec is not None and spec.loader is not None
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)

                class Decoder:
                    metadata = types.SimpleNamespace(average_fps=10.0, duration_seconds=1.0)

                    def __init__(self, _path: str) -> None:
                        return None

                    def __len__(self) -> int:
                        return 10

                    def get_frames_at(self, indices: Any, return_pil: bool = False) -> Any:
                        return types.SimpleNamespace(data=_Tensor(list(indices)), pil=[])

                    def close(self) -> None:
                        return None

                module.PyAVVideoDecoder = Decoder
                module.PYAV_AVAILABLE = True
                dataset = module.DDMDataset(
                    mode="train",
                    anno_path=str(annotation),
                    data_root=root,
                    frames_per_side=1,
                    downsample=1,
                    min_change_dur=0.1,
                    video_backend="pyav",
                )
                self.assertGreater(len(dataset), 0)
                labels = {int(item["label"]) for item in dataset.seqs}
                self.assertEqual(labels, {0, 1})
                sample = dataset[0]
                self.assertIn("label", sample)
                self.assertIn("path", sample)
        finally:
            for name, value in previous.items():
                if value is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = value


if __name__ == "__main__":
    unittest.main()
