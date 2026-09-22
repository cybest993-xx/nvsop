"""复用 NVIDIA VLM 训练读取器的适配器。"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType, SimpleNamespace

from factory_sop.dataset.usage import VlmReaderInputError, VlmReaderUnavailableError

_BASE_READER = (
    "sop-monitoring-blueprints/microservices/sop-training-bp/assets/tools/cosmos_custom_dataset.py"
)


class NvidiaVlmReader:
    """调用 NVIDIA `CosmosSFTDataset` 消费冻结候选记录。"""

    def __init__(self, reader_path: Path | None = None) -> None:
        self._reader_path = reader_path
        self._module: ModuleType | None = None

    def available(self) -> bool:
        """确认当前 worker 环境具备基座读取器依赖。"""
        return all(
            importlib.util.find_spec(name) is not None
            for name in ("torch", "cosmos_rl", "cosmos_reason2_utils", "pydantic", "toml")
        )

    def validate(self, *, workspace: Path, annotation_filename: str) -> None:
        """构造原读取器并读取全部候选记录。"""
        try:
            module = self._load_module()
        except (FileNotFoundError, ImportError, OSError) as error:
            raise VlmReaderUnavailableError from error
        reader = getattr(module, "CosmosSFTDataset", None)
        if not callable(reader):
            raise VlmReaderUnavailableError
        config = SimpleNamespace(
            train=SimpleNamespace(
                train_policy=SimpleNamespace(
                    dataset=SimpleNamespace(
                        name=[str(workspace / annotation_filename)],
                        split=["train"],
                    )
                )
            )
        )
        custom_config = SimpleNamespace(
            vision=SimpleNamespace(model_dump=lambda exclude_none=True: {}),
            system_prompt="",
        )
        try:
            dataset = reader(config=config, custom_config=custom_config)
            for index in range(len(dataset)):
                dataset[index]
        except (AssertionError, IndexError, KeyError, TypeError, ValueError) as error:
            raise VlmReaderInputError from error
        except ImportError as error:
            raise VlmReaderUnavailableError from error

    def _load_module(self) -> ModuleType:
        if self._module is not None:
            return self._module
        reader_path = self._reader_path or _find_base_reader()
        spec = importlib.util.spec_from_file_location("nvsop_nvidia_vlm_reader", reader_path)
        if spec is None or spec.loader is None:
            raise VlmReaderUnavailableError
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self._module = module
        return module


def _find_base_reader() -> Path:
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "vendor" / _BASE_READER
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(f"未找到 NVIDIA VLM 读取器：vendor/{_BASE_READER}")
