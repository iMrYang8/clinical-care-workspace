# Nightingale - Docker Compose Deployment

You can deploy the project to your own remote server with Docker Compose. The deployment configuration includes Traefik to handle HTTPS and route incoming traffic to the application.

## Preparation

* Have a remote server ready and available.
* Configure a DNS record pointing to the server for the application domain, such as `fastapi-project.example.com`.
* Install and configure [Docker](https://docs.docker.com/engine/install/) on the remote server (Docker Engine, not Docker Desktop).

## Copy the Code

```bash
rsync -av --exclude=".git/" --filter=":- .gitignore" ./ root@your-server.example.com:/root/code/app/
```

The `--filter=":- .gitignore"` option tells `rsync` to use the same ignore rules as Git, excluding files such as the Python virtual environment.

## Configure the Application

### Environment Variables

Set the application domain and project name:

```bash
export DOMAIN=fastapi-project.example.com
export PROJECT_NAME="Nightingale"
```

You can also configure these environment variables as needed:

* `SMTP_HOST`: The SMTP server host from your email provider.
* `SMTP_USER`: The SMTP server user.
* `EMAILS_FROM_EMAIL`: The email account used to send emails.
* `SENTRY_DSN`: The DSN for Sentry.

### Secrets

Generate and set secure values for the database passwords, token signing key,
and independent field-encryption key:

```bash
export POSTGRES_PASSWORD="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
export POSTGRES_APP_PASSWORD="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
export SECRET_KEY="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
export FIELD_ENCRYPTION_MASTER_KEY="$(python -c 'import secrets; print(secrets.token_hex(32))')"
```

`FIELD_ENCRYPTION_MASTER_KEY` is an independent persisted 32-byte key. Back it
up separately and do not rotate it as part of JWT `SECRET_KEY` rotation; losing
it makes existing clinical ciphertext unreadable.

The default image is model-free and uses the deterministic provider. To enable
remote OpenAI text egress, build the locked local Presidio profile and configure
all provider values explicitly:

```bash
export INSTALL_PRESIDIO_NLP=true
export PRESIDIO_NLP_MODEL=en_core_web_sm
export AI_PROVIDER=openai
export REMOTE_TEXT_EGRESS_ENABLED=true
export OPENAI_API_KEY="$(security find-generic-password -w -s NIGHTINGALE_OPENAI_KEY)"
export OPENAI_EXTRACT_MODEL="YOUR_CONFIGURED_MODEL_ID"
export OPENAI_REVIEW_MODEL="YOUR_CONFIGURED_REVIEW_MODEL_ID"
```

No model ID is hard-coded as an external API contract. A production API or
worker configured for remote egress exits at startup if the Presidio model is
not installed and loadable. Keep the provider deterministic if this profile is
not built.

To use an authenticated email provider, also set `SMTP_PASSWORD`.

## Deploy

```bash
cd /root/code/app/
export NIGHTINGALE_SOURCE_COMMIT="$(git rev-parse HEAD)"
release_evidence="$(mktemp -d /var/tmp/nightingale-release.XXXXXXXX)"
NIGHTINGALE_RELEASE_EVIDENCE_DIR="$release_evidence" \
  ./scripts/verify-release.sh --e2e --benchmark --ffmpeg
export NIGHTINGALE_BACKEND_IMAGE="$(cat "$release_evidence/verified-backend-image-id.txt")"
test "$(docker image inspect --format '{{.Id}}' "$NIGHTINGALE_BACKEND_IMAGE")" = \
  "$NIGHTINGALE_BACKEND_IMAGE"
docker compose -f compose.yml -f compose.deploy.yml run --rm prestart
docker compose -f compose.yml -f compose.deploy.yml \
  up -d --wait --wait-timeout 180 proxy backend ai-worker
test "$(curl --fail --show-error --silent \
  "https://${DOMAIN}/api/v1/utils/health-check/")" = "true"
docker compose -f compose.yml -f compose.deploy.yml exec -T ai-worker \
  python -c "import app.ai_worker; from app.core.db import assert_restricted_runtime_database; assert_restricted_runtime_database()"
```

The `compose.deploy.yml` file adds HTTPS and automatic certificate handling to the shared `compose.yml` configuration. Explicitly listing both files excludes the local settings from `compose.override.yml`.

The production overlay requires `NIGHTINGALE_BACKEND_IMAGE`. The full gate
writes the exact content-addressed ID that passed Scenario A-F, benchmark,
FFmpeg, and production-topology smoke to
`verified-backend-image-id.txt`. Use that ID directly and do not rebuild between
verification and migration. The migration owner, API, and worker therefore use
identical content even if another checkout builds on the same Docker daemon.

The backend Docker image builds the frontend, so the server does not need Bun or prebuilt frontend files.

Production prestart runs migrations and role bootstrap but deliberately does
not seed demo data. Provision the first clinic explicitly after prestart. The
command is idempotent for identical inputs and prints the clinic ID required by
clinic-scoped login requests:

```bash
export NIGHTINGALE_PROVISION_CLINIC_SLUG=YOUR_CLINIC_SLUG
export NIGHTINGALE_PROVISION_CLINIC_NAME="YOUR_CLINIC_NAME"
export NIGHTINGALE_PROVISION_ADMIN_EMAIL=ADMIN_EMAIL
export NIGHTINGALE_PROVISION_ADMIN_PASSWORD="$(security find-generic-password -w -s NIGHTINGALE_ADMIN_PASSWORD)"
export NIGHTINGALE_PROVISION_WORKER_EMAIL=WORKER_EMAIL
docker compose -f compose.yml -f compose.deploy.yml run --rm \
  -e NIGHTINGALE_PROVISION_CLINIC_SLUG \
  -e NIGHTINGALE_PROVISION_CLINIC_NAME \
  -e NIGHTINGALE_PROVISION_ADMIN_EMAIL \
  -e NIGHTINGALE_PROVISION_ADMIN_PASSWORD \
  -e NIGHTINGALE_PROVISION_WORKER_EMAIL \
  prestart bash scripts/provision-clinic-admin.sh
```

Record the printed `clinic_id`, open `https://${DOMAIN}/login`, and use the
**Clinic account** form with that ID, `NIGHTINGALE_PROVISION_ADMIN_EMAIL`, and
the password supplied through `NIGHTINGALE_PROVISION_ADMIN_PASSWORD`. The
production API rejects the development persona buttons; the password form sets
the secure HttpOnly browser cookie and does not persist a token in browser
storage.

The owner URL exists only in that one-shot container. The backend and
`ai-worker` receive only the restricted `nightingale_app` URL and verify it is
`NOBYPASSRLS`, non-owner, and non-superuser before serving work.

## Deploy with GitHub Actions

The included `.github/workflows/deploy-docker-compose.yml` workflow runs the
deployment commands on the server when manually triggered from GitHub Actions.
Its hosted gate saves the exact verified image as an integrity-hashed OCI
artifact; the protected self-hosted job loads and verifies that artifact rather
than rebuilding from mutable base images.

Use a self-hosted runner only for a repository whose contributors and workflow code you trust. GitHub recommends using self-hosted runners with private repositories because workflows execute directly on the runner machine.

### Configure Repository Variables and Secrets

In the repository, go to **Settings** > **Secrets and variables** > **Actions** and add these repository variables:

* `DOMAIN`
* `PROJECT_NAME`

To enable emails, add these optional repository variables:

* `SMTP_HOST`
* `SMTP_USER`
* `EMAILS_FROM_EMAIL`

To enable Sentry, add the optional `SENTRY_DSN` repository variable.

For remote text egress, add `INSTALL_PRESIDIO_NLP=true`,
`PRESIDIO_NLP_MODEL=en_core_web_sm`, `AI_PROVIDER=openai`,
`REMOTE_TEXT_EGRESS_ENABLED=true`, `OPENAI_EXTRACT_MODEL`, and
`OPENAI_REVIEW_MODEL` as repository variables. Add `OPENAI_API_KEY` as a
repository secret. The default/blank values keep the provider deterministic
and omit the NLP model.

Add these repository secrets:

* `POSTGRES_PASSWORD`
* `POSTGRES_APP_PASSWORD` (an independent runtime-role password)
* `SECRET_KEY`
* `FIELD_ENCRYPTION_MASTER_KEY` (an independent persisted 32-byte hex key)
* `OPENAI_API_KEY` (only when remote text egress is enabled)

To use an authenticated email provider, add the optional `SMTP_PASSWORD` repository secret.

### Install a Self-Hosted Runner

On the server, create a dedicated user and grant it access to Docker:

```bash
sudo adduser github
sudo usermod -aG docker github
sudo su - github
```

In the GitHub repository, go to **Settings** > **Actions** > **Runners**, select **New self-hosted runner**, choose Linux, and follow the commands GitHub provides to download, configure, and register the runner. Install it in `/home/github/actions-runner`.

After registering the runner, exit the `github` user session and install the runner as a system service:

```bash
exit
cd /home/github/actions-runner
sudo ./svc.sh install github
sudo ./svc.sh start
sudo ./svc.sh status
```

See GitHub's guides for [adding a self-hosted runner](https://docs.github.com/en/actions/how-tos/manage-runners/self-hosted-runners/add-runners) and [configuring the runner as a service](https://docs.github.com/en/actions/how-tos/manage-runners/self-hosted-runners/configure-the-application?platform=linux).

### Run the Deployment

When the runner is online, open the repository's **Actions** tab, select **Deploy with Docker Compose**, and select **Run workflow**.

## URLs

Replace `fastapi-project.example.com` with your domain.

Application (frontend and API): `https://fastapi-project.example.com`

Interactive API docs: `https://fastapi-project.example.com/docs`

No database administration UI is included in the production Compose files.
