#!/usr/bin/env bash

set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
env_file="$root/.env"

if [[ "${CI:-}" != "true" ]]; then
  echo "setup-ci-env.sh only creates credentials inside CI=true jobs." >&2
  exit 2
fi
if [[ -e "$env_file" ]]; then
  echo "Refusing to overwrite existing $env_file" >&2
  exit 2
fi
if ! command -v openssl >/dev/null 2>&1; then
  echo "openssl is required to generate ephemeral CI credentials." >&2
  exit 1
fi

# Compose and direct FastAPI imports both read the ignored root .env file. Keep
# the release artifact free of credentials by creating fresh, URL-safe values
# for every CI job and never printing them to the workflow log.
umask 077
tmp_file="$(mktemp "${TMPDIR:-/tmp}/nightingale-ci-env.XXXXXX")"
cleanup() {
  rm -f "$tmp_file"
}
trap cleanup EXIT INT TERM

secret_key="$(openssl rand -hex 32)"
field_key="$(openssl rand -base64 32 | tr -d '\n')"
notification_webhook_secret="$(openssl rand -hex 32)"
postgres_password="$(openssl rand -hex 24)"
postgres_app_password="$(openssl rand -hex 24)"
first_superuser_password="$(openssl rand -hex 24)"
install_presidio_nlp="${INSTALL_PRESIDIO_NLP:-false}"
case "$install_presidio_nlp" in
  true | false) ;;
  *)
    echo "INSTALL_PRESIDIO_NLP must be true or false in CI." >&2
    exit 2
    ;;
esac

cat >"$tmp_file" <<EOF
FASTAPI_ENV=development
ENABLE_DEMO_AUTH=true

PROJECT_NAME=Nightingale
FRONTEND_HOST=https://localhost
BROWSER_TRUSTED_ORIGINS=https://localhost

SECRET_KEY=$secret_key
FIELD_ENCRYPTION_MASTER_KEY=$field_key
NOTIFICATION_WEBHOOK_SECRET=$notification_webhook_secret
FIRST_SUPERUSER=ci-admin@example.com
FIRST_SUPERUSER_PASSWORD=$first_superuser_password

SMTP_HOST=localhost
EMAILS_FROM_EMAIL=ci@example.com
SMTP_TLS=false
SMTP_PORT=1025

POSTGRES_PASSWORD=$postgres_password
POSTGRES_APP_PASSWORD=$postgres_app_password
DATABASE_URL=postgresql+psycopg://nightingale_app:$postgres_app_password@db:5432/app
MIGRATION_DATABASE_URL=postgresql+psycopg://postgres:$postgres_password@db:5432/app

AI_PROVIDER=deterministic
VOICE_TRANSCRIPTION_PROVIDER=disabled
REMOTE_AUDIO_EGRESS_ENABLED=false
STRICT_NO_AUDIO_EGRESS=false
REMOTE_TEXT_EGRESS_ENABLED=false
LIVE_TRANSCRIPT_ENABLED=false
LIVE_TRANSCRIPT_PROVIDER=disabled
PRESIDIO_REQUIRED=true
PRESIDIO_NLP_MODEL=en_core_web_sm
INSTALL_PRESIDIO_NLP=$install_presidio_nlp
INSTALL_LOCAL_ASR=false
INSTALL_DIARIZATION=false

# CI's production-Compose rendering uses an inspectable synthetic policy ID;
# development Compose overrides this with deterministic_fixture explicitly.
EXTERNAL_PROXY_RETENTION_DAYS=30
EXTERNAL_CONTAINER_RETENTION_DAYS=30
EXTERNAL_APM_RETENTION_DAYS=30
EXTERNAL_OBSERVABILITY_RETENTION_EVIDENCE=deployment_policy
EXTERNAL_OBSERVABILITY_RETENTION_EVIDENCE_ID=policy:nightingale-ci-external-observability-30d

IMPORTANCE_LEARNING_ENABLED=true
DATA_DECAY_ENABLED=true
DATA_DECAY_DRY_RUN=true
EOF

mv "$tmp_file" "$env_file"
chmod 600 "$env_file"
trap - EXIT INT TERM
echo "Created ephemeral CI environment at $env_file"
