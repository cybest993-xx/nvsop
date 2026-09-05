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
from typing import Literal

from pydantic import Field, SecretStr, ValidationError, model_validator
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

ENVIRONMENT_PREFIX = "SOP_"
SECRET_FILE_SUFFIX = "_FILE"  # pragma: allowlist secret

LogLevel = Literal["debug", "info", "warning", "error"]

# The deployment states the required transport explicitly, but there is only one valid state:
# §六 requires Secure cookies and records no development exception. Local development must
# terminate TLS rather than changing a production security attribute.
CookieTransport = Literal["require_https"]


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

    @model_validator(mode="after")
    def _absolute_lifetime_outlasts_the_idle_timeout(self) -> Settings:
        if self.session_absolute_lifetime_minutes < self.session_idle_timeout_minutes:
            raise ValueError(
                "session_absolute_lifetime_minutes must not be shorter than the idle timeout; "
                "the idle timeout would then be configured but never able to fire, and the "
                "deployment would believe an unattended browser is closed when it is not"
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
            name for name, field in cls.model_fields.items() if field.annotation is SecretStr
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
            return cls.model_validate(values)
        except ValidationError as error:
            raise ConfigurationError(str(error)) from error


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
