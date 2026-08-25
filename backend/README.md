# Nightingale backend

The backend is FastAPI + SQLModel + PostgreSQL. It enforces clinic-scoped
memberships, role permissions, row-level security, immutable entry versions,
metadata-only audit, encrypted clinical fields, deterministic and optional AI
providers, data-decay archives, and the voice-review state machine.

Use the repository's HTTPS Compose path for an integrated environment:

```bash
./scripts/demo-up.sh
```

The API is available through `https://localhost/api/v1`; the backend container
port is not published by default. For host-side work, opt in to the loopback
PostgreSQL port described in [`../development.md`](../development.md).

## Static and test gates

From the repository root, the complete gate is:

```bash
./scripts/verify-release.sh
```

Focused backend commands, run from this directory with an initialized test
database, are:

```bash
uv run --frozen ruff check app tests
uv run --frozen ruff format app tests --check
uv run --frozen mypy app
uv run --frozen ty check app
uv run --frozen coverage run -m pytest tests
uv run --frozen coverage report --fail-under=90
```

Tests reset only their configured PostgreSQL database and use synthetic
fixtures. Never point the test environment at production or real patient data.

## Migrations

Models live in `app/models.py` and migrations in `app/alembic/versions/`.
Generate additive changes with:

```bash
uv run --frozen alembic revision --autogenerate -m "describe change"
uv run --frozen alembic upgrade head
uv run --frozen alembic check
```

The historical imported migrations are intentionally retained so a clean
checkout can reproduce the baseline before Nightingale migrations remove the
example schema. Runtime requests use the restricted `nightingale_app` role;
only provisioning and migration commands receive `MIGRATION_DATABASE_URL`.

## API and worker boundaries

- Routes are under `app/api/routes/`; authorization comes from the current
  server-side membership, not request-supplied role, actor, or clinic values.
- Browser auth is a Secure/HttpOnly/SameSite cookie with Origin checks for
  cookie-authenticated mutations. Bearer support remains for non-browser API
  and worker compatibility.
- AI jobs and voice jobs are durable; external providers remain disabled unless
  every capability/egress/redaction setting is present.
- The deterministic provider and synthetic voice fixture are test/demo tools,
  not evidence of model or ASR quality.

See [`../MODEL_INVENTORY.md`](../MODEL_INVENTORY.md) and
[`../docs/VOICE_PIPELINE.md`](../docs/VOICE_PIPELINE.md) for exact capability
states and evidence boundaries.
