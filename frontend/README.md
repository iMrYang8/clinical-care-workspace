# Nightingale frontend

The frontend is React, TypeScript, Vite, TanStack Router/Query, shadcn/ui, and
Tiptap. It is built into FastAPI and served from the same HTTPS origin in the
supported development and production paths.

## Integrated development

From the repository root:

```bash
./scripts/demo-up.sh
```

Open `https://localhost`. Browser login uses only the same-origin Secure,
HttpOnly, SameSite=Lax session cookie. Frontend code must not persist a bearer
token in local storage, IndexedDB, a query string, or an SSE URL. Logout clears
TanStack Query state and the local encrypted voice-upload queue.

A standalone Vite server can be used for visual iteration, but it is not the
cookie/TLS acceptance environment. Run authentication and end-to-end checks
against the Compose HTTPS application.

## Commands

Use the repository-pinned Bun executable on local machines and CI:

```bash
bun run --filter frontend typecheck
bun run --filter frontend lint
bun run --filter frontend test
bun run --filter frontend build
```

Playwright Scenario A-F is run through Compose so Chromium sees the same TLS,
proxy, cookie, API, and worker boundaries as the demo:

```bash
docker compose run --rm -e CI=1 playwright bun run test:e2e
```

## Generated API client

`openapi.json` and `src/client/` are generated artifacts. After a backend schema
change, run from the repository root:

```bash
BUN_BIN="$(command -v bun)" ./scripts/generate-client.sh
git diff --exit-code -- frontend/openapi.json frontend/src/client
```

CI performs the same regeneration and rejects drift. Application code should
use the generated types where practical, but patient screens must still call
only patient-safe endpoints and DTOs. Playwright recursively inspects those
network responses for internal comments, raw AI, and scoring fields.

## Structure

- `src/routes/`: role-aware screens and navigation
- `src/components/`: Timeline, Glance, editor, collaboration, admin, and voice UI
- `src/features/voice/`: encrypted chunk queue and voice API integration
- `src/client/`: generated OpenAPI client
- `tests/`: Scenario A-F and core Playwright acceptance tests
