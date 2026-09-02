import warnings
from dataclasses import dataclass
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
    "nightingale-notification-webhook-local",
    "4e69676874696e67616c652d73796e7468657469632d6465762d6b65792d3031",
}

ExternalObservabilityRetentionEvidence = Literal[
    "unqualified",
    "deterministic_fixture",
    "deployment_policy",
    "provider_contract",
]
ExternalObservabilityRetentionReason = Literal[
    "external_retention_window_invalid",
    "external_retention_evidence_unqualified",
    "external_retention_evidence_not_production_qualified",
]


@dataclass(frozen=True)
class ExternalObservabilityRetentionCapability:
    """Qualified retention evidence for every observability sink outside the app.

    The repository-owned operational-event database enforces its own deletion
    window. Proxy access logs, container logs, and an optional APM provider sit
    beyond that repository, so Clinic onboarding must also see a deployment
    attestation for those three independent sinks.
    """

    proxy_days: int
    container_days: int
    apm_days: int
    evidence: ExternalObservabilityRetentionEvidence
    evidence_id: str
    qualified: bool
    reason_code: ExternalObservabilityRetentionReason | None


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
    PLATFORM_AUTH_COOKIE_NAME: str = "nightingale_platform_session"
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
    # Every remote model boundary shares one set of deliberately short,
    # observable deadlines.  A provider that accepts a connection but produces
    # no useful result is different from a bounded background transcription job.
    REMOTE_CONNECT_TIMEOUT_SECONDS: float = 5.0
    REMOTE_FIRST_RESULT_TIMEOUT_SECONDS: float = 15.0
    REMOTE_REQUEST_TIMEOUT_SECONDS: float = 30.0
    AI_TEXT_JOB_TIMEOUT_SECONDS: float = 75.0
    AI_JOB_LEASE_SECONDS: int = 300
    AI_WORKER_ENABLED: bool = True
    AI_WORKER_POLL_SECONDS: float = 2.0
    AI_WORKER_HEARTBEAT_MAX_AGE_SECONDS: int = 30
    PRESIDIO_REQUIRED: bool = True
    PRESIDIO_NLP_MODEL: str = "en_core_web_sm"
    # ``shadow`` records the complete candidate/exposure/feedback stream but
    # never mutates ranking weights.  ``active`` is an explicit, audited clinic
    # rollout decision rather than a permissive boolean default.
    IMPORTANCE_LEARNING_MODE: Literal["disabled", "shadow", "active"] = "shadow"
    DATA_DECAY_ENABLED: bool = True
    DATA_DECAY_DRY_RUN: bool = True
    OBSERVABILITY_RETENTION_DAYS: int = 30
    # Explicit deployment evidence for observability sinks not owned by the
    # application repository. ``deterministic_fixture`` is accepted only in
    # development; production must identify a qualified deployment policy or
    # provider contract covering proxy, container, and APM retention.
    EXTERNAL_PROXY_RETENTION_DAYS: int = 30
    EXTERNAL_CONTAINER_RETENTION_DAYS: int = 30
    EXTERNAL_APM_RETENTION_DAYS: int = 30
    EXTERNAL_OBSERVABILITY_RETENTION_EVIDENCE: ExternalObservabilityRetentionEvidence = "deterministic_fixture"
    EXTERNAL_OBSERVABILITY_RETENTION_EVIDENCE_ID: str = (
        "fixture:nightingale:external-observability-30d"
    )
    GLANCE_STALE_AFTER_MINUTES: int = 15
    PATIENT_OTP_TTL_SECONDS: int = 10 * 60
    PATIENT_OTP_MAX_ATTEMPTS: int = 5
    PATIENT_OTP_RESEND_SECONDS: int = 60
    PATIENT_CLAIM_TTL_DAYS: int = 7
    # Delivery is selected per channel so a clinic cannot silently route an SMS
    # through an email transport. Deterministic adapters are observable local
    # fixtures and are rejected outside development; production may explicitly
    # disable a channel until its live transport is configured.
    NOTIFICATION_EMAIL_PROVIDER: Literal["deterministic", "smtp", "disabled"] = (
        "deterministic"
    )
    NOTIFICATION_SMS_PROVIDER: Literal["deterministic", "twilio", "disabled"] = (
        "deterministic"
    )
    NOTIFICATION_WHATSAPP_PROVIDER: Literal["deterministic", "twilio", "disabled"] = (
        "deterministic"
    )
    NOTIFICATION_WEBHOOK_SECRET: str = "nightingale-notification-webhook-local"
    NOTIFICATION_PUBLIC_BASE_URL: HttpUrl | None = None
    NOTIFICATION_MAX_ATTEMPTS: int = 5
    NOTIFICATION_SUBMITTED_STALE_SECONDS: int = 15 * 60
    NOTIFICATION_CONNECT_TIMEOUT_SECONDS: float = 5.0
    NOTIFICATION_REQUEST_TIMEOUT_SECONDS: float = 15.0
    TWILIO_ACCOUNT_SID: str | None = None
    TWILIO_AUTH_TOKEN: str | None = None
    TWILIO_SMS_FROM: str | None = None
    TWILIO_WHATSAPP_FROM: str | None = None
    TWILIO_API_BASE_URL: str = "https://api.twilio.com"
    # Repository-owned, allowlisted operational events are retained in this
    # SQLite sink. Production Compose mounts the parent directory persistently.
    OPERATIONAL_EVENT_DB_PATH: str = "/tmp/nightingale-operational-events.sqlite3"
    OPERATIONAL_EVENT_PURGE_INTERVAL_SECONDS: float = 60 * 60
    VOICE_TRANSCRIPTION_PROVIDER: Literal["disabled", "openai", "local"] = "local"
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
    LIVE_TRANSCRIPT_PROVIDER: Literal["disabled", "deterministic", "openai"] = (
        "disabled"
    )
    OPENAI_LIVE_TRANSCRIBE_MODEL: str | None = None
    LIVE_TRANSCRIPT_MAX_FRAME_BYTES: int = 96 * 1024
    LIVE_TRANSCRIPT_MAX_SESSION_BYTES: int = 192 * 1024 * 1024
    # 24 kHz mono PCM16 is 48,000 bytes/s. The extra headroom absorbs browser
    # scheduling jitter while bounding malicious accelerated replay.
    LIVE_TRANSCRIPT_MAX_BYTES_PER_SECOND: int = 64 * 1024
    LIVE_TRANSCRIPT_FRAME_TIMEOUT_SECONDS: float = 60.0
    LIVE_TRANSCRIPT_PROVIDER_TIMEOUT_SECONDS: float = 30.0
    LIVE_TRANSCRIPT_OUTPUT_SILENCE_SECONDS: float = 20.0
    LIVE_TRANSCRIPT_LEASE_TIMEOUT_SECONDS: float = 1.0
    LIVE_TRANSCRIPT_MAX_GLOBAL_CONNECTIONS: int = 8
    LIVE_TRANSCRIPT_MAX_CLINIC_CONNECTIONS: int = 8
    LIVE_TRANSCRIPT_MAX_USER_CONNECTIONS: int = 2

    @property
    def IMPORTANCE_LEARNING_ENABLED(self) -> bool:  # noqa: N802
        """Compatibility view for code rolling forward with the new mode.

        New code must branch on ``IMPORTANCE_LEARNING_MODE`` so ``shadow`` can
        retain telemetry without applying feedback deltas.  Keeping this
        read-only view prevents an older worker from treating shadow mode as an
        active learning rollout during a rolling deploy.
        """

        return self.IMPORTANCE_LEARNING_MODE == "active"

    PROJECT_NAME: str
    NIGHTINGALE_SOURCE_COMMIT: str = "unknown"
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
        if self.NOTIFICATION_WEBHOOK_SECRET == self.SECRET_KEY:
            raise ValueError(
                "NOTIFICATION_WEBHOOK_SECRET must be independent from SECRET_KEY"
            )
        if not 1 <= self.OBSERVABILITY_RETENTION_DAYS <= 30:
            raise ValueError(
                "OBSERVABILITY_RETENTION_DAYS must be between 1 and 30 days"
            )
        if not 0 < self.OPERATIONAL_EVENT_PURGE_INTERVAL_SECONDS <= 60 * 60:
            raise ValueError(
                "OPERATIONAL_EVENT_PURGE_INTERVAL_SECONDS must be greater than zero "
                "and no more than one hour"
            )
        external_retention = {
            "proxy": self.EXTERNAL_PROXY_RETENTION_DAYS,
            "container": self.EXTERNAL_CONTAINER_RETENTION_DAYS,
            "apm": self.EXTERNAL_APM_RETENTION_DAYS,
        }
        invalid_external_retention = sorted(
            sink for sink, days in external_retention.items() if not 1 <= days <= 30
        )
        if invalid_external_retention:
            raise ValueError(
                "External observability retention must be between 1 and 30 days "
                "for every sink: " + ",".join(invalid_external_retention)
            )
        evidence_id = self.EXTERNAL_OBSERVABILITY_RETENTION_EVIDENCE_ID.strip()
        if not evidence_id or len(evidence_id) > 200:
            raise ValueError(
                "EXTERNAL_OBSERVABILITY_RETENTION_EVIDENCE_ID must identify the "
                "qualified policy or contract"
            )
        self.EXTERNAL_OBSERVABILITY_RETENTION_EVIDENCE_ID = evidence_id
        if self.FASTAPI_ENV != "development":
            if self.EXTERNAL_OBSERVABILITY_RETENTION_EVIDENCE not in {
                "deployment_policy",
                "provider_contract",
            }:
                raise ValueError(
                    "Production requires qualified external observability retention "
                    "evidence for proxy, container, and APM sinks"
                )
            if evidence_id.startswith("fixture:"):
                raise ValueError(
                    "Production external observability retention evidence cannot "
                    "reference a deterministic fixture"
                )
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
            if (
                self.LIVE_TRANSCRIPT_PROVIDER == "openai"
                and self.REMOTE_AUDIO_EGRESS_ENABLED
                and not self.PRESIDIO_REQUIRED
            ):
                raise ValueError(
                    "PRESIDIO_REQUIRED must remain enabled for remote live audio workflows"
                )
            configured_notification_providers = {
                "email": self.NOTIFICATION_EMAIL_PROVIDER,
                "sms": self.NOTIFICATION_SMS_PROVIDER,
                "whatsapp": self.NOTIFICATION_WHATSAPP_PROVIDER,
            }
            deterministic_channels = sorted(
                channel
                for channel, provider in configured_notification_providers.items()
                if provider == "deterministic"
            )
            if deterministic_channels:
                raise ValueError(
                    "Deterministic notification adapters are development-only: "
                    + ",".join(deterministic_channels)
                )
            if self.NOTIFICATION_EMAIL_PROVIDER == "smtp" and not self.emails_enabled:
                raise ValueError(
                    "SMTP_HOST and EMAILS_FROM_EMAIL are required for SMTP notifications"
                )
            twilio_channels = {
                channel
                for channel, provider in configured_notification_providers.items()
                if provider == "twilio"
            }
            if twilio_channels and not (
                self.TWILIO_ACCOUNT_SID and self.TWILIO_AUTH_TOKEN
            ):
                raise ValueError(
                    "TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN are required for Twilio notifications"
                )
            if "sms" in twilio_channels and not self.TWILIO_SMS_FROM:
                raise ValueError("TWILIO_SMS_FROM is required for Twilio SMS")
            if "whatsapp" in twilio_channels and not self.TWILIO_WHATSAPP_FROM:
                raise ValueError("TWILIO_WHATSAPP_FROM is required for Twilio WhatsApp")
            if twilio_channels and self.NOTIFICATION_PUBLIC_BASE_URL is None:
                raise ValueError(
                    "NOTIFICATION_PUBLIC_BASE_URL is required for Twilio callbacks"
                )
            if (
                self.NOTIFICATION_PUBLIC_BASE_URL is not None
                and self.NOTIFICATION_PUBLIC_BASE_URL.scheme != "https"
            ):
                raise ValueError(
                    "NOTIFICATION_PUBLIC_BASE_URL must use HTTPS outside development"
                )
        self._check_default_secret("SECRET_KEY", self.SECRET_KEY)
        self._check_default_secret(
            "FIELD_ENCRYPTION_MASTER_KEY", self.FIELD_ENCRYPTION_MASTER_KEY
        )
        self._check_default_secret(
            "NOTIFICATION_WEBHOOK_SECRET", self.NOTIFICATION_WEBHOOK_SECRET
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


def external_observability_retention_capability(
    configuration: Settings,
) -> ExternalObservabilityRetentionCapability:
    """Return the fail-closed clinic-onboarding view of external retention."""

    windows = (
        configuration.EXTERNAL_PROXY_RETENTION_DAYS,
        configuration.EXTERNAL_CONTAINER_RETENTION_DAYS,
        configuration.EXTERNAL_APM_RETENTION_DAYS,
    )
    evidence = configuration.EXTERNAL_OBSERVABILITY_RETENTION_EVIDENCE
    evidence_id = configuration.EXTERNAL_OBSERVABILITY_RETENTION_EVIDENCE_ID.strip()
    reason_code: ExternalObservabilityRetentionReason | None = None
    if any(not 1 <= days <= 30 for days in windows):
        reason_code = "external_retention_window_invalid"
    elif evidence == "unqualified" or not evidence_id:
        reason_code = "external_retention_evidence_unqualified"
    elif configuration.FASTAPI_ENV != "development" and (
        evidence not in {"deployment_policy", "provider_contract"}
        or evidence_id.startswith("fixture:")
    ):
        reason_code = "external_retention_evidence_not_production_qualified"
    return ExternalObservabilityRetentionCapability(
        proxy_days=windows[0],
        container_days=windows[1],
        apm_days=windows[2],
        evidence=evidence,
        evidence_id=evidence_id,
        qualified=reason_code is None,
        reason_code=reason_code,
    )


settings = Settings()  # type: ignore
