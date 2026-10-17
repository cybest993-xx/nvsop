"""VLM 训练服务对共享命令状态机的 adapter：真实 HTTP 契约与实际消费/结果事实。

共享状态机（一次性派发意图、在途保护、基座身份唯一绑定、观测与结果登记）在
`adapters/training_execution.py`；本模块只承担 VLM 的差异：受检候选的材料化与消费核对、
cosmos-reason 微服务的 start/all_jobs/status/cancel 契约、消费目录身份与 ``vlm-results``
结果命名，以及步目录 checkpoint 的清单登记。

``dataset_root`` 与标注数据卷由受信组合层注入：它必须是 VLM 服务 ``DATASET_ROOT`` 指向的
共享挂载；HTTP 契约本身无法证明该同源事实，执行器不对此做断言。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from hashlib import sha256
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from uuid import UUID

from factory_sop.dataset.adapters.training_execution import (
    AllocatedWorkcopies,
    PreparedTrainingDispatch,
    RegisteredTrainingResult,
    TrainingBackendUnavailableError,
    TrainingPreparationRefusedError,
    TrainingResultInvalidError,
    TrainingResultUnavailableError,
    copy_result_file,
    expose_workcopies,
    http_backend_connection,
    http_request,
    http_response,
    json_document,
    valid_base_job_id,
    write_result_object,
)
from factory_sop.dataset.adapters.vlm import (
    NvidiaVlmJsonDiscovery,
    VlmResultSource,
    VlmResultVolume,
    vlm_file_sha256,
)
from factory_sop.dataset.annotation import AnnotationDataVolume
from factory_sop.dataset.errors import DatasetRefusedError
from factory_sop.dataset.model import (
    TRAINING_RESULT_NAMING,
    TrainingCommand,
    TrainingService,
)
from factory_sop.dataset.storage import ObjectStorage, ObjectStorageUnavailableError
from factory_sop.dataset.usage import (
    VlmReaderInputError,
    VlmReaderUnavailableError,
    canonical_json,
)
from factory_sop.dataset.usecases.usage import (
    VLM_CONTRACT_VERSION,
    FrozenVlmInput,
    VlmInputConsumption,
    materialize_vlm_input,
    verify_vlm_input_consumption,
)
from factory_sop.dataset.usecases.vlm_command import decode_frozen_vlm_input

_SERVICE_LABEL = "VLM"


class HttpVlmTrainingBackend:
    """通过真实 HTTP 调用 cosmos-reason 训练微服务的 start/all_jobs/status/cancel 契约。

    start 的 4xx 与 5xx 一律按不可达处理：请求可能已在基座创建任务，只有调用方的一次性
    意图与后续原查询接口能证明归属，HTTP 状态码本身不构成「未执行」的证据。取消入口不同：
    它返回的任何 HTTP 响应都是真实响应事实，但仍不构成进程停止证明。
    """

    def __init__(self, *, base_url: str, timeout_seconds: int) -> None:
        scheme, host, port, base_path, timeout = http_backend_connection(
            base_url=base_url, timeout_seconds=timeout_seconds, service_label=_SERVICE_LABEL
        )
        self._scheme = scheme
        self._host = host
        self._port = port
        self._base_path = base_path
        self._timeout = timeout

    def start(self, *, dataset_id: str) -> str:
        query = urlencode({"dataset_id": dataset_id})
        body = self._request(
            method="POST", path=f"{self._base_path}/api/v1/fine-tuning/start?{query}"
        )
        document = json_document(body, service_label=_SERVICE_LABEL)
        job_id = document.get("job_id")
        if not isinstance(job_id, str) or not job_id:
            raise TrainingBackendUnavailableError("VLM 训练基座没有返回任务身份")
        return job_id

    def all_jobs(self) -> Mapping[str, Mapping[str, Any]]:
        body = self._request(method="GET", path=f"{self._base_path}/api/v1/fine-tuning/all_jobs")
        document = json_document(body, service_label=_SERVICE_LABEL)
        jobs: dict[str, Mapping[str, Any]] = {}
        for job_id, job in document.items():
            if not isinstance(job, dict):
                raise TrainingBackendUnavailableError("VLM 训练基座任务集合格式不合法")
            # 只有键与任务体自带身份一致的真实 UUID 才能用于唯一归属证明。
            if not valid_base_job_id(job_id) or str(job.get("id")) != str(job_id):
                raise TrainingBackendUnavailableError("VLM 训练基座任务身份不一致")
            jobs[str(job_id)] = job
        return jobs

    def status(self, job_id: str) -> Mapping[str, Any]:
        return json_document(
            self._request(
                method="GET", path=f"{self._base_path}/api/v1/fine-tuning/status/{job_id}"
            ),
            service_label=_SERVICE_LABEL,
        )

    def cancel(self, *, job_id: str) -> Mapping[str, Any]:
        """调用原服务真实取消入口，把 HTTP 状态与消息摘要作为原始事实返回。

        该服务只有消息文案，成功文案与 HTTP 200 都不证明进程已停止，因此这里不返回任何
        「已停止」结论，具体结论由执行器按原响应持久化。
        """
        status, body = http_response(
            scheme=self._scheme,
            host=self._host,
            port=self._port,
            timeout_seconds=self._timeout,
            service_label=_SERVICE_LABEL,
            method="POST",
            path=f"{self._base_path}/api/v1/fine-tuning/cancel/{job_id}",
        )
        fact: dict[str, Any] = {"outcome": "retryable", "http_status": status}
        try:
            document = json_document(body, service_label=_SERVICE_LABEL)
        except TrainingBackendUnavailableError:
            return fact
        message = document.get("message")
        if isinstance(message, str):
            fact["message_sha256"] = sha256(message.encode("utf-8")).hexdigest()
            fact["message_length"] = len(message)
            # cr-training-ms/app.py: not assigned / failed 文案都要求后续重试；
            # 只有原接口明确受理取消才停止重复 POST，且仍不证明进程已停。
            if (
                status == 200
                and message == f"Training job {job_id} has been cancelled successfully"
            ):
                fact["outcome"] = "http"
        return fact

    def _request(self, *, method: str, path: str) -> bytes:
        return http_request(
            scheme=self._scheme,
            host=self._host,
            port=self._port,
            timeout_seconds=self._timeout,
            service_label=_SERVICE_LABEL,
            method=method,
            path=path,
        )


class VlmTrainingServiceAdapter:
    """VLM 的输入准备、基座身份、取消入口与结果清单；其余状态机行为完全共享。"""

    def __init__(
        self,
        *,
        backend: HttpVlmTrainingBackend,
        backend_target: str,
        dataset_root: Path,
        annotation_volume: Callable[[], AnnotationDataVolume | None],
        result_volume: VlmResultVolume | None = None,
        discovery: NvidiaVlmJsonDiscovery | None = None,
    ) -> None:
        self._backend = backend
        self._backend_target = backend_target
        self._dataset_root = dataset_root
        self._annotation_volume = annotation_volume
        self._result_volume = result_volume
        self._discovery = discovery or NvidiaVlmJsonDiscovery()

    @property
    def service(self) -> TrainingService:
        return TrainingService.VLM

    @property
    def backend_target(self) -> str:
        return self._backend_target

    @property
    def result_registration_available(self) -> bool:
        """结果读取未配置时基座完成也不登记，只保留可诊断的在途保护。"""
        return self._result_volume is not None

    def input_identity(self, command_id: UUID) -> str:
        return f"vlm-{command_id}"

    def identity_matches(self, job: Mapping[str, Any], *, identity: str) -> bool:
        return str(job.get("aug_dataset_id")) == identity

    def all_jobs(self) -> Mapping[str, Mapping[str, Any]]:
        return self._backend.all_jobs()

    def status(self, job_id: str) -> Mapping[str, Any]:
        return self._backend.status(job_id)

    def cancel(self, *, job_id: str) -> Mapping[str, Any]:
        return self._backend.cancel(job_id=job_id)

    def prepare(
        self,
        *,
        command: TrainingCommand,
        storage: ObjectStorage,
        owned: AllocatedWorkcopies,
    ) -> PreparedTrainingDispatch:
        try:
            frozen = decode_frozen_vlm_input(command.payload)
        except (KeyError, TypeError, ValueError) as error:
            raise TrainingPreparationRefusedError(
                "DISPATCH_PAYLOAD_INVALID", "VLM 训练命令冻结负载不可还原"
            ) from error
        identity = self.input_identity(command.id)
        directory = self._dataset_root / identity
        try:
            materialized = materialize_vlm_input(
                frozen=frozen,
                storage=storage,
                annotation_volume=self._annotation_volume(),
                root=self._dataset_root,
            )
        except DatasetRefusedError:
            raise
        except (ObjectStorageUnavailableError, OSError) as error:
            raise TrainingPreparationRefusedError(
                "STORAGE_UNAVAILABLE", "VLM 输入工作副本准备失败"
            ) from error
        owned.root = materialized.root
        try:
            # 基座按 dataset_id 只看到平铺目录：清单留在 input_id 根目录，不进入发现范围。
            expose_workcopies(sources=((materialized.reader_directory, directory),), owned=owned)
        except DatasetRefusedError:
            raise
        except OSError as error:
            raise TrainingPreparationRefusedError(
                "STORAGE_UNAVAILABLE", "VLM 输入工作副本暴露失败"
            ) from error
        consumption = self._verify_consumption(
            frozen=frozen, directory=directory, identity=identity
        )
        return PreparedTrainingDispatch(
            identity=identity,
            dispatch_facts=_dispatch_facts(
                frozen=frozen, identity=identity, consumption=consumption
            ),
            start=lambda: self._backend.start(dataset_id=identity),
        )

    def result_object_keys(self, *, command_id: UUID, candidate: str) -> tuple[str, str]:
        naming = TRAINING_RESULT_NAMING[TrainingService.VLM]
        return (
            f"{naming.prefix}/{command_id}/{candidate}/{naming.config_filename}",
            f"{naming.prefix}/{command_id}/{candidate}/{naming.checkpoint_filename}",
        )

    def copy_result(
        self,
        *,
        command: TrainingCommand,
        base_job_id: str,
        storage: ObjectStorage,
        config_object_key: str,
        checkpoint_object_key: str,
    ) -> RegisteredTrainingResult:
        if self._result_volume is None:
            raise TrainingResultUnavailableError("VLM 训练结果读取尚未配置")
        facts = command.dispatch_facts or {}
        identity = self.input_identity(command.id)
        if str(facts.get("dataset_id", "")) != identity:
            raise TrainingResultInvalidError("消费目录与本命令身份不一致")
        frozen = decode_frozen_vlm_input(command.payload)
        source = self._result_volume.locate(
            base_job_id=base_job_id, identity=identity, frozen=frozen
        )
        config_size, config_sha256 = copy_result_file(
            source=source.config_path, storage=storage, object_key=config_object_key
        )
        manifest = _checkpoint_manifest(
            source=source,
            frozen=frozen,
            identity=identity,
            base_job_id=base_job_id,
        )
        checkpoint_size, checkpoint_sha256 = write_result_object(
            content=canonical_json(manifest),
            storage=storage,
            object_key=checkpoint_object_key,
        )
        return RegisteredTrainingResult(
            config_object_key=config_object_key,
            config_sha256=config_sha256,
            config_size=config_size,
            checkpoint_object_key=checkpoint_object_key,
            checkpoint_sha256=checkpoint_sha256,
            checkpoint_size=checkpoint_size,
        )

    def _verify_consumption(
        self, *, frozen: FrozenVlmInput, directory: Path, identity: str
    ) -> VlmInputConsumption:
        try:
            return verify_vlm_input_consumption(
                frozen=frozen, directory=directory, discover=self._discovery.discover
            )
        except DatasetRefusedError as error:
            # 核对阶段的任何拒绝都是确定失败：只有材料化/暴露时的目录占用才可能是并发准备。
            raise TrainingPreparationRefusedError(error.code.value, error.detail) from error
        except (VlmReaderUnavailableError, VlmReaderInputError) as error:
            raise TrainingPreparationRefusedError(
                "VLM_DISCOVERY_UNAVAILABLE", "VLM 基座发现函数不可用，未发送基座启动请求"
            ) from error
        except OSError as error:
            raise TrainingPreparationRefusedError(
                "STORAGE_UNAVAILABLE", "VLM 输入消费核对失败"
            ) from error


def _dispatch_facts(
    *,
    frozen: FrozenVlmInput,
    identity: str,
    consumption: VlmInputConsumption,
) -> dict[str, Any]:
    """冻结目标、受检候选身份与实际消费事实，作为意图的一部分原子落库。"""
    return {
        "input_id": str(frozen.input_id),
        "dataset_id": identity,
        "usage_check_id": str(frozen.usage_check_id),
        "candidate_id": str(frozen.candidate_id),
        "input_digest": frozen.input_digest,
        "base_commit": frozen.base_commit,
        "contract_version": frozen.contract_version,
        "records_sha256": frozen.records_sha256,
        "consumption": {
            "input_id": str(consumption.input_id),
            "records_filename": consumption.records_filename,
            "records_sha256": consumption.records_sha256,
            "consumed_files": [
                {
                    "role": item.role,
                    "filename": item.filename,
                    "source_object_key": item.source_object_key,
                    "source_sha256": item.source_sha256,
                    "size": item.size,
                }
                for item in consumption.consumed_files
            ],
        },
    }


def _checkpoint_manifest(
    *,
    source: VlmResultSource,
    frozen: FrozenVlmInput,
    identity: str,
    base_job_id: str,
) -> dict[str, Any]:
    """把真实步目录与输入/基座/模型来源登记成一份不可变清单对象。

    清单不复制权重字节，但逐个记录真实文件的相对路径、大小与 sha256，使“非空 checkpoint
    真实存在”可验证，并与本次输入、基座提交与模型来源绑定。
    """
    files: list[dict[str, Any]] = []
    for path in source.checkpoint_files:
        try:
            size = path.stat().st_size
            digest = vlm_file_sha256(path)
        except OSError as error:
            raise TrainingResultUnavailableError("基座 checkpoint 文件不可读") from error
        files.append(
            {
                "path": str(path.relative_to(source.checkpoint_directory)),
                "size": size,
                "sha256": digest,
            }
        )
    if not files:
        raise TrainingResultUnavailableError("基座 checkpoint 文件尚未写出")
    return {
        "job_id": base_job_id,
        "step": source.step,
        "checkpoint_directory": source.checkpoint_relative_directory,
        "model_source": source.model_source,
        "base_commit": frozen.base_commit,
        "contract_version": VLM_CONTRACT_VERSION,
        "input_identity": identity,
        "usage_check_id": str(frozen.usage_check_id),
        "candidate_id": str(frozen.candidate_id),
        "input_digest": frozen.input_digest,
        "records_sha256": frozen.records_sha256,
        "files": files,
    }
