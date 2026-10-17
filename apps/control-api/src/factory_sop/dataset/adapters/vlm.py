"""复用 NVIDIA VLM 训练读取器的适配器。"""

from __future__ import annotations

import importlib.util
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Protocol, cast
from uuid import UUID

from factory_sop.dataset.adapters.training_execution import (
    TrainingResultInvalidError,
    TrainingResultUnavailableError,
    ensure_within_root,
    is_directory,
    is_regular_file,
)
from factory_sop.dataset.usage import (
    VLM_CONSUMER_PARAMETERS,
    VlmReaderInputError,
    VlmReaderUnavailableError,
)
from factory_sop.dataset.usecases.usage import (
    VLM_RECORDS_FILENAME,
    FrozenVlmInput,
)

_BASE_READER = (
    "sop-monitoring-blueprints/microservices/sop-training-bp/assets/tools/cosmos_custom_dataset.py"
)
_BASE_JSON_DISCOVERY = (
    "sop-monitoring-blueprints/microservices/sop-training-bp/microservices/cr-training-ms"
    "/utils/dataset_utils.py"
)
_VLM_MERGED_CONFIG_SUFFIX = ".toml"
_STEP_PREFIX = "step_"


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
            vision=SimpleNamespace(
                model_dump=lambda exclude_none=True: dict(
                    cast(Mapping[str, object], VLM_CONSUMER_PARAMETERS["vision"])
                )
            ),
            system_prompt=cast(str, VLM_CONSUMER_PARAMETERS["system_prompt"]),
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
    return _find_vendor_file(_BASE_READER, "未找到 NVIDIA VLM 读取器")


def _find_vendor_file(relative: str, message: str) -> Path:
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "vendor" / relative
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(f"{message}：vendor/{relative}")


class NvidiaVlmJsonDiscovery:
    """调用基座自己的发现函数，枚举一个输入目录实际会被消费的 JSON 文件。

    基座对 ``DATASET_ROOT/<dataset_id>`` 递归消费全部 ``*.json``；核对必须用同一个函数，
    否则“多一个 JSON 静默进入训练”无法被发现。
    """

    def __init__(self, module_path: Path | None = None) -> None:
        self._module_path = module_path
        self._module: ModuleType | None = None

    def discover(self, directory: Path) -> Sequence[str]:
        """返回基座会在该目录下消费的全部 JSON 路径。"""
        module = self._load_module()
        discover = getattr(module, "get_all_json_paths", None)
        if not callable(discover):
            raise VlmReaderUnavailableError
        try:
            found = discover(str(directory))
        except OSError as error:
            raise VlmReaderUnavailableError from error
        if not isinstance(found, list) or any(not isinstance(item, str) for item in found):
            raise VlmReaderUnavailableError
        return [str(item) for item in found]

    def _load_module(self) -> ModuleType:
        if self._module is not None:
            return self._module
        path = self._module_path or _find_vendor_file(
            _BASE_JSON_DISCOVERY, "未找到 VLM 基座发现函数"
        )
        spec = importlib.util.spec_from_file_location("nvsop_nvidia_vlm_json_discovery", path)
        if spec is None or spec.loader is None:
            raise VlmReaderUnavailableError
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self._module = module
        return module


@dataclass(frozen=True, slots=True)
class VlmResultSource:
    """已核实来源身份的 VLM 结果：合并配置、步目录、基座模型来源与实际文件。"""

    config_path: Path
    checkpoint_directory: Path
    checkpoint_relative_directory: str
    checkpoint_files: tuple[Path, ...]
    step: int
    model_source: str


class VlmResultVolume(Protocol):
    """在受信挂载上定位并核实一次 VLM 训练结果的最小契约。"""

    def locate(self, *, base_job_id: str, identity: str, frozen: FrozenVlmInput) -> VlmResultSource:
        """核实本次 job 的合并配置与 checkpoint 步目录；不可读/不可信时必须抛出。"""
        ...


class LocalVlmResultVolume:
    """在共享结果卷上核实 VLM 结果，不加载权重、不执行。

    只做文件存在性、常规文件、非空、可读与合并配置字段核对；不做任何反序列化，也不执行
    模型代码。任何不一致都作为结果不可读的可恢复诊断抛出。
    """

    def __init__(self, *, results_root: Path, dataset_root: Path) -> None:
        self._results_root = results_root
        self._dataset_root = dataset_root

    def locate(self, *, base_job_id: str, identity: str, frozen: FrozenVlmInput) -> VlmResultSource:
        try:
            if str(UUID(base_job_id)) != base_job_id:
                raise ValueError
        except ValueError as error:
            raise TrainingResultInvalidError("基座结果任务身份非法") from error
        if not identity or Path(identity).name != identity or identity in {".", ".."}:
            raise TrainingResultInvalidError("基座输入目录身份非法")
        job_directory = self._results_root / base_job_id
        ensure_within_root(self._results_root, job_directory)
        if not is_directory(job_directory):
            raise TrainingResultUnavailableError("基座结果目录尚未出现")
        config_path = job_directory / f"{base_job_id}{_VLM_MERGED_CONFIG_SUFFIX}"
        ensure_within_root(self._results_root, config_path)
        if not is_regular_file(config_path):
            raise TrainingResultUnavailableError("基座合并配置尚未写出")
        model_source = _verify_merged_config(
            config_path=config_path,
            base_job_id=base_job_id,
            job_directory=job_directory,
            expected_records_path=self._dataset_root / identity / VLM_RECORDS_FILENAME,
        )
        if not frozen.records_sha256:
            raise TrainingResultInvalidError("冻结输入缺少候选记录摘要")
        directory, step = _latest_step_directory(self._results_root, job_directory)
        files = _checkpoint_files(self._results_root, directory)
        return VlmResultSource(
            config_path=config_path,
            checkpoint_directory=directory,
            checkpoint_relative_directory=str(directory.relative_to(self._results_root)),
            checkpoint_files=files,
            step=step,
            model_source=model_source,
        )


def _verify_merged_config(
    *,
    config_path: Path,
    base_job_id: str,
    job_directory: Path,
    expected_records_path: Path,
) -> str:
    """核对基座真实写出的合并配置：输出目录、试验身份、实际消费输入与基座模型来源。"""
    try:
        with config_path.open("rb") as source:
            document = tomllib.load(source)
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise TrainingResultUnavailableError("基座合并配置不可读") from error
    train = document.get("train")
    logging_section = document.get("logging")
    policy = document.get("policy")
    if (
        not isinstance(train, Mapping)
        or not isinstance(logging_section, Mapping)
        or not isinstance(policy, Mapping)
    ):
        raise TrainingResultInvalidError("基座合并配置结构不完整")
    if str(train.get("output_dir")) != str(job_directory):
        raise TrainingResultInvalidError("基座合并配置的输出目录与本任务不一致")
    if str(logging_section.get("experiment_name")) != base_job_id:
        raise TrainingResultInvalidError("基座合并配置的试验身份与本任务不一致")
    train_policy = train.get("train_policy")
    dataset_section = train_policy.get("dataset") if isinstance(train_policy, Mapping) else None
    if not isinstance(dataset_section, Mapping):
        raise TrainingResultInvalidError("基座合并配置缺少数据集小节")
    if dataset_section.get("name") != [str(expected_records_path)] or dataset_section.get(
        "split"
    ) != [Path(VLM_RECORDS_FILENAME).stem]:
        raise TrainingResultInvalidError("基座实际消费的输入与本命令冻结输入不一致")
    model_source = policy.get("model_name_or_path")
    if not isinstance(model_source, str) or not model_source:
        raise TrainingResultInvalidError("基座合并配置缺少本次训练的基座模型来源")
    return model_source


def _latest_step_directory(results_root: Path, job_directory: Path) -> tuple[Path, int]:
    found: dict[int, Path] = {}
    for candidate in sorted(job_directory.rglob(f"{_STEP_PREFIX}*")):
        if not candidate.is_dir():
            continue
        suffix = candidate.name[len(_STEP_PREFIX) :]
        if not suffix.isdigit():
            continue
        found[int(suffix)] = candidate
    if not found:
        raise TrainingResultUnavailableError("基座 checkpoint 尚未写出")
    step = max(found)
    directory = found[step]
    ensure_within_root(results_root, directory)
    return directory, step


def _checkpoint_files(results_root: Path, directory: Path) -> tuple[Path, ...]:
    """列出步目录下真实非空文件；目录只有空文件或尚未写入时按未就绪处理。"""
    files: list[Path] = []
    for candidate in sorted(directory.rglob("*")):
        if not candidate.is_file():
            continue
        ensure_within_root(results_root, candidate)
        if candidate.stat().st_size <= 0:
            continue
        files.append(candidate)
    if not files:
        raise TrainingResultUnavailableError("基座 checkpoint 文件尚未写出")
    return tuple(files)


def vlm_file_sha256(path: Path) -> str:
    """计算结果文件摘要；供登记清单在写入前核对真实文件。"""
    digest = sha256()
    with path.open("rb") as reader:
        while True:
            chunk = reader.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()
