"""复用 NVIDIA DDM 标注聚合函数与本地结果卷的适配器。"""

from __future__ import annotations

import importlib.util
import json
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from types import ModuleType
from typing import Any, Protocol, cast
from uuid import UUID

import yaml  # type: ignore[import-untyped]  # PyYAML 无随包类型存根，解析失败由本适配器显式分类

from factory_sop.dataset.usage import (
    DdmReaderInputError,
    DdmReaderUnavailableError,
    DdmSampleClassEmptyError,
)
from factory_sop.dataset.usecases.usage import FrozenDdmInput

# 基座在 RESULTS_ROOT 下按 job_id 落地的真实文件：
# app.py 先写 RESULTS_ROOT/<job_id>/<job_id>.yaml，训练脚本再写
# RESULTS_ROOT/<job_id>/train/<job_id>/config.yaml，并在同目录save_last/last.ckpt。
_JOB_CONFIG_SUFFIXES = (".yaml", ".toml")
_MERGED_CONFIG_RELATIVE = Path("train")
_MERGED_CONFIG_NAME = "config.yaml"
_CHECKPOINT_NAME = "last.ckpt"
_TRUSTED_CONFIG_FILENAMES = frozenset({"config.yaml", "ddm_train_config.yaml"})


class DdmResultUnavailableError(Exception):
    """结果文件尚不存在/未就绪：按可恢复诊断处理，不做为成功。"""


class DdmResultInvalidError(Exception):
    """结果文件存在但不可信：不是本次输入的真实配置或真实非空 checkpoint。"""


@dataclass(frozen=True, slots=True)
class DdmResultSource:
    """已核实来源身份的基座结果文件；只暴露受信卷内的文件路径与文件名。"""

    config_path: Path
    config_filename: str
    checkpoint_path: Path
    checkpoint_filename: str


class DdmResultVolume(Protocol):
    """在受信挂载上定位并核实一次 DDM 训练结果的最小契约。"""

    def locate(
        self,
        *,
        base_job_id: str,
        training_dataset_id: str,
        validation_dataset_id: str,
        training_consumer_parameters: Mapping[str, object],
        validation_consumer_parameters: Mapping[str, object],
        frozen: FrozenDdmInput,
        dispatch_facts: Mapping[str, Any],
    ) -> DdmResultSource:
        """核实本次 job 的配置与 checkpoint；不可读/不可信时必须抛出。"""
        ...


class LocalDdmResultVolume:
    """在共享挂载的基座 DATASET_ROOT/RESULTS_ROOT 上核实结果，不加载权重、不执行。

    只做文件存在性、常规文件、非空、可读与配置字段核对；不做 torch/pickle 反序列化，也不
    执行任何模型代码。任何不一致都作为结果不可读的可恢复诊断抛出。
    """

    def __init__(self, *, results_root: Path, dataset_root: Path) -> None:
        self._results_root = results_root
        self._dataset_root = dataset_root

    def locate(
        self,
        *,
        base_job_id: str,
        training_dataset_id: str,
        validation_dataset_id: str,
        training_consumer_parameters: Mapping[str, object],
        validation_consumer_parameters: Mapping[str, object],
        frozen: FrozenDdmInput,
        dispatch_facts: Mapping[str, Any],
    ) -> DdmResultSource:
        try:
            if str(UUID(base_job_id)) != base_job_id:
                raise ValueError
        except ValueError as error:
            raise DdmResultInvalidError("基座结果任务身份非法") from error
        for name in (training_dataset_id, validation_dataset_id):
            if not name or Path(name).name != name or name in {".", ".."}:
                raise DdmResultInvalidError("基座输入目录身份非法")
        job_directory = self._results_root / base_job_id
        _within_root(self._results_root, job_directory)
        if not _is_directory(job_directory):
            raise DdmResultUnavailableError("基座结果目录尚未出现")
        job_config = self._locate_job_config(job_directory, base_job_id)
        merged_config = job_directory / _MERGED_CONFIG_RELATIVE / base_job_id / _MERGED_CONFIG_NAME
        if not _is_regular_file(merged_config):
            raise DdmResultUnavailableError("基座合并配置尚未写出")
        _within_root(self._results_root, job_config)
        _within_root(self._results_root, merged_config)
        document = _load_config_document(job_config)
        _verify_job_config(
            document, job_config=job_config, job_directory=job_directory, base_job_id=base_job_id
        )
        merged = _load_config_document(merged_config)
        _verify_job_config(
            merged, job_config=merged_config, job_directory=job_directory, base_job_id=base_job_id
        )
        for config in (document, merged):
            _verify_merged_config(
                config,
                dataset_root=self._dataset_root,
                training_dataset_id=training_dataset_id,
                validation_dataset_id=validation_dataset_id,
                training_consumer_parameters=training_consumer_parameters,
                validation_consumer_parameters=validation_consumer_parameters,
            )
        _verify_consumed_bytes(
            dataset_root=self._dataset_root,
            training_dataset_id=training_dataset_id,
            validation_dataset_id=validation_dataset_id,
            frozen=frozen,
            facts=dispatch_facts,
        )
        checkpoint = job_directory / _MERGED_CONFIG_RELATIVE / base_job_id / _CHECKPOINT_NAME
        _within_root(self._results_root, checkpoint)
        if not _is_regular_file(checkpoint):
            raise DdmResultUnavailableError("基座 checkpoint 尚未写出")
        try:
            with checkpoint.open("rb") as reader:
                if not reader.read(1):
                    raise DdmResultInvalidError("基座 checkpoint 为空文件")
            if checkpoint.stat().st_size <= 0:
                raise DdmResultInvalidError("基座 checkpoint 为空文件")
        except OSError as error:
            raise DdmResultUnavailableError("基座 checkpoint 不可读") from error
        return DdmResultSource(
            config_path=merged_config,
            config_filename=_MERGED_CONFIG_NAME,
            checkpoint_path=checkpoint,
            checkpoint_filename=_CHECKPOINT_NAME,
        )

    def _locate_job_config(self, job_directory: Path, base_job_id: str) -> Path:
        for suffix in _JOB_CONFIG_SUFFIXES:
            candidate = job_directory / f"{base_job_id}{suffix}"
            _within_root(self._results_root, candidate)
            if _is_regular_file(candidate):
                return candidate
        raise DdmResultUnavailableError("基座 job 配置尚未写出")


def _is_directory(path: Path) -> bool:
    try:
        return path.is_dir()
    except OSError:
        return False


def _is_regular_file(path: Path) -> bool:
    try:
        return stat.S_ISREG(path.lstat().st_mode) and not path.is_symlink()
    except OSError:
        return False


def _load_config_document(path: Path) -> dict[str, Any]:
    try:
        if path.suffix == ".toml":
            import tomllib

            with path.open("rb") as source:
                document = tomllib.load(source)
        else:
            document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, yaml.YAMLError) as error:
        raise DdmResultInvalidError("基座结果配置不可解析") from error
    if not isinstance(document, dict):
        raise DdmResultInvalidError("基座结果配置不是映射")
    return document


def _verify_job_config(
    document: Mapping[str, Any],
    *,
    job_config: Path,
    job_directory: Path,
    base_job_id: str,
) -> None:
    if job_config.name not in _TRUSTED_CONFIG_FILENAMES and job_config.stem != base_job_id:
        raise DdmResultInvalidError("基座 job 配置文件名与任务身份不一致")
    training_config = document.get("training_config")
    if not isinstance(training_config, Mapping):
        raise DdmResultInvalidError("基座 job 配置缺少 training_config")
    if _normalized_path(training_config.get("output")) != _normalized_path(job_directory):
        raise DdmResultInvalidError("基座 job 配置的输出去向不是本任务结果目录")
    if str(training_config.get("exp_name")) != base_job_id:
        raise DdmResultInvalidError("基座 job 配置的实验名与任务身份不一致")


def _verify_merged_config(
    document: Mapping[str, Any],
    *,
    dataset_root: Path,
    training_dataset_id: str,
    validation_dataset_id: str,
    training_consumer_parameters: Mapping[str, object],
    validation_consumer_parameters: Mapping[str, object],
) -> None:
    dataset_config = document.get("dataset_config")
    if not isinstance(dataset_config, Mapping):
        raise DdmResultInvalidError("基座配置缺少 dataset_config")
    actual_training, actual_validation = ddm_reader_parameters(document)
    if dict(actual_training) != dict(training_consumer_parameters) or dict(
        actual_validation
    ) != dict(validation_consumer_parameters):
        raise DdmResultInvalidError("基座读取器按角色参数与本命令冻结值不一致")
    train_config = dataset_config.get("train_config")
    val_config = dataset_config.get("val_config")
    if not isinstance(train_config, Mapping) or not isinstance(val_config, Mapping):
        raise DdmResultInvalidError("基座合并配置缺少训练/验证配置")
    if _normalized_path(train_config.get("data_root")) != _normalized_path(
        dataset_root / training_dataset_id
    ):
        raise DdmResultInvalidError("基座训练路径与本次冻结训练集不一致")
    if _normalized_path(val_config.get("data_root")) != _normalized_path(
        dataset_root / validation_dataset_id
    ):
        raise DdmResultInvalidError("基座验证路径与本次冻结验证集不一致")
    for role_config, directory, filename in (
        (train_config, dataset_root / training_dataset_id, "ddm_train_annotation.json"),
        (val_config, dataset_root / validation_dataset_id, "ddm_val_annotation.json"),
    ):
        _within_root(dataset_root, directory)
        if _normalized_path(role_config.get("anno_path")) != str((directory / filename).absolute()):
            raise DdmResultInvalidError("基座标注路径不是本次角色的消费文件")


def _normalized_path(value: object) -> str | None:
    if isinstance(value, Path):
        return str(value.absolute())
    if not isinstance(value, str) or not value:
        return None
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        return None
    return str(path)


def ddm_reader_parameters(
    document: Mapping[str, Any],
) -> tuple[Mapping[str, object], Mapping[str, object]]:
    """按实际 DDMDataModule 的字段位置提取 reader 参数，不以当前全局常量代替部署配置。"""
    try:
        dataset = document["dataset_config"]
        model = document["model_config"]
        if dataset["dataset"] != "DDMDataset" or dataset["val_config"]["mode"] != "val":
            raise ValueError
        training = {
            key: dataset[key]
            for key in (
                "num_classes",
                "frames_per_side",
                "downsample",
                "min_change_dur",
                "video_backend",
            )
        }
        training["mode"] = dataset["train_config"]["mode"]
        if model["num_classes"] != training["num_classes"]:
            raise ValueError
        validation = {
            key: dataset[key] for key in ("frames_per_side", "downsample", "min_change_dur")
        }
        validation["temporal_stride"] = dataset["val_config"]["temporal_stride"]
        return training, validation
    except (KeyError, TypeError, ValueError) as error:
        raise DdmResultInvalidError("基座配置缺少或不符合真实训练/验证读取器字段") from error


def load_ddm_reader_parameters(path: Path) -> tuple[Mapping[str, object], Mapping[str, object]]:
    """每次派发前才读取受信部署配置，缺失文件不阻止控制面/worker 启动。"""
    return ddm_reader_parameters(_load_config_document(path))


def _within_root(root: Path, path: Path) -> None:
    root = root.absolute()
    path = path.absolute()
    try:
        relative = path.relative_to(root)
    except ValueError as error:
        raise DdmResultInvalidError("基座文件路径越出受信挂载") from error
    if ".." in relative.parts or root.is_symlink():
        raise DdmResultInvalidError("基座文件路径越界或使用符号链接")
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise DdmResultInvalidError("基座文件路径使用符号链接")


def _verify_consumed_bytes(
    *,
    dataset_root: Path,
    training_dataset_id: str,
    validation_dataset_id: str,
    frozen: FrozenDdmInput,
    facts: Mapping[str, Any],
) -> None:
    consumption = facts.get("consumption")
    if (
        not isinstance(consumption, Mapping)
        or facts.get("input_id") != str(frozen.input_id)
        or consumption.get("input_id") != str(frozen.input_id)
    ):
        raise DdmResultInvalidError("消费记录与冻结输入身份不一致")
    expected_files = []
    for role, inputs, directory_id in (
        ("training", frozen.training, training_dataset_id),
        ("validation", frozen.validation, validation_dataset_id),
    ):
        directory = dataset_root / directory_id
        filename = consumption.get(f"{role}_annotation_filename")
        expected_name = (
            "ddm_train_annotation.json" if role == "training" else "ddm_val_annotation.json"
        )
        if filename != expected_name:
            raise DdmResultInvalidError("消费标注文件身份不一致")
        _verify_digest(
            dataset_root, directory / expected_name, consumption.get(f"{role}_annotation_sha256")
        )
        for member in inputs.members:
            name = f"{member.member_id}.mp4"
            _verify_digest(dataset_root, directory / name, member.source_sha256, member.actual_size)
            _verify_digest(
                dataset_root,
                directory / str(member.member_id) / f"{member.member_id}_annotation.json",
                member.annotation_sha256,
            )
            expected_files.append(
                {
                    "role": role,
                    "filename": name,
                    "source_object_key": member.object_key,
                    "source_sha256": member.source_sha256,
                    "size": member.actual_size,
                }
            )
    if consumption.get("consumed_files") != expected_files:
        raise DdmResultInvalidError("实际消费媒体清单与冻结来源不一致")


def _verify_digest(root: Path, path: Path, expected: object, size: int | None = None) -> None:
    _within_root(root, path)
    if not _is_regular_file(path):
        raise DdmResultInvalidError("实际消费输入不是常规文件")
    try:
        digest = sha256()
        with path.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                digest.update(chunk)
        if digest.hexdigest() != expected or (size is not None and path.stat().st_size != size):
            raise DdmResultInvalidError("实际消费输入摘要/大小与冻结来源不一致")
    except OSError as error:
        raise DdmResultUnavailableError("实际消费输入不可读") from error


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
        self._reader_path = reader_path
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
        except (FileNotFoundError, ImportError, OSError) as error:
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
            FileNotFoundError,
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
        reader_path = self._reader_path or _find_base_reader()
        spec = importlib.util.spec_from_file_location("nvsop_nvidia_ddm_reader", reader_path)
        if spec is None or spec.loader is None:
            raise DdmReaderUnavailableError
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
