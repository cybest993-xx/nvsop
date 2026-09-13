"""MinIO 对象存储适配器：只生成原生签名，不中继视频请求体。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, BinaryIO
from urllib.parse import quote, urlsplit

from minio import Minio
from minio.datatypes import PostPolicy
from minio.error import S3Error
from urllib3.exceptions import HTTPError as Urllib3Error

from factory_sop.dataset.model import ObjectStat, UploadInstructions
from factory_sop.dataset.storage import ObjectNotFoundError, ObjectStorageUnavailableError
from factory_sop.settings import Settings

# 浏览器 FormData 和脚本 multipart 体会携带字段、分隔线及文件头；该预算只保护请求
# envelope，不改变中心随后依据对象事实执行的精确大小校验。
_POST_MULTIPART_OVERHEAD_BYTES = 64 * 1024


class MinioObjectStorage:
    """把 MinIO SDK 隔离在 `ObjectStorage` seam 后。"""

    def __init__(
        self,
        *,
        client: Minio,
        bucket: str,
        signer: Minio | None = None,
        upload_url: str | None = None,
    ) -> None:
        self._client = client
        self._bucket = bucket
        self._signer = signer or client
        self._upload_url = upload_url

    @classmethod
    def from_settings(cls, settings: Settings) -> MinioObjectStorage:
        """从已解析配置创建客户端；缺配置时拒绝进入伪本地上传路径。"""
        if (
            settings.minio_endpoint is None
            or settings.minio_bucket is None
            or settings.minio_access_key is None
            or settings.minio_secret_key is None
        ):
            raise ObjectStorageUnavailableError("MinIO 尚未完成配置")
        internal = _client(
            endpoint=settings.minio_endpoint,
            access_key=settings.minio_access_key.get_secret_value(),
            secret_key=settings.minio_secret_key.get_secret_value(),
        )
        # 签名需要 bucket region，但公开地址可能是浏览器可达而容器不可达的 localhost
        # 反代。先通过内部地址读取 region，再让公开 signer 只负责本地生成表单签名。
        region = internal._get_region(settings.minio_bucket)
        public_endpoint = settings.minio_public_endpoint or settings.minio_endpoint
        public_upload_url = (
            f"{_endpoint_url(public_endpoint)}/{quote(settings.minio_bucket, safe='')}"
        )
        signer = _client(
            endpoint=public_endpoint,
            access_key=settings.minio_access_key.get_secret_value(),
            secret_key=settings.minio_secret_key.get_secret_value(),
            region=region,
        )
        return cls(
            client=internal,
            bucket=settings.minio_bucket,
            signer=signer,
            upload_url=public_upload_url,
        )

    def create_upload(
        self,
        *,
        object_key: str,
        declared_size: int,
        max_bytes: int,
        expires_at: datetime,
    ) -> UploadInstructions:
        """签发精确 key 与受控大小范围的短期 POST policy。"""
        if declared_size <= 0 or declared_size > max_bytes:
            raise ObjectStorageUnavailableError("上传大小不在部署限制内")
        expiration = expires_at.astimezone(UTC).replace(tzinfo=None)
        policy = PostPolicy(self._bucket, expiration)
        policy.add_equals_condition("key", object_key)
        # multipart 请求体比文件本身多出表单 envelope；确认阶段仍再次从对象读取实际大小。
        policy.add_content_length_range_condition(
            declared_size,
            declared_size + _POST_MULTIPART_OVERHEAD_BYTES,
        )
        try:
            form = dict(self._signer.presigned_post_policy(policy))
        except (S3Error, Urllib3Error, OSError, ValueError) as error:
            raise ObjectStorageUnavailableError("无法生成 MinIO 上传说明") from error
        url = form.pop("url", None) or self._upload_url
        if not isinstance(url, str) or not url:
            raise ObjectStorageUnavailableError("MinIO 未配置上传地址")
        # MinIO SDK 返回签名字段，不会把 policy 中的 key 复制到 multipart 表单。
        form["key"] = object_key
        return UploadInstructions(
            method="POST",
            url=url,
            fields=form,
            headers={},
            expires_at=expires_at,
            max_bytes=max_bytes,
            object_key=object_key,
        )

    def stat(self, *, object_key: str) -> ObjectStat:
        """读取对象大小和版本；不信任 ETag 充当 SHA-256。"""
        try:
            found = self._client.stat_object(self._bucket, object_key)
        except S3Error as error:
            if error.code in {"NoSuchKey", "NoSuchObject", "NoSuchBucket", "NotFound"}:
                raise ObjectNotFoundError(object_key) from error
            raise ObjectStorageUnavailableError("无法读取 MinIO 对象") from error
        except (Urllib3Error, OSError) as error:
            raise ObjectStorageUnavailableError("无法连接 MinIO") from error
        version_id = getattr(found, "version_id", None)
        if found.size is None:
            raise ObjectStorageUnavailableError("MinIO 未返回对象大小")
        return ObjectStat(size=int(found.size), version_id=version_id or None)

    def download_to(
        self,
        *,
        object_key: str,
        destination: BinaryIO,
        version_id: str | None = None,
    ) -> None:
        """流式下载指定对象代次并在完成后释放 HTTP 连接。"""
        response: Any = None
        try:
            response = self._client.get_object(
                self._bucket,
                object_key,
                version_id=version_id,
            )
            for chunk in response.stream(1024 * 1024):
                destination.write(chunk)
        except S3Error as error:
            if error.code in {"NoSuchKey", "NoSuchObject", "NoSuchBucket", "NotFound"}:
                raise ObjectNotFoundError(object_key) from error
            raise ObjectStorageUnavailableError("无法读取 MinIO 对象") from error
        except (Urllib3Error, OSError) as error:
            raise ObjectStorageUnavailableError("无法连接 MinIO") from error
        finally:
            if response is not None:
                response.close()
                response.release_conn()

    def finalize_upload(
        self,
        *,
        object_key: str,
        source: BinaryIO,
        size: int,
    ) -> ObjectStat:
        """用服务端凭据写入定稿对象；客户端永远拿不到这个写目标。"""
        try:
            source.seek(0)
            self._client.put_object(
                self._bucket,
                object_key,
                source,
                length=size,
                content_type="application/octet-stream",
            )
            return self.stat(object_key=object_key)
        except (S3Error, Urllib3Error, OSError, ValueError) as error:
            raise ObjectStorageUnavailableError("无法保存 MinIO 定稿对象") from error

    def delete(self, *, object_key: str) -> None:
        """删除已定稿的临时对象；对象已经不存在时视为成功。"""
        try:
            self._client.remove_object(self._bucket, object_key)
        except S3Error as error:
            if error.code in {"NoSuchKey", "NoSuchObject", "NotFound"}:
                return
            raise ObjectStorageUnavailableError("无法删除 MinIO 临时对象") from error
        except (Urllib3Error, OSError) as error:
            raise ObjectStorageUnavailableError("无法连接 MinIO") from error


def _endpoint_url(endpoint: str) -> str:
    parsed = urlsplit(endpoint)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.path not in {"", "/"}:
        raise ObjectStorageUnavailableError("MinIO endpoint 必须是带主机的 HTTP(S) 地址")
    return f"{parsed.scheme}://{parsed.netloc}"


def _client(*, endpoint: str, access_key: str, secret_key: str, region: str | None = None) -> Minio:
    parsed = urlsplit(_endpoint_url(endpoint))
    return Minio(
        parsed.netloc,
        access_key=access_key,
        secret_key=secret_key,
        secure=parsed.scheme == "https",
        region=region,
    )
