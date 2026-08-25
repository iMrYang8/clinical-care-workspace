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
    FRONTEND_HOST: str = "http://localhost:5173"
    FASTAPI_ENV: Literal["development"] | None = None
    ENABLE_DEMO_AUTH: bool = False
    AI_PROVIDER: Literal["deterministic", "openai", "disabled"] = "deterministic"
    REMOTE_TEXT_EGRESS_ENABLED: bool = False
    OPENAI_API_KEY: str | None = None
    OPENAI_EXTRACT_MODEL: str | None = None
    OPENAI_REVIEW_MODEL: str | None = None
    AI_JOB_LEASE_SECONDS: int = 300
    PRESIDIO_REQUIRED: bool = True
    PRESIDIO_NLP_MODEL: str = "en_core_web_sm"
    IMPORTANCE_LEARNING_ENABLED: bool = True
    DATA_DECAY_ENABLED: bool = True
    DATA_DECAY_DRY_RUN: bool = True

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
    FIRST_SUPERUSER: EmailStr
    FIRST_SUPERUSER_PASSWORD: str

    def _check_default_secret(self, var_name: str, value: str | None) -> None:
        if value == "changethis":
            message = (
                f'The value of {var_name} is "changethis", '
                "for security, please change it, at least for deployments."
            )
            if self.FASTAPI_ENV == "development":
                warnings.warn(message, stacklevel=1)
            else:
                raise ValueError(message)

    @model_validator(mode="after")
    def _enforce_non_default_secrets(self) -> Self:
        self._check_default_secret("SECRET_KEY", self.SECRET_KEY)
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
        self._check_default_secret(
            "FIRST_SUPERUSER_PASSWORD", self.FIRST_SUPERUSER_PASSWORD
        )

        return self


settings = Settings()  # type: ignore
