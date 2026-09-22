"""复用 NVIDIA DDM 标注聚合函数的适配器。"""

from __future__ import annotations

import importlib.util
import json
from collections.abc import Mapping
from pathlib import Path
from types import ModuleType
from typing import cast

from factory_sop.dataset.usage import (
    DdmReaderInputError,
    DdmReaderUnavailableError,
    DdmSampleClassEmptyError,
)

_BASE_READER = (
    "sop-monitoring-blueprints/microservices/sop-training-bp/"
    "microservices/ddm-training-ms/ddm/DDM-Net/datasets/ddm_dataset.py"
)
_BASE_UTILITY = (
    "sop-monitoring-blueprints/microservices/sop-training-bp/"
    "microservices/ddm-training-ms/utils/dataset_utils.py"
)


class NvidiaDdmReader:
    """调用 NVIDIA DDM 训练读取器验证真实样本消费。"""

    def __init__(self, reader_path: Path | None = None) -> None:
        self._reader_path = reader_path or _find_base_reader()
        self._module: ModuleType | None = None

    def available(self) -> bool:
        """确认当前 worker 环境具备基座读取器依赖。"""
        return all(
            importlib.util.find_spec(name) is not None
            for name in (
                "torch",
                "numpy",
                "torchvision",
                "tqdm",
                "qwen_vl_utils",
                "transformers",
                "av",
            )
        )

    def sample_counts(
        self,
        *,
        workspace: Path,
        annotation_filename: str,
        parameters: Mapping[str, object],
    ) -> Mapping[str, int]:
        """构造原读取器并实际读取边界与非边界样本。"""
        try:
            module = self._load_module()
        except (FileNotFoundError, ImportError, OSError, RuntimeError) as error:
            raise DdmReaderUnavailableError from error
        reader = getattr(module, "DDMDataset", None)
        if not callable(reader):
            raise DdmReaderUnavailableError
        try:
            annotation = json.loads((workspace / annotation_filename).read_text(encoding="utf-8"))
            if not isinstance(annotation, dict) or not annotation:
                raise DdmReaderInputError
            dataset = reader(
                mode=str(parameters["mode"]),
                anno_path=str(workspace / annotation_filename),
                data_root=workspace,
                num_classes=cast(int, parameters["num_classes"]),
                frames_per_side=cast(int, parameters["frames_per_side"]),
                downsample=cast(int, parameters["downsample"]),
                min_change_dur=cast(float, parameters["min_change_dur"]),
                video_backend=cast(str, parameters["video_backend"]),
            )
            expected_video_ids = set(annotation)
            video_info = getattr(dataset, "video_info", {})
            if not isinstance(video_info, dict) or set(video_info) != expected_video_ids:
                raise DdmReaderInputError
            if any(
                not isinstance(info, dict)
                or not isinstance(info.get("fps"), (int, float))
                or isinstance(info.get("fps"), bool)
                or not isinstance(info.get("vlen"), int)
                or isinstance(info.get("vlen"), bool)
                or info["vlen"] < 3
                for info in video_info.values()
            ):
                raise DdmReaderInputError
            sequences = getattr(dataset, "seqs", ())
            sampled_video_ids = {
                str(item["video_id"])
                for item in sequences
                if isinstance(item, dict) and "video_id" in item
            }
            if sampled_video_ids != expected_video_ids:
                raise DdmReaderInputError
            labels = [int(item["label"]) for item in sequences]
            counts = {
                "boundary_sample_count": sum(label == 1 for label in labels),
                "non_boundary_sample_count": sum(label == 0 for label in labels),
            }
            if not counts["boundary_sample_count"] or not counts["non_boundary_sample_count"]:
                raise DdmSampleClassEmptyError
            dataset[0]
            return counts
        except DdmReaderInputError:
            raise
        except ZeroDivisionError as error:
            raise DdmSampleClassEmptyError from error
        except (
            AssertionError,
            IndexError,
            KeyError,
            TypeError,
            ValueError,
        ) as error:
            raise DdmReaderInputError from error
        except ImportError as error:
            raise DdmReaderUnavailableError from error

    def _load_module(self) -> ModuleType:
        if self._module is not None:
            return self._module
        spec = importlib.util.spec_from_file_location("nvsop_nvidia_ddm_reader", self._reader_path)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"无法加载 NVIDIA DDM 读取器：{self._reader_path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self._module = module
        return module


class NvidiaDdmAnnotationGenerator:
    """把临时隔离工作区交给基座生成 DDM annotation。"""

    def __init__(self, utility_path: Path | None = None) -> None:
        self._utility_path = utility_path or _find_base_utility()
        self._module: ModuleType | None = None

    def generate(self, workspace: Path, output_filename: str) -> bytes:
        """调用基座聚合函数并读取它写出的结果。"""
        module = self._load_module()
        generate = getattr(module, "generate_ddm_annotation", None)
        if not callable(generate):
            raise RuntimeError("NVIDIA DDM 聚合函数不可用")
        output_path = Path(generate(str(workspace), output_filename))
        return output_path.read_bytes()

    def _load_module(self) -> ModuleType:
        if self._module is not None:
            return self._module
        spec = importlib.util.spec_from_file_location("nvsop_nvidia_ddm_utils", self._utility_path)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"无法加载 NVIDIA DDM 聚合模块：{self._utility_path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self._module = module
        return module


def _find_base_reader() -> Path:
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "vendor" / _BASE_READER
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(f"未找到 NVIDIA DDM 读取器：vendor/{_BASE_READER}")


def _find_base_utility() -> Path:
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "vendor" / _BASE_UTILITY
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(f"未找到 NVIDIA DDM 聚合模块：vendor/{_BASE_UTILITY}")
