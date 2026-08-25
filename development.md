# Nightingale development

## Secure default stack

The supported development path is the same-origin HTTPS Compose stack:

```bash
./scripts/demo-up.sh
```

Open `https://localhost`. The generated certificate is intentionally local, so
accept the browser warning once. HTTP on port 80 redirects to HTTPS; PostgreSQL,
the FastAPI container port, Mailpit, Adminer, and the Traefik dashboard are not
published by the default stack.

The local override enables only synthetic demo authentication and deterministic
fixtures. The explicit production file never seeds demo data:

```bash
DOMAIN=nightingale.invalid NIGHTINGALE_BACKEND_IMAGE=nightingale-backend:config-check \
  docker compose -f compose.yml -f compose.deploy.yml config --quiet
```

## Source development

Install the frozen dependencies from the repository root:

```bash
uv sync --frozen --package app
bun install --frozen-lockfile
```

For host-side backend tests or debugging, publish supporting services only via
the opt-in development-tools file, bound to loopback:

```bash
DEV_DB_PORT=55432 docker compose \
  -f compose.yml -f compose.override.yml -f compose.dev-tools.yml \
  up -d db mailpit
```

Set `DATABASE_URL` and `MIGRATION_DATABASE_URL` to the selected loopback port,
then run `backend/scripts/prestart.sh` and the FastAPI development server from
`backend/`. A Vite-only host server is useful for UI iteration, but it is not
the cookie/TLS release boundary; use the Compose application for authentication
and browser acceptance tests.

## Optional development tools

The following ports exist only when `compose.dev-tools.yml` is selected:

- PostgreSQL: `127.0.0.1:${DEV_DB_PORT:-5432}`
- Traefik dashboard: `127.0.0.1:8090`
- Adminer: `127.0.0.1:8080`
- Mailpit: `127.0.0.1:8025`

Do not add that file to production deployment commands.

## Verification

```bash
./scripts/verify-release.sh
./scripts/verify-release.sh --e2e --benchmark --ffmpeg
```

The default gate runs backend static analysis, PostgreSQL tests with coverage,
an Alembic downgrade/upgrade/check, frontend type/lint/unit/build, generated
OpenAPI synchronization, and Compose rendering. See
[`docs/DEMO_RUNBOOK.md`](./docs/DEMO_RUNBOOK.md) for focused commands and
Scenario A-F rehearsal.

## Migrations and generated clients

Create additive Alembic revisions from `backend/`; do not rewrite the imported
baseline history. After any API schema change, regenerate the checked-in client:

```bash
BUN_BIN="$(command -v bun)" ./scripts/generate-client.sh
git diff -- frontend/openapi.json frontend/src/client
```

## Local reset

Use the project-scoped reset helper when deterministic fixtures must be
recreated:

```bash
./scripts/reset-demo.sh
```

It asks for the Compose project name before removing only that project's
containers and named volumes. It does not perform host filesystem cleanup.
