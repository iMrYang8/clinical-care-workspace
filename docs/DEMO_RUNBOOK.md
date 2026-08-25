# Nightingale synthetic demo runbook

## 1. Start from a deterministic state

Requirements: Docker Desktop/Compose and free host ports 80 and 443.

```bash
./scripts/demo-up.sh
```

Open `https://localhost` and accept the generated local-certificate warning.
The local override explicitly enables development-only demo authentication and
synthetic fixtures. The production Compose selection does not seed them.

Scenarios B, C, D, and F create new immutable data. Before a recorded demo run,
reset only the current Compose project's containers and database volume:

```bash
NIGHTINGALE_RESET_CONFIRM=YES ./scripts/reset-demo.sh
```

Use the same `COMPOSE_PROJECT_NAME` for start and reset if the default
`nightingale` name is not used.

## 2. Scenario A — Glance to exact source

1. Choose **Care staff**.
2. Open **Alex Synthetic**.
3. Confirm Glance shows concise risk/follow-up cards and the Timeline shows the
   formal AI Doctor Consult, AI Nurse Consult, and AI Patient Session types.
4. On **Fall risk remains elevated**, select **View source**.
5. Confirm the immutable-source dialog highlights that exact text in **Current
   care review**, rather than navigating only to the note.

Evidence: `frontend/tests/scenarios.spec.ts`, `[Scenario A]` verifies the
resolved immutable version and highlighted span.

## 3. Scenario B — collaboration, versions, learning, audit

1. As **Care staff**, open Alex's care note.
2. Open **Comments** on **Current care review**. Show the seeded
   `@clinician` mention and assignment.
3. Open **Versions** on **Medication reconciliation**. Compare Version 1 and
   Version 2, then select **Revert as new version** on Version 1. The older
   version is not deleted; a new current version is created.
4. In Glance, show the medication item and its **Clinician accepted** reason.
5. Log out, choose **Clinic admin**, and open the metadata-only audit trail.
   Show the `entry.reverted` event and that clinical body text is absent.

The comment extension supplies only the selected-text `commentId` mark.
Nightingale owns persistence, quote/context anchors, immutable-version binding,
mentions, assignment, resolution, orphan state, and audit.

Evidence: `[Scenario B]`, plus backend collaboration, revision, importance, and
admin API tests.

## 4. Scenario C — dates, archive, and rehydrate

1. Choose **Clinician** and open Alex.
2. Show entries dated **2025-04-15** and **2026-02-06** in one Timeline.
3. The development fixture also contains a separate unprotected synthetic
   historical record eligible for cold storage. There is no archive operator
   control in the browser UI; `[Scenario C]` calls the authenticated API.
4. The test previews eligibility, archives the selected immutable version with
   development dry-run disabled, rehydrates it, and checks the SHA-256 plus the
   restored `warm` tier.

Versions, audit events, and provenance remain in PostgreSQL; cold storage moves
only the eligible encrypted payload. Production defaults to
`DATA_DECAY_DRY_RUN=true` until an operator opts in.

## 5. Scenario D — deterministic conflict and tenant boundary

This is clearest as the two-context Playwright scenario:

```bash
docker compose --project-name "${COMPOSE_PROJECT_NAME:-nightingale}" run --rm --build \
  -e CI=1 playwright bun run test:e2e --grep "Scenario D"
```

It performs all of the following against the running TLS application:

1. Two Care staff browser contexts read the same entry/version.
2. The first `PATCH` with `If-Match` wins.
3. The stale second `PATCH` returns `409 VERSION_CONFLICT`.
4. Updates to two different entries both succeed.
5. Clinic admin cannot read clinical content (`403`).
6. Staff in the other synthetic clinic cannot discover the entry (`404`).

## 6. Scenario E — patient-safe network and provider-off truth

1. Choose **Patient** and show **My Care · Alex Synthetic**.
2. The Patient view contains patient-facing Timeline/Glance data only. It does
   not request admin, AI, comment, decay, or job endpoints.
3. `[Scenario E]` captures every `/api/v1` response and asserts that clinical
   author IDs, raw AI, internal comments, critical/scoring internals, and
   risk-reason internals are absent. The authenticated `/auth/me` response does
   expose the caller's own clinic and membership identifiers.
4. It also verifies the browser sends no Authorization header, the session
   cookie is Secure/HttpOnly/SameSite=Lax, local storage contains no auth token,
   and logout removes the cookie.
5. The `/live` capability response is explicit:
   `LIVE_TRANSCRIPT_NOT_CONFIGURED`. No provisional caption is fabricated.

Remote text remains disabled in the default demo. Provider-contract and
redaction tests cover fail-closed handling; the UI does not imply that a live
model ran.

## 7. Scenario F — encrypted voice recovery and Review Mode

The Playwright path uses a browser MediaRecorder mock to create valid synthetic
WAV bytes, forces a network outage, reloads the page, resumes the encrypted
IndexedDB queue, finalizes the session, and enters Review Mode:

```bash
docker compose --project-name "${COMPOSE_PROJECT_NAME:-nightingale}" run --rm --build \
  -e CI=1 playwright bun run test:e2e --grep "Scenario F"
```

The synthetic transcript checkbox selects the fixed
`code-switch-overlap-v1` fixture. It exercises speaker, timestamp, language,
confidence, overlap, fact, and provenance UI; it is **not** speech recognition.
Ordinary audio with the default disabled ASR is retained encrypted and ends in
an explicit review/provider-disabled state.

Server tests separately cover multi-device sealing, missing/duplicate chunks,
FFmpeg limits, worker recovery, permission boundaries, transcript correction,
publication, and transcript-to-fact-to-audio provenance. The implementation
does not claim OS background upload after the application is closed, precise
cross-device clock synchronization, blind-source separation, or validated
pyannote diarization.

## 8. Run all browser scenarios

```bash
docker compose --project-name "${COMPOSE_PROJECT_NAME:-nightingale}" build playwright
docker compose --project-name "${COMPOSE_PROJECT_NAME:-nightingale}" run --rm \
  -e CI=1 playwright bun run test:e2e
```

For three clean consecutive rehearsals:

```bash
for round in 1 2 3; do
  echo "Demo round ${round}"
  NIGHTINGALE_RESET_CONFIRM=YES ./scripts/reset-demo.sh
  docker compose --project-name "${COMPOSE_PROJECT_NAME:-nightingale}" run --rm \
    -e CI=1 playwright bun run test:e2e
done
```

Do not count a round as passed from UI appearance alone. Preserve the
Playwright exit code/report for each run.

## 9. Release verification commands

The orchestrated check is:

```bash
./scripts/verify-release.sh
./scripts/verify-release.sh --e2e --benchmark --ffmpeg
```

The default verifies the frozen Python and Bun locks, backend Ruff/mypy/ty,
pytest with a 90% coverage gate, an Alembic downgrade/upgrade roundtrip,
frontend type/lint/unit/build, generated OpenAPI client synchronization, and
both development and production Compose rendering. It uses a temporary
`nightingale-verify-*` Compose project for PostgreSQL tests and removes only
that temporary project's containers/volume on exit.

Useful focused commands:

```bash
uv lock --check
uv sync --frozen --package app
cd backend
uv run --frozen ruff check app tests
uv run --frozen ruff format app tests --check
uv run --frozen mypy app
uv run --frozen ty check app

cd ../frontend
bun run typecheck
bun run lint
bun run test
bun run build

cd ..
BUN_BIN="$(command -v bun)" ./scripts/generate-client.sh
git diff --exit-code -- frontend/openapi.json frontend/src/client
docker compose config --quiet
DOMAIN=nightingale.invalid \
  docker compose -f compose.yml -f compose.deploy.yml config --quiet
```

Glance performance:

```bash
uv run --frozen --package app python scripts/benchmark_glance.py --insecure
```

The benchmark authenticates, warms the precomputed endpoint, verifies the
response source is `precomputed`, records hardware/commit/sample counts, and
fails if warm p95 exceeds 300 ms. It does not measure a model call.

Container FFmpeg evidence:

```bash
./scripts/capture_ffmpeg_inventory.sh
cat docs/evidence/ffmpeg-container-version.txt
```

If the generator has not run against the release image, record the container
FFmpeg build as **not tested** rather than copying host output.

## 10. Demo teardown

Stop containers while preserving the demo database:

```bash
docker compose --project-name "${COMPOSE_PROJECT_NAME:-nightingale}" down
```

Use `reset-demo.sh` when the project database volume must also be recreated;
it has an explicit confirmation and never performs host filesystem or disk
cleanup.
