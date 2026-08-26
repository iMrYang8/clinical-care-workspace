# Nightingale synthetic demo runbook

## 1. Start from a deterministic state

Requirements: Docker Desktop/Compose and free host ports 80 and 443.

```bash
./scripts/demo-up.sh
```

Open `https://localhost` and accept the generated local-certificate warning.
The local override explicitly enables development-only demo authentication and
synthetic fixtures. Ports 80/443 bind only to `127.0.0.1`, so fixed personas
are not exposed to the LAN. The production Compose selection binds public
HTTP/HTTPS explicitly and does not seed synthetic personas.

Scenarios B, C, D, and F create new immutable data. Before a recorded demo run,
reset only the current Compose project's containers and database volume:

```bash
RESET_NIGHTINGALE_LOCAL_DEMO="$(./scripts/demo-project-name.sh --fingerprint)" \
  ./scripts/reset-demo.sh
```

`demo-project-name.sh` hashes this checkout's canonical path. Start/reset reject
any unrelated project override, and reset validates existing container working
directory/config-file labels before touching its path-bound volume.

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

The browser suite also performs the full invitation path: an Admin sends a
care-team invitation through Mailpit, the recipient opens the public
`/accept-invitation` form with the code in a URL fragment (never a query),
verifies the invited email, chooses a password, and appears as an active member.
Patient onboarding is deliberately absent from this form because it requires a
separate patient-record link.

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
docker compose --project-name "$(./scripts/demo-project-name.sh)" run --rm --no-deps --build \
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

The default Scenario E remains provider-off. An optional development-only
synthetic live-caption run may set `LIVE_TRANSCRIPT_ENABLED=true` and
`LIVE_TRANSCRIPT_PROVIDER=deterministic`, but only for a session explicitly
created with the synthetic fixture. That run demonstrates the WebSocket/UI
contract; it is not an OpenAI call or ASR-quality evidence.

Remote text remains disabled in the default demo. Provider-contract and
redaction tests cover fail-closed handling; the UI does not imply that a live
model ran.

## 7. Scenario F — encrypted voice recovery and Review Mode

The Playwright path uses a browser MediaRecorder mock to create valid synthetic
WAV bytes, forces a network outage, reloads the page, resumes the encrypted
IndexedDB queue, finalizes the session, and enters Review Mode:

```bash
docker compose --project-name "$(./scripts/demo-project-name.sh)" run --rm --no-deps --build \
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
docker compose --project-name "$(./scripts/demo-project-name.sh)" build playwright
docker compose --project-name "$(./scripts/demo-project-name.sh)" run --rm --no-deps \
  -e CI=1 playwright bun run test:e2e
```

For three clean consecutive rehearsals:

```bash
for round in 1 2 3; do
  echo "Demo round ${round}"
  RESET_NIGHTINGALE_LOCAL_DEMO="$(./scripts/demo-project-name.sh --fingerprint)" \
    ./scripts/reset-demo.sh
  docker compose --project-name "$(./scripts/demo-project-name.sh)" run --rm --no-deps \
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

The second command is the mandatory pre-deployment gate in the protected Docker
Compose workflow. It runs Scenario A-F with three repetitions, checks the local
TLS route and durable worker process, benchmarks the exact running backend
image, captures FFmpeg from it, and then boots that content-addressed image
without rebuilding under the production Compose selection. The production
runtime check requires production mode, demo auth off, zero auto-seeded clinics,
HTTPS redirect/routing, and restricted worker database access. Set
`NIGHTINGALE_RELEASE_EVIDENCE_DIR` to a directory outside the worktree when the
evidence must be preserved as a CI artifact.

The default verifies the frozen Python and Bun locks, backend Ruff/mypy/ty,
pytest with a 90% coverage gate, an Alembic downgrade/upgrade roundtrip,
frontend type/lint/unit/build, tracked OpenAPI schema/client synchronization, and
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
git ls-files --error-unmatch frontend/openapi.json
git diff --exit-code -- frontend/openapi.json frontend/src/client
docker compose config --quiet
DOMAIN=nightingale.invalid NIGHTINGALE_BACKEND_IMAGE=nightingale-backend:config-check \
  docker compose -f compose.yml -f compose.deploy.yml config --quiet
```

Glance performance:

```bash
uv run --frozen --package app python scripts/benchmark_glance.py \
  --base-url https://localhost --insecure \
  --compose-project "$(./scripts/demo-project-name.sh)"
```

The benchmark uniquely resolves **Alex Synthetic** rather than trusting list
order, requires the expected four non-empty cards on every read, and records
the fixture patient ID plus observed card count. It requires the running
backend OCI revision to equal checkout `HEAD`, warms the precomputed endpoint,
records hardware/commit/sample counts, and fails if warm p95 exceeds 300 ms.
It does not measure a model call. A checked-in record from an older commit is
historical evidence only and cannot satisfy a current release gate.

## Production migration/deploy ordering

Only the supported Docker Compose workflow owns the literal `production-main`
concurrency group with `cancel-in-progress: false`; one protected release
finishes before the next can migrate or replace it. It skips non-main refs,
runs the complete live gates without production secrets, saves the exact
content-addressed image that passed them as an integrity-hashed OCI artifact,
and binds approval to that artifact and SHA. The protected runner loads and
verifies the artifact instead of rebuilding, then uses `--wait` and checks
public HTTPS, the worker command, restricted database access, and running image
IDs. The FastAPI Cloud workflow is an unprivileged verification-only check with
no production environment, secrets, or concurrency lock because that
single-service path does not provision the required durable
`python -m app.ai_worker` process. Repository operators must add required
reviewers and a main-only deployment-branch rule to the GitHub `production`
environment; YAML cannot configure those repository settings.
The `f9127d3b4c50` entry-metadata migration is an expand
step: existing rows use trusted backfill, while old API binaries may still
insert using `legacy_review_required` and `CURRENT_TIMESTAMP` server defaults.
New code supplies formal metadata. Remove those compatibility defaults only in
a later contract migration after every old API process has been retired.

Container FFmpeg evidence:

```bash
./scripts/capture_ffmpeg_inventory.sh
cat docs/evidence/ffmpeg-container-version.txt
```

If the generator has not run against the release image, record the container
FFmpeg build as **not tested** rather than copying host output.

## 10. Record the silent synthetic walkthrough

Requirements: Bun, Docker Desktop/Compose, host FFmpeg/ffprobe, and the local
ability to use Playwright's browser cache/download on the first run.

```bash
BUN_BIN="$(command -v bun)" ./scripts/record-demo.sh
```

The command performs a path-bound reset through `reset-demo.sh` (which starts
the application through `demo-up.sh`), installs only the frozen Bun lock, and
installs the matching Playwright Chromium before using `scripts/record_demo.ts`
to drive the real UI over self-signed local TLS.
It records a 1280×720 silent video and converts Playwright's temporary recording
with host FFmpeg to H.264/yuv420p with fast-start metadata:

```text
output/demo/Nightingale_Demo.mp4
```

The walkthrough logs in as Staff, resolves Alex Synthetic's Glance card to the
exact immutable source, opens a version diff, changes to Patient and shows the
approved-only My Care source, then changes to Clinician and completes the local
synthetic voice fixture into Review Mode. The script mocks only browser capture
hardware with deterministic WAV bytes, exactly as Scenario F does; it does not
stub API responses or insert fabricated browser-only clinical data. Scene
labels and focus rings are temporary DOM annotations used only for recording.
The output intentionally has no audio track.

To inspect the encoded deliverable and sample four frames:

```bash
ffprobe -v error \
  -show_entries stream=codec_name,width,height:format=duration,size \
  -of default=noprint_wrappers=1 output/demo/Nightingale_Demo.mp4
mkdir -p /tmp/nightingale-demo-frames
ffmpeg -y -i output/demo/Nightingale_Demo.mp4 \
  -vf "select='eq(n,150)+eq(n,600)+eq(n,1050)+eq(n,1500)'" -vsync 0 \
  /tmp/nightingale-demo-frames/frame-%02d.png
```

Set `NIGHTINGALE_RECORD_KEEP_STATE=1` to skip the path-bound database reset and
record an already-running synthetic state. Reproducible delivery uses the
default reset. If recording is interrupted, no other checkout/project is
touched and the Playwright scratch video remains in the operating-system temp
directory only.

## 11. Demo teardown

Stop containers while preserving the demo database:

```bash
docker compose --project-name "$(./scripts/demo-project-name.sh)" down
```

Use `reset-demo.sh` when the verified path-bound local database volume must also
be recreated. It requires an exact checkout fingerprint, validates Compose
labels/config files, and never performs host filesystem or disk cleanup.
