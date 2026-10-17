"""DDM 训练服务对共享命令状态机的 adapter：真实 HTTP 契约与实际消费/结果事实。

共享状态机（一次性派发意图、在途保护、基座身份唯一绑定、观测与结果登记）在
`adapters/training_execution.py`；本模块只承担 DDM 的差异：双集输入的材料化与消费核对、
DDM 微服务的 start/all_jobs/status 契约、消费目录身份与 ``ddm-results`` 结果命名。

``dataset_root`` 与按角色 reader 参数由受信组合层注入：它必须是 DDM 服务 ``DATASET_ROOT``
指向的共享挂载，且构造时传入的 reader 参数就是服务当前实际使用的参数；HTTP 契约本身无法
证明该同源事实，执行器不对此做断言。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from uuid import UUID

from factory_sop.dataset.adapters.ddm import (
    DdmResultVolume,
    LocalDdmResultVolume,
)
from factory_sop.dataset.adapters.training_execution import (
    AllocatedWorkcopies,
    PreparedTrainingDispatch,
    RegisteredTrainingResult,
    TrainingBackendUnavailableError,
    TrainingPreparationRefusedError,
    TrainingResultInvalidError,
    TrainingResultUnavailableError,
    TrainingServiceAdapter,
    copy_result_file,
    expose_workcopies,
    http_backend_connection,
    http_request,
    json_document,
    valid_base_job_id,
)
from factory_sop.dataset.errors import DatasetRefusalCode, DatasetRefusedError
from factory_sop.dataset.model import (
    TRAINING_RESULT_NAMING,
    TrainingCommand,
    TrainingService,
)
from factory_sop.dataset.storage import ObjectStorage, ObjectStorageUnavailableError
from factory_sop.dataset.usecases.ddm_command import decode_frozen_ddm_input
from factory_sop.dataset.usecases.usage import (
    DdmInputConsumption,
    FrozenDdmInput,
    MaterializedDdmInput,
    materialize_ddm_input,
    verify_ddm_input_consumption,
)

_SERVICE_LABEL = "DDM"


class HttpDdmTrainingBackend:
    """通过真实 HTTP 调用 DDM 训练微服务的 start/all_jobs/status 契约。

    start 的 4xx 与 5xx 一律按不可达处理：请求可能已在基座创建任务，只有调用方的一次性
    意图与后续原查询接口能证明归属，HTTP 状态码本身不构成「未执行」的证据。
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

    def start(self, *, training_dataset_id: str, validation_dataset_id: str) -> str:
        query = urlencode(
            {"dataset_id": training_dataset_id, "validation_dataset_id": validation_dataset_id}
        )
        body = self._request(
            method="POST", path=f"{self._base_path}/api/v1/fine-tuning/start?{query}"
        )
        document = json_document(body, service_label=_SERVICE_LABEL)
        job_id = document.get("job_id")
        if not isinstance(job_id, str) or not job_id:
            raise TrainingBackendUnavailableError("DDM 训练基座没有返回任务身份")
        return job_id

    def all_jobs(self) -> Mapping[str, Mapping[str, Any]]:
        body = self._request(method="GET", path=f"{self._base_path}/api/v1/fine-tuning/all_jobs")
        document = json_document(body, service_label=_SERVICE_LABEL)
        jobs: dict[str, Mapping[str, Any]] = {}
        for job_id, job in document.items():
            if not isinstance(job, dict):
                raise TrainingBackendUnavailableError("DDM 训练基座任务集合格式不合法")
            # 只有键与任务体自带身份一致的真实 UUID 才能用于唯一归属证明。
            if not valid_base_job_id(job_id) or str(job.get("id")) != str(job_id):
                raise TrainingBackendUnavailableError("DDM 训练基座任务身份不一致")
            jobs[str(job_id)] = job
        return jobs

    def status(self, job_id: str) -> Mapping[str, Any]:
        return json_document(
            self._request(
                method="GET", path=f"{self._base_path}/api/v1/fine-tuning/status/{job_id}"
            ),
            service_label=_SERVICE_LABEL,
        )

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


class DdmTrainingServiceAdapter:
    """DDM 的输入准备、基座身份与结果核实；其余状态机行为完全共享。"""

    def __init__(
        self,
        *,
        backend: HttpDdmTrainingBackend,
        backend_target: str,
        dataset_root: Path,
        training_consumer_parameters: Mapping[str, object],
        validation_consumer_parameters: Mapping[str, object],
        generate: Callable[[Path, str], bytes],
        result_volume: DdmResultVolume | None = None,
        reader_parameters: Callable[[], tuple[Mapping[str, object], Mapping[str, object]]]
        | None = None,
    ) -> None:
        self._backend = backend
        self._backend_target = backend_target
        self._dataset_root = dataset_root
        self._training_consumer_parameters = dict(training_consumer_parameters)
        self._validation_consumer_parameters = dict(validation_consumer_parameters)
        self._generate = generate
        self._result_volume = result_volume
        self._reader_parameters = reader_parameters

    @property
    def service(self) -> TrainingService:
        return TrainingService.DDM

    @property
    def backend_target(self) -> str:
        return self._backend_target

    @property
    def result_registration_available(self) -> bool:
        """结果读取未配置时基座完成也不登记，只保留可诊断的在途保护。"""
        return self._result_volume is not None

    def input_identity(self, command_id: UUID) -> str:
        return self._training_directory_id(command_id)

    def identity_matches(self, job: Mapping[str, Any], *, identity: str) -> bool:
        return str(job.get("aug_dataset_id")) == identity

    def all_jobs(self) -> Mapping[str, Mapping[str, Any]]:
        return self._backend.all_jobs()

    def status(self, job_id: str) -> Mapping[str, Any]:
        return self._backend.status(job_id)

    def cancel(self, *, job_id: str) -> Mapping[str, Any]:
        """DDM 已派发取消的停止核实由 S082 拥有，本部署未开放该入口。"""
        raise TrainingBackendUnavailableError(
            "DDM 已派发命令的停止核实尚未开放，不能证明进程已停止"
        )

    def prepare(
        self,
        *,
        command: TrainingCommand,
        storage: ObjectStorage,
        owned: AllocatedWorkcopies,
    ) -> PreparedTrainingDispatch:
        try:
            frozen = decode_frozen_ddm_input(command.payload)
        except (KeyError, TypeError, ValueError) as error:
            raise TrainingPreparationRefusedError(
                "DISPATCH_PAYLOAD_INVALID", "DDM 训练命令冻结负载不可还原"
            ) from error
        try:
            training_parameters, validation_parameters = self._parameters()
        except (
            TrainingResultUnavailableError,
            TrainingResultInvalidError,
            OSError,
            ValueError,
        ) as error:
            raise TrainingPreparationRefusedError(
                "BASE_CONFIG_UNAVAILABLE",
                "部署训练配置不可读或读取器字段不完整，未发送基座启动请求",
            ) from error
        materialized = self._materialize(frozen=frozen, storage=storage)
        owned.root = materialized.root
        consumption = self._verify_consumption(
            frozen=frozen,
            materialized=materialized,
            training_parameters=training_parameters,
            validation_parameters=validation_parameters,
        )
        training_id = self._training_directory_id(command.id)
        validation_id = self._validation_directory_id(command.id)
        self._expose(
            sources=(
                (materialized.training_directory, self._dataset_root / training_id),
                (materialized.validation_directory, self._dataset_root / validation_id),
            ),
            owned=owned,
        )
        return PreparedTrainingDispatch(
            identity=training_id,
            dispatch_facts=_dispatch_facts(
                frozen=frozen,
                training_id=training_id,
                validation_id=validation_id,
                consumption=consumption,
            ),
            start=lambda: self._backend.start(
                training_dataset_id=training_id, validation_dataset_id=validation_id
            ),
        )

    def result_object_keys(self, *, command_id: UUID, candidate: str) -> tuple[str, str]:
        naming = TRAINING_RESULT_NAMING[TrainingService.DDM]
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
            raise TrainingResultUnavailableError("训练结果读取尚未配置")
        facts = command.dispatch_facts or {}
        training_dataset_id = str(facts.get("training_dataset_id", ""))
        validation_dataset_id = str(facts.get("validation_dataset_id", ""))
        frozen = decode_frozen_ddm_input(command.payload)
        if training_dataset_id != self._training_directory_id(
            command.id
        ) or validation_dataset_id != self._validation_directory_id(command.id):
            raise TrainingResultInvalidError("消费目录与本命令身份不一致")
        source = self._result_volume.locate(
            base_job_id=base_job_id,
            training_dataset_id=training_dataset_id,
            validation_dataset_id=validation_dataset_id,
            training_consumer_parameters=frozen.training.consumer_parameters,
            validation_consumer_parameters=frozen.validation_consumer_parameters,
            frozen=frozen,
            dispatch_facts=facts,
        )
        config_size, config_sha256 = copy_result_file(
            source=source.config_path, storage=storage, object_key=config_object_key
        )
        checkpoint_size, checkpoint_sha256 = copy_result_file(
            source=source.checkpoint_path, storage=storage, object_key=checkpoint_object_key
        )
        return RegisteredTrainingResult(
            config_object_key=config_object_key,
            config_sha256=config_sha256,
            config_size=config_size,
            checkpoint_object_key=checkpoint_object_key,
            checkpoint_sha256=checkpoint_sha256,
            checkpoint_size=checkpoint_size,
        )

    def _parameters(self) -> tuple[Mapping[str, object], Mapping[str, object]]:
        if self._reader_parameters is not None:
            return self._reader_parameters()
        return self._training_consumer_parameters, self._validation_consumer_parameters

    def _materialize(
        self, *, frozen: FrozenDdmInput, storage: ObjectStorage
    ) -> MaterializedDdmInput:
        try:
            return materialize_ddm_input(
                frozen=frozen,
                storage=storage,
                root=self._dataset_root,
            )
        except DatasetRefusedError:
            raise
        except (ObjectStorageUnavailableError, OSError) as error:
            raise TrainingPreparationRefusedError(
                "STORAGE_UNAVAILABLE", "训练输入工作副本准备失败"
            ) from error

    def _verify_consumption(
        self,
        *,
        frozen: FrozenDdmInput,
        materialized: MaterializedDdmInput,
        training_parameters: Mapping[str, object],
        validation_parameters: Mapping[str, object],
    ) -> DdmInputConsumption:
        try:
            return verify_ddm_input_consumption(
                frozen=frozen,
                materialized=materialized,
                training_consumer_parameters=training_parameters,
                validation_consumer_parameters=validation_parameters,
                generate=self._generate,
            )
        except DatasetRefusedError as error:
            # 核对阶段的任何拒绝都是确定失败：只有材料化/暴露时的目录占用才可能是并发准备。
            raise TrainingPreparationRefusedError(error.code.value, error.detail) from error
        except (ObjectStorageUnavailableError, OSError) as error:
            raise TrainingPreparationRefusedError(
                "STORAGE_UNAVAILABLE", "训练输入消费核对失败"
            ) from error

    def _expose(self, *, sources: Sequence[tuple[Path, Path]], owned: AllocatedWorkcopies) -> None:
        try:
            expose_workcopies(sources=sources, owned=owned)
        except DatasetRefusedError:
            raise
        except OSError as error:
            raise TrainingPreparationRefusedError(
                "STORAGE_UNAVAILABLE", "训练输入工作副本暴露失败"
            ) from error

    @staticmethod
    def _training_directory_id(command_id: UUID) -> str:
        return f"ddm-{command_id}-training"

    @staticmethod
    def _validation_directory_id(command_id: UUID) -> str:
        return f"ddm-{command_id}-validation"


def _dispatch_facts(
    *,
    frozen: FrozenDdmInput,
    training_id: str,
    validation_id: str,
    consumption: DdmInputConsumption,
) -> dict[str, Any]:
    """冻结目标、独立输入身份与实际消费事实，作为意图的一部分原子落库。"""
    return {
        "input_id": str(frozen.input_id),
        "training_dataset_id": training_id,
        "validation_dataset_id": validation_id,
        "consumption": {
            "input_id": str(consumption.input_id),
            "training_annotation_filename": consumption.training_annotation_filename,
            "training_annotation_sha256": consumption.training_annotation_sha256,
            "validation_annotation_filename": consumption.validation_annotation_filename,
            "validation_annotation_sha256": consumption.validation_annotation_sha256,
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


__all__ = [
    "DatasetRefusalCode",
    "DdmTrainingServiceAdapter",
    "HttpDdmTrainingBackend",
    "LocalDdmResultVolume",
    "TrainingServiceAdapter",
]
