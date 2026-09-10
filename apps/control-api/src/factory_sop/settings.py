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

# The deployment states the required transport explicitly, but there is only one valid state:
# §六 requires Secure cookies and records no development exception. Local development must
# terminate TLS rather than changing a production security attribute.
CookieTransport = Literal["require_https"]
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

    @model_validator(mode="after")
    def _validate_deployment_values(self) -> Settings:
        if self.session_absolute_lifetime_minutes < self.session_idle_timeout_minutes:
            raise ValueError(
                "session_absolute_lifetime_minutes must not be shorter than the idle timeout; "
                "the idle timeout would then be configured but never able to fire, and the "
                "deployment would believe an unattended browser is closed when it is not"
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
