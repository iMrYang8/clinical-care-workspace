# Nightingale - Deployment

Deploy the project to [FastAPI Cloud](https://fastapicloud.com) with the included GitHub Actions workflow.

## Create the FastAPI Cloud Application

Create an application in FastAPI Cloud and set its [Application Directory](https://fastapicloud.com/docs/builds-and-deployments/application-directory/) to `backend`.

Connect a PostgreSQL database using the [Neon](https://fastapicloud.com/docs/integrations/neon-integration/) or [Supabase](https://fastapicloud.com/docs/integrations/supabase-integration/) integration. Both integrations configure a `DATABASE_URL` secret automatically. You can also configure `DATABASE_URL` manually for another PostgreSQL provider.

## Configure the Application

### Environment Variables

Add these required [environment variables](https://fastapicloud.com/docs/builds-and-deployments/environment-variables/) to the FastAPI Cloud application:

* `PROJECT_NAME`: The name of the project, used in the API documentation and emails.
* `FRONTEND_HOST`: The public URL of the application, such as the generated `https://your-app.fastapicloud.dev` URL or a custom domain.

To enable emails, add these optional environment variables with values from your email provider:

* `SMTP_HOST`
* `SMTP_USER`
* `EMAILS_FROM_EMAIL`

To enable Sentry, configure `SENTRY_DSN`.

### Secrets

Add these required values and mark them as secrets:

* `SECRET_KEY`: A secret key used to sign security tokens.
* `DATABASE_URL`: the restricted `nightingale_app` runtime connection URL.
* `MIGRATION_DATABASE_URL`: an independent owner URL used only by prestart.
* `POSTGRES_APP_PASSWORD`: a generated password used by prestart to create or
  rotate the restricted runtime role.
* `FIELD_ENCRYPTION_MASTER_KEY`: an independent persisted 32-byte hex key used
  for clinical field encryption. Back it up separately; JWT key rotation must
  not rotate this key.

To enable emails with an authenticated provider, add `SMTP_PASSWORD` as a secret.

You can generate a secure value for `SECRET_KEY` with:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Generate the field encryption key separately:

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

## Configure Continuous Deployment

The included `.github/workflows/deploy.yml` workflow builds the frontend, prepares the database, and deploys the application whenever changes are pushed to `master`. You can also run it manually from the **Actions** tab.

Log in to FastAPI Cloud and configure the [deploy token](https://fastapicloud.com/docs/advanced-features/deploy-tokens/) and application ID as GitHub repository secrets:

```bash
uv run fastapi login
uv run fastapi cloud setup-ci --secrets-only --app-id <your-app-id>
```

If the GitHub CLI is installed and authenticated, the command configures `FASTAPI_CLOUD_TOKEN` and `FASTAPI_CLOUD_APP_ID` automatically. Otherwise, it prints the values so you can add them in your repository under **Settings** > **Secrets and variables** > **Actions**.

The workflow runs database migrations and configures the restricted runtime
role before deploying. In the repository's **Settings** > **Secrets and
variables** > **Actions** page, add these repository variables:

* `PROJECT_NAME`

Add these repository secrets (the workflow fails before reading `.env` if any
database boundary secret is missing):

* `DATABASE_URL`
* `MIGRATION_DATABASE_URL`
* `POSTGRES_APP_PASSWORD`
* `SECRET_KEY`
* `FIELD_ENCRYPTION_MASTER_KEY`

Use the restricted URL in the application and the owner URL only in GitHub
Actions. The database must be reachable from GitHub-hosted runners. Remote AI
jobs additionally require a separately deployed long-running
`python -m app.ai_worker` process; otherwise keep `AI_PROVIDER=deterministic`.

FastAPI Cloud's default build does not install the optional
`presidio-nlp` dependency group. Remote OpenAI text egress therefore remains
disabled on this default path. A remote worker image must install the locked
group, set `PRESIDIO_NLP_MODEL=en_core_web_sm`, and pass the same independent
field key; API/worker startup fails closed when remote egress is enabled but the
model cannot load. The Docker Compose guide documents that build profile.

The deployment workflow performs these steps:

1. Installs and builds the frontend into `backend/app/frontend`.
2. Runs `backend/scripts/prestart.sh` to apply database migrations and configure
   the restricted runtime role. Production does not seed demo users.
3. Deploys the project with `uv run fastapi deploy`.

Provision the first production clinic/Admin/Worker explicitly with an owner
connection after migrations:

```bash
export MIGRATION_DATABASE_URL='OWNER_POSTGRES_URL'
export NIGHTINGALE_PROVISION_CLINIC_SLUG=YOUR_CLINIC_SLUG
export NIGHTINGALE_PROVISION_CLINIC_NAME='YOUR_CLINIC_NAME'
export NIGHTINGALE_PROVISION_ADMIN_EMAIL=ADMIN_EMAIL
export NIGHTINGALE_PROVISION_ADMIN_PASSWORD='ADMIN_PASSWORD_FROM_SECRET_STORE'
export NIGHTINGALE_PROVISION_WORKER_EMAIL=WORKER_EMAIL
cd backend
uv run --frozen bash scripts/provision-clinic-admin.sh
```

The operation is idempotent for identical values and prints the clinic ID. The
password is read only from the environment, never a CLI argument. Run it from a
trusted one-shot environment and remove the password variable afterward.

## URLs

Replace `your-app.fastapicloud.dev` with the URL of your FastAPI Cloud application.

Application (frontend and API): `https://your-app.fastapicloud.dev`

Interactive API docs: `https://your-app.fastapicloud.dev/docs`

## Docker Compose

For deployment to your own server, see the [Docker Compose deployment guide](./deployment-docker-compose.md).

## GitHub Repository Automation

Install the following GitHub Apps to enable the included repository automation:

* [Latest Changes](https://github.com/apps/latest-changes) updates `release-notes.md` when a pull request is merged.
* [PR Push](https://github.com/apps/pr-push) lets the pre-commit workflow push automated fixes to pull request branches.
* [PR Submit](https://github.com/apps/pr-submit) lets the **Bump pre-commit hooks** and **Prepare Release** workflows create pull requests.

To publish code coverage with [Smokeshow](https://github.com/samuelcolvin/smokeshow), add `SMOKESHOW_AUTH_KEY` as a repository secret.
