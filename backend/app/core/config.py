import warnings
from typing import Literal, Self

from pydantic import (
    EmailStr,
    HttpUrl,
    PostgresDsn,
    computed_field,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

_KNOWN_LOCAL_SECRET_VALUES = {
    "changethis",
    "nightingale-app-local",
    "4e69676874696e67616c652d73796e7468657469632d6465762d6b65792d3031",
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        # Use top level .env file (one level above ./backend/)
        env_file="../.env",
        env_ignore_empty=True,
        extra="ignore",
    )
    API_V1_STR: str = "/api/v1"
    SECRET_KEY: str
    # Base64/hex encoded 32-byte AES master key. Local demo falls back to a
    # SHA-256 derivation of SECRET_KEY; deployed environments set this directly.
    FIELD_ENCRYPTION_MASTER_KEY: str | None = None
    # 60 minutes * 24 hours * 8 days = 8 days
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 24 * 8
    AUTH_COOKIE_NAME: str = "nightingale_session"
    AUTH_COOKIE_SECURE: bool = True
    AUTH_COOKIE_SAMESITE: Literal["lax", "strict"] = "lax"
    # The supported browser path is same-origin TLS. A standalone Vite origin
    # must be opted in explicitly for source-only development.
    FRONTEND_HOST: str = "https://localhost"
    BROWSER_TRUSTED_ORIGINS: str = ""
    FASTAPI_ENV: Literal["development", "production"] | None = None
    ENABLE_DEMO_AUTH: bool = False
    AI_PROVIDER: Literal["deterministic", "openai", "disabled"] = "deterministic"
    REMOTE_TEXT_EGRESS_ENABLED: bool = False
    OPENAI_API_KEY: str | None = None
    OPENAI_EXTRACT_MODEL: str | None = None
    OPENAI_REVIEW_MODEL: str | None = None
    AI_JOB_LEASE_SECONDS: int = 300
    AI_WORKER_POLL_SECONDS: float = 2.0
    PRESIDIO_REQUIRED: bool = True
    PRESIDIO_NLP_MODEL: str = "en_core_web_sm"
    IMPORTANCE_LEARNING_ENABLED: bool = True
    DATA_DECAY_ENABLED: bool = True
    DATA_DECAY_DRY_RUN: bool = True
    VOICE_TRANSCRIPTION_PROVIDER: Literal["disabled", "openai", "local"] = "disabled"
    REMOTE_AUDIO_EGRESS_ENABLED: bool = False
    STRICT_NO_AUDIO_EGRESS: bool = False
    OPENAI_TRANSCRIBE_MODEL: str | None = None
    VOICE_MAX_CHUNK_BYTES: int = 8 * 1024 * 1024
    VOICE_MAX_SESSION_BYTES: int = 512 * 1024 * 1024
    VOICE_CHUNK_READ_TIMEOUT_SECONDS: float = 30.0
    # Declaration bounds protect API work; decoded PCM has a tighter processing
    # bound so a compressed upload cannot expand until disk or memory is full.
    VOICE_MAX_DECODED_DURATION_MS: int = 60 * 60 * 1_000
    VOICE_MAX_NORMALIZED_BYTES: int = 128 * 1024 * 1024
    VOICE_FFMPEG_BIN: str = "ffmpeg"
    VOICE_FFMPEG_TIMEOUT_SECONDS: int = 120
    VOICE_ASR_TIMEOUT_SECONDS: int = 600
    # Covers bounded preprocessing (up to eight device tracks) plus bounded ASR.
    VOICE_JOB_LEASE_SECONDS: int = 1_800
    LOCAL_ASR_MODEL_DIR: str | None = None
    PYANNOTE_ENABLED: bool = False
    PYANNOTE_MODEL_DIR: str | None = None
    LIVE_TRANSCRIPT_ENABLED: bool = False

    PROJECT_NAME: str
    SENTRY_DSN: HttpUrl | None = None
    DATABASE_URL: PostgresDsn
    # Only one-shot migration/bootstrap processes receive this credential.
    # The long-running backend receives DATABASE_URL for the restricted runtime
    # role and leaves MIGRATION_DATABASE_URL unset.
    MIGRATION_DATABASE_URL: PostgresDsn | None = None
    POSTGRES_APP_PASSWORD: str | None = None

    @field_validator("DATABASE_URL", "MIGRATION_DATABASE_URL", mode="before")
    @classmethod
    def _use_psycopg_driver(
        cls, value: str | PostgresDsn | None
    ) -> str | PostgresDsn | None:
        if value is None:
            return value
        database_url = str(value)
        for scheme in ("postgres://", "postgresql://"):
            if database_url.startswith(scheme):
                return database_url.replace(scheme, "postgresql+psycopg://", 1)
        return database_url

    SMTP_TLS: bool = True
    SMTP_SSL: bool = False
    SMTP_PORT: int = 587
    SMTP_HOST: str | None = None
    SMTP_USER: str | None = None
    SMTP_PASSWORD: str | None = None
    EMAILS_FROM_EMAIL: EmailStr | None = None
    EMAILS_FROM_NAME: str | None = None

    @model_validator(mode="after")
    def _set_default_emails_from(self) -> Self:
        if not self.EMAILS_FROM_NAME:
            self.EMAILS_FROM_NAME = self.PROJECT_NAME
        return self

    EMAIL_RESET_TOKEN_EXPIRE_HOURS: int = 48

    @computed_field  # type: ignore[prop-decorator]
    @property
    def emails_enabled(self) -> bool:
        return bool(self.SMTP_HOST and self.EMAILS_FROM_EMAIL)

    EMAIL_TEST_USER: EmailStr = "test@example.com"
    # Retained only for the template's local Playwright compatibility. Production
    # identities are created by the explicit provision-clinic-admin command.
    FIRST_SUPERUSER: EmailStr | None = None
    FIRST_SUPERUSER_PASSWORD: str | None = None

    def _check_default_secret(self, var_name: str, value: str | None) -> None:
        if value in _KNOWN_LOCAL_SECRET_VALUES:
            message = (
                f"The value of {var_name} is a tracked local fixture value; "
                "replace it for every deployment."
            )
            if self.FASTAPI_ENV == "development":
                warnings.warn(message, stacklevel=1)
            else:
                raise ValueError(message)

    @model_validator(mode="after")
    def _enforce_non_default_secrets(self) -> Self:
        if self.FASTAPI_ENV != "development":
            if not self.FIELD_ENCRYPTION_MASTER_KEY:
                raise ValueError(
                    "FIELD_ENCRYPTION_MASTER_KEY is required outside development"
                )
            if (
                self.AI_PROVIDER == "openai"
                and self.REMOTE_TEXT_EGRESS_ENABLED
                and not self.PRESIDIO_REQUIRED
            ):
                raise ValueError(
                    "PRESIDIO_REQUIRED must remain enabled for remote text egress"
                )
            if (
                self.VOICE_TRANSCRIPTION_PROVIDER == "openai"
                and self.REMOTE_AUDIO_EGRESS_ENABLED
                and not self.PRESIDIO_REQUIRED
            ):
                raise ValueError(
                    "PRESIDIO_REQUIRED must remain enabled for remote audio workflows"
                )
        self._check_default_secret("SECRET_KEY", self.SECRET_KEY)
        self._check_default_secret(
            "FIELD_ENCRYPTION_MASTER_KEY", self.FIELD_ENCRYPTION_MASTER_KEY
        )
        for host in self.DATABASE_URL.hosts():
            self._check_default_secret("DATABASE_URL password", host["password"])
        if self.MIGRATION_DATABASE_URL is not None:
            for host in self.MIGRATION_DATABASE_URL.hosts():
                self._check_default_secret(
                    "MIGRATION_DATABASE_URL password", host["password"]
                )
        if self.POSTGRES_APP_PASSWORD is not None:
            self._check_default_secret(
                "POSTGRES_APP_PASSWORD", self.POSTGRES_APP_PASSWORD
            )
        if self.FIRST_SUPERUSER_PASSWORD is not None:
            self._check_default_secret(
                "FIRST_SUPERUSER_PASSWORD", self.FIRST_SUPERUSER_PASSWORD
            )

        return self


settings = Settings()  # type: ignore
