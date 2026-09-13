"""The center backend's single configuration object.

Environment variables are the only source, and a secret arrives as the **path** to a file
rather than as the value itself (`solution-and-roadmap.md` §六). Loading is fail-fast: a
missing variable, an unreadable secret file, an empty one, or a variable this object does
not recognize refuses start-up rather than falling back to a default. Every weak default is
a deployment that looks healthy while being wrong.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Literal, get_args
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, ValidationError, model_validator
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

ENVIRONMENT_PREFIX = "SOP_"
SECRET_FILE_SUFFIX = "_FILE"  # pragma: allowlist secret

LogLevel = Literal["debug", "info", "warning", "error"]

# 正式部署和 HTTPS 开发模式要求 Secure cookie；只有固定 main 本地实例的明确配置
# 才允许 HTTP 会话，避免把安全放宽变成任意部署选项。
CookieTransport = Literal["require_https", "allow_http"]
DeploymentMode = Literal["production", "fixed_main"]
_REQUIRED_RUNTIME_SETTINGS = (
    "minio_endpoint",
    "minio_bucket",
    "minio_access_key",
    "minio_secret_key",
    "redis_url",
    "dataset_upload_ttl_seconds",
    "dataset_max_upload_bytes",
    "dataset_supported_codecs",
    "media_probe_binary",
    "media_probe_timeout_seconds",
)


class ConfigurationError(Exception):
    """Start-up is refused: the environment does not describe a usable deployment."""


class Settings(BaseSettings):
    """Resolved deployment configuration. Construct it with `from_environment`."""

    model_config = SettingsConfigDict(frozen=True, extra="forbid")

    log_level: LogLevel

    # PostgreSQL is the center's store (§六). The engine itself arrives with the data model
    # (C3); the credential is a deployment fact from the moment the service is installed,
    # and it is this object's one secret — the reason the `*_FILE` mechanism above exists
    # rather than being described in prose and implemented later.
    database_host: str
    database_port: int
    database_name: str
    database_user: str
    database_password: SecretStr = Field(repr=False)

    # How long a session survives, idle and in total (§六: sessions are server-side records,
    # so both are enforced here rather than encoded in a cookie). Minutes, because that is the
    # granularity an operator setting a shift-length timeout thinks in. `create_app` turns
    # these into `auth`'s `SessionPolicy`: this object sits below the domain in the layering,
    # so it carries the configured numbers rather than the domain type built from them.
    session_idle_timeout_minutes: int = Field(gt=0)
    session_absolute_lifetime_minutes: int = Field(gt=0)
    session_cookie_transport: CookieTransport
    deployment_mode: DeploymentMode = "production"

    # Signs the CSRF token derived from each session token (`auth/csrf.py`). A secret, so it
    # arrives as a file path like the database password.
    csrf_secret: SecretStr = Field(repr=False)

    # 直接构造 Settings 仍服务于不需要基础设施的 adapter 测试；from_environment 会要求
    # 生产运行所需的完整数据集、对象存储和任务队列配置。
    minio_endpoint: str | None = None
    minio_public_endpoint: str | None = None
    minio_bucket: str | None = None
    minio_access_key: SecretStr | None = Field(default=None, repr=False)
    minio_secret_key: SecretStr | None = Field(default=None, repr=False)
    redis_url: SecretStr | None = Field(default=None, repr=False)
    dataset_upload_ttl_seconds: int = Field(default=900, gt=0, le=86400)
    dataset_max_upload_bytes: int = Field(default=8 * 1024**3, gt=0)
    dataset_supported_codecs: str = "h264,h265"
    media_probe_binary: str = "ffprobe"
    media_probe_timeout_seconds: int = Field(default=60, gt=0, le=3600)
    annotation_backend_url: str | None = None
    annotation_media_origin: str | None = None
    annotation_data_root: str | None = None
    annotation_http_timeout_seconds: int = Field(default=120, gt=0, le=3600)
    annotation_context_ttl_seconds: int = Field(default=3600, gt=0, le=86400)
    # ARQ 默认值保持 1 小时；开发环境通过环境变量缩短为 5 秒，让 worker 健康事实及时过期。
    worker_health_check_interval_seconds: int = Field(default=3600, gt=0, le=86400)

    @model_validator(mode="after")
    def _validate_deployment_values(self) -> Settings:
        if self.session_absolute_lifetime_minutes < self.session_idle_timeout_minutes:
            raise ValueError(
                "session_absolute_lifetime_minutes must not be shorter than the idle timeout; "
                "the idle timeout would then be configured but never able to fire, and the "
                "deployment would believe an unattended browser is closed when it is not"
            )
        if self.session_cookie_transport == "allow_http":
            if self.deployment_mode != "fixed_main":
                raise ValueError("allow_http is only valid for the fixed_main local deployment")
            if not _is_fixed_main_http_origin(self.minio_public_endpoint, port=9443):
                raise ValueError(
                    "allow_http fixed_main requires minio_public_endpoint at http://localhost:9443"
                )
            if not _is_fixed_main_http_origin(self.annotation_media_origin, port=8444):
                raise ValueError(
                    "allow_http fixed_main requires annotation_media_origin at "
                    "http://localhost:8444"
                )
        minio_values = (
            self.minio_endpoint,
            self.minio_bucket,
            self.minio_access_key,
            self.minio_secret_key,
        )
        if any(
            value is not None for value in (*minio_values, self.minio_public_endpoint)
        ) and not all(value is not None for value in minio_values):
            raise ValueError(
                "minio_endpoint, minio_bucket, minio_access_key and minio_secret_key "
                "must be configured together"
            )
        if not self.dataset_supported_codecs.strip():
            raise ValueError("dataset_supported_codecs must not be empty")
        if (self.annotation_backend_url is None) != (self.annotation_media_origin is None):
            raise ValueError(
                "annotation_backend_url and annotation_media_origin must be configured together"
            )
        if self.annotation_data_root is not None:
            if not self.annotation_data_root.strip():
                raise ValueError("annotation_data_root must not be empty")
            if not Path(self.annotation_data_root).is_absolute():
                raise ValueError("annotation_data_root must be an absolute path")
        if self.annotation_backend_url is not None and self.annotation_data_root is None:
            raise ValueError("annotation_data_root must be configured with annotation_backend_url")
        if self.annotation_backend_url is not None:
            backend_url = urlsplit(self.annotation_backend_url)
            try:
                backend_port = backend_url.port
            except ValueError as error:
                raise ValueError("annotation_backend_url has an invalid port") from error
            if (
                backend_url.scheme not in {"http", "https"}
                or not backend_url.hostname
                or backend_url.username is not None
                or backend_url.password is not None
                or backend_url.query
                or backend_url.fragment
                or (backend_port is not None and not 1 <= backend_port <= 65535)
            ):
                raise ValueError(
                    "annotation_backend_url must be an HTTP(S) URL without userinfo, "
                    "query, or fragment"
                )
        if self.annotation_media_origin is not None:
            media_origin = urlsplit(self.annotation_media_origin)
            try:
                media_port = media_origin.port
            except ValueError as error:
                raise ValueError("annotation_media_origin has an invalid port") from error
            if (
                media_origin.scheme not in {"http", "https"}
                or not media_origin.hostname
                or media_origin.username is not None
                or media_origin.password is not None
                or media_origin.query
                or media_origin.fragment
                or media_origin.path not in {"", "/"}
                or (media_port is not None and not 1 <= media_port <= 65535)
            ):
                raise ValueError(
                    "annotation_media_origin must be an HTTP(S) origin without userinfo, "
                    "path, query, or fragment"
                )
            if media_origin.scheme == "http" and self.session_cookie_transport != "allow_http":
                raise ValueError(
                    "HTTP annotation_media_origin is only valid with allow_http local mode"
                )
        return self

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Take values only from the mapping handed to `from_environment`.

        The default sources would also read the ambient process environment and any
        `.env` file, which makes the effective configuration depend on where the process
        happens to run and leaves tests reaching for the process environment to set up a
        case (harness §4 forbids that). One source, passed in explicitly.
        """
        return (init_settings,)

    @classmethod
    def from_environment(cls, environ: Mapping[str, str]) -> Settings:
        """Resolve settings from `environ`, raising `ConfigurationError` on anything unusable.

        Pass `os.environ` at the process entrypoint; pass a literal mapping in a test.
        """
        secret_fields = {
            name
            for name, field in cls.model_fields.items()
            if field.annotation is SecretStr or SecretStr in get_args(field.annotation)
        }
        accepted = {
            _variable_name(name) + (SECRET_FILE_SUFFIX if name in secret_fields else "")
            for name in cls.model_fields
        }
        rejected_value_forms = {_variable_name(name): name for name in secret_fields}

        for variable in sorted(environ):
            if not variable.startswith(ENVIRONMENT_PREFIX) or variable in accepted:
                continue
            if variable in rejected_value_forms:
                raise ConfigurationError(
                    f"{variable} must not carry the secret itself; provide "
                    f"{variable}{SECRET_FILE_SUFFIX} pointing at a file that holds it"
                )
            raise ConfigurationError(
                f"{variable} is not a recognized setting; a misspelled variable would "
                "otherwise be silently ignored"
            )

        values: dict[str, object] = {}
        for name in cls.model_fields:
            variable = _variable_name(name)
            if name in secret_fields:
                path = environ.get(variable + SECRET_FILE_SUFFIX)
                if path is not None:
                    values[name] = read_secret_file(Path(path), variable + SECRET_FILE_SUFFIX)
            elif variable in environ:
                values[name] = environ[variable]

        try:
            settings = cls.model_validate(values)
        except ValidationError as error:
            raise ConfigurationError(str(error)) from error
        _require_runtime_infrastructure(settings)
        missing = [
            _variable_name(name) for name in _REQUIRED_RUNTIME_SETTINGS if name not in values
        ]
        if missing:
            raise ConfigurationError("部署缺少必需配置：" + ", ".join(missing))
        return settings


def _require_runtime_infrastructure(settings: Settings) -> None:
    """拒绝缺失对象存储或任务队列的可运行配置。"""
    if any(
        value is None
        for value in (
            settings.minio_endpoint,
            settings.minio_bucket,
            settings.minio_access_key,
            settings.minio_secret_key,
        )
    ):
        raise ConfigurationError("部署必须完整配置 MinIO 对象存储")
    if settings.minio_bucket is None or not settings.minio_bucket.strip():
        raise ConfigurationError("MinIO bucket 不能为空")
    if not any(item.strip() for item in settings.dataset_supported_codecs.split(",")):
        raise ConfigurationError("dataset_supported_codecs 不能为空")
    if settings.redis_url is None:
        raise ConfigurationError("部署必须配置 Redis 任务队列")
    redis_url = urlsplit(settings.redis_url.get_secret_value())
    try:
        port = redis_url.port
    except ValueError as error:
        raise ConfigurationError("redis_url 端口无效") from error
    if port is not None and not 1 <= port <= 65535:
        raise ConfigurationError("redis_url 端口无效")
    if redis_url.scheme not in {"redis", "rediss"} or not redis_url.hostname:
        raise ConfigurationError("redis_url 必须使用带主机的 redis(s) 地址")
    try:
        database = int(redis_url.path.strip("/") or "0")
    except ValueError as error:
        raise ConfigurationError("redis_url 数据库编号无效") from error
    if database < 0:
        raise ConfigurationError("redis_url 数据库编号无效")
    for label, endpoint in (
        ("minio_endpoint", settings.minio_endpoint),
        ("minio_public_endpoint", settings.minio_public_endpoint),
    ):
        if endpoint is None:
            continue
        parsed = urlsplit(endpoint)
        try:
            port = parsed.port
        except ValueError as error:
            raise ConfigurationError(f"{label} 端口无效") from error
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.path
            not in {
                "",
                "/",
            }
            or (port is not None and not 1 <= port <= 65535)
        ):
            raise ConfigurationError(f"{label} 必须是带主机的 HTTP(S) 地址")


def _variable_name(field_name: str) -> str:
    return ENVIRONMENT_PREFIX + field_name.upper()


def _is_fixed_main_http_origin(value: str | None, *, port: int) -> bool:
    if value is None:
        return False
    parsed = urlsplit(value)
    try:
        parsed_port = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme == "http"
        and parsed.hostname == "localhost"
        and parsed_port == port
        and parsed.path in {"", "/"}
        and not parsed.username
        and not parsed.password
        and not parsed.query
        and not parsed.fragment
    )


def read_secret_file(path: Path, variable: str) -> SecretStr:
    """Read one secret from the file `variable` points at, applying the deployment's rule.

    One trailing newline is what every way of writing such a file produces; it is not part
    of the secret. Anything else is, so only the trailing newline is removed. An empty file
    is refused rather than accepted as an empty secret.
    """
    if not path.is_file():
        raise ConfigurationError(f"the file {variable} points at does not exist: {path}")
    secret = path.read_text(encoding="utf-8").removesuffix("\n")
    if not secret:
        raise ConfigurationError(
            f"the file {variable} points at is empty: {path}; start-up is refused rather "
            "than continuing without the secret"
        )
    return SecretStr(secret)
