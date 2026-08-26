# Nightingale

Nightingale is a clinic-scoped, synthetic-data care-note demo built for a
72-hour product exercise. It makes the path from a Glance card to an immutable
source visible, while keeping editing, comments, AI-derived material, voice
review, audit metadata, and patient-facing responses behind server-enforced
role and tenant boundaries.

The default checkout is an **offline deterministic demo**. It does not need an
OpenAI key, a Hugging Face token, or model weights, and it never represents its
synthetic transcript fixture as speech recognition.

## Run the development demo

Requirements: Docker Desktop with Docker Compose, and free local ports 80/443.

```bash
./scripts/demo-up.sh
```

Open [https://localhost](https://localhost). Traefik uses a generated local
certificate, so the browser will show a certificate warning on first use. The
four visible personas are Care staff, Clinician, Patient, and Clinic admin. All
records and identities are synthetic.

The script derives a dedicated Compose name from this checkout's canonical path
and rejects unrelated `COMPOSE_PROJECT_NAME` overrides. Before a reset it also
verifies every matching container's Compose working-directory/config labels and
rejects production deployment files. To recreate the verified local demo
containers and database volume with an explicit path-bound confirmation:

```bash
./scripts/reset-demo.sh
# non-interactive CI/operator form; the value is unique to this checkout:
RESET_NIGHTINGALE_LOCAL_DEMO="$(./scripts/demo-project-name.sh --fingerprint)" \
  ./scripts/reset-demo.sh
```

See [`docs/DEMO_RUNBOOK.md`](./docs/DEMO_RUNBOOK.md) for the Scenario A-F
walkthrough, deterministic reset order, and the exact automated checks.

### Import the optional synthetic evaluation pack

The default two-patient fixture remains the stable Scenario A-F demo. For a
larger local evaluation set, the pinned importer can add up to 20 Synthea
patients, 10 ACI-Bench dialogue/note pairs, and 5 PriMock57 mock consultations:

```bash
./scripts/import-test-datasets.sh
```

The command verifies every download by SHA-256, keeps raw payloads in the
Git-ignored `datasets/raw/` directory, imports stable UUIDv5 records
idempotently, encrypts clinical payloads, records source/version/checksum
metadata, and restarts the backend and worker. It does not require an API key.
Only synthetic or mock data is used; benchmark reference notes are explicitly
labelled as imported references rather than model output. See
[`datasets/README.md`](./datasets/README.md) for sources, pinned revisions,
licenses, limits, and caveats.

An OpenAI key is needed only for a separate, explicitly enabled live-model
quality evaluation. Supply it through the local shell environment, never Git:

```bash
read -s OPENAI_API_KEY && export OPENAI_API_KEY
export AI_PROVIDER=openai REMOTE_TEXT_EGRESS_ENABLED=true
# also set explicit model IDs and a working required Presidio model before use
```

To regenerate the checked-in, **silent synthetic** UI walkthrough (Scenario A
Glance/provenance, Scenario E patient-safe My Care, and Scenario F voice Review
Mode), run:

```bash
BUN_BIN="$(command -v bun)" ./scripts/record-demo.sh
```

The recorder resets only this checkout-scoped local Compose project, drives the
real TLS application with Playwright at 1280×720, and asks host FFmpeg to write
`output/demo/Nightingale_Demo.mp4` as H.264. The microphone and transcript are
the explicitly labelled synthetic fixtures; no backend responses are stubbed.
Set `NIGHTINGALE_RECORD_KEEP_STATE=1` only when deliberately recording the
already-running synthetic state.

## What is implemented

- **Trustworthy Glance:** up to five precomputed cards with a reason label and
  immutable provenance. Selecting a card resolves its exact Timeline span.
- **Care-note collaboration:** separate Staff and Clinician entries, Tiptap
  editing, selected-text comment anchors, mentions, assignment, resolution,
  immutable versions, diff, revert-as-new-version, and metadata-only audit.
- **Concurrency:** `ETag`/`If-Match` compare-and-swap on one entry; independent
  entries remain independently editable.
- **AI workflow boundary:** clinic-scoped jobs, attempts, idempotency, encrypted
  run records, deterministic extraction, fail-closed redaction, evidence
  pointers, fallback/review states, and an optional configured OpenAI adapter.
- **Bounded importance learning:** clinic-scoped feedback with clamped weights;
  critical, unresolved, pinned, and clinician-confirmed content is protected.
- **Data decay:** eligibility preview, dry run, zstd + AES-GCM cold archive,
  checksum verification, and rehydration without deleting versions, audit, or
  provenance.
- **Voice review:** encrypted resumable browser chunks, multi-device server
  barriers, bounded FFmpeg normalization, durable processing jobs, immutable
  transcript revisions, fact-to-transcript-to-audio evidence, and
  clinician-only publication.
- **Provisional live captions:** an authenticated, clinic-scoped WebSocket
  carries 100 ms application frames as bounded 24 kHz PCM16 to an explicitly
  configured live provider. Per-connection byte/rate caps, a per-process
  provider semaphore, and cross-worker clinic/user/session leases bound
  concurrency. Captions are ephemeral
  and are replaced by the immutable finalize result; disconnects and provider
  errors remain visible as review states.
- **Patient-safe view:** patient DTOs omit raw AI, internal comments, scoring
  internals, raw transcript/facts, and audio. Browser tests inspect actual
  patient network responses.

## Security and privacy boundaries

All tenant data carries a `clinic_id`. API requests resolve clinic, role, and
actor from a current server-side `ClinicMembership`; client-supplied values are
not trusted. PostgreSQL row-level security and clinic-composite foreign keys
backstop application RBAC. The runtime database login is non-owner,
`NOSUPERUSER`, and `NOBYPASSRLS`; only one-shot migration/provisioning commands
receive the owner connection.

Browser sessions use a `Secure`, `HttpOnly`, `SameSite=Lax` cookie over
same-origin TLS. Cookie-authenticated mutations have an Origin check. The
frontend does not keep a bearer token in local storage, and logout clears the
session plus the local encrypted voice queue. Logout intent, failure, and
confirmation are broadcast to every tab: PHI is masked immediately, while a
failed server logout remains visibly retryable. Every rejected `/auth/me`
probe—including a direct visit to `/login` with an expired cookie—uses the same
path and finishes bounded IndexedDB cleanup before sign-in controls appear.
Bearer JWTs remain available for non-browser API compatibility.

Clinic admins invite an email but never create its global user, choose a
temporary password, or silently attach an identity already used by another
clinic. A 24-hour high-entropy one-time code is stored only as a hash; the
recipient opens `/accept-invitation` (the code may be pasted or carried only in
the URL fragment), verifies the invited email, and chooses the password before
the membership is activated. Care-team invites allow Staff, Clinician, and
Admin only; Patient access requires a separate patient-record linking flow.
Deactivation revokes related pending invites, acceptance rechecks the inviter,
and serialized removal cannot remove the final active Admin.

All `/api/v1` responses and the HTML shell set `Cache-Control: private,
no-store` and vary on Cookie, Authorization, and Origin. Content-hashed static
assets alone use a public immutable cache policy.

Clinical text, comments, Glance payloads, transcript/facts, and audio payloads
use a clinic-derived AES-256-GCM envelope. `FIELD_ENCRYPTION_MASTER_KEY` is
independent of `SECRET_KEY`; losing the field key makes existing ciphertext
unreadable. Audit events store metadata rather than clinical body text.

Before remote text egress, Nightingale combines server-known patient terms,
Singapore identifier/contact recognizers, embedded Presidio analysis, and a
residual scan. A missing required model, analyzer error, or residual finding
blocks the remote call and records a fallback/needs-review state. Logs record
entity categories and counts, not the detected value. Automated de-identification
is a defense layer, not a guarantee that arbitrary real clinical text is safe.

This repository is exercised with synthetic data only; it is not presented as
a production EHR, medical device, or compliance certification.

## Provider capability states

The default values are `AI_PROVIDER=deterministic`,
`VOICE_TRANSCRIPTION_PROVIDER=disabled`, and both remote-egress flags `false`.
Provider/model IDs are configuration, never hard-coded promises.

- Remote text requires `AI_PROVIDER=openai`,
  `REMOTE_TEXT_EGRESS_ENABLED=true`, a key, an extract-model ID, and a loadable
  required Presidio model. High-risk independent review also needs a review
  model ID.
- Remote audio requires `VOICE_TRANSCRIPTION_PROVIDER=openai`,
  `REMOTE_AUDIO_EGRESS_ENABLED=true`, a key, and a transcription-model ID.
  `STRICT_NO_AUDIO_EGRESS=true` overrides those settings.
- Local ASR requires the optional image group and a non-empty, pre-cached
  `LOCAL_ASR_MODEL_DIR`; runtime download is disabled. It does not claim
  diarization.
- The pyannote profile exposes experimental local readiness only. No gated
  model, token, diarization output, or acceptance of model terms is bundled.
- `/voice/.../live` reports the configured live-caption capability, and
  `/voice/.../live/ws` is the same-origin WebSocket transport. The default is
  disabled. The deterministic provider is restricted to explicitly synthetic
  development sessions; the OpenAI adapter additionally requires
  `LIVE_TRANSCRIPT_PROVIDER=openai`, `REMOTE_AUDIO_EGRESS_ENABLED=true`, an API
  key, and an explicit `gpt-live-transcribe` model ID. Fixture/mock tests are
  transport tests, not evidence of live model accuracy or latency.

The complete, claim-bounded inventory is in
[`MODEL_INVENTORY.md`](./MODEL_INVENTORY.md). Voice-specific limits and failure
states are in [`docs/VOICE_PIPELINE.md`](./docs/VOICE_PIPELINE.md).

## Verification

Run the release checks (lock, backend static/tests/coverage, frontend
type/lint/unit/build, tracked OpenAPI schema/client sync, and development/production Compose
rendering):

```bash
./scripts/verify-release.sh
```

The complete deployment gate first creates a collision-resistant,
checkout-bound synthetic demo on dynamic loopback ports, then boots the exact
same content-addressed backend image with `compose.yml + compose.deploy.yml` in
an independently scoped production-topology project:

```bash
./scripts/verify-release.sh --e2e --benchmark --ffmpeg
```

`--ffmpeg` builds an image labeled with the current Git commit, rejects a stale
image, and archives its immutable image ID plus **container** `ffmpeg -version`
output at
`docs/evidence/ffmpeg-container-version.txt`. If that command has not been run
for a release image, the container build must be recorded as **not tested**;
host FFmpeg output is not a substitute.

The protected Docker Compose workflow runs this complete command before its
release boundary. Scenario A-F runs with `--repeat-each=3`; the live benchmark
rejects a running backend image whose OCI revision differs from checkout
`HEAD`. The second topology proves production mode, disabled demo auth, no
automatic fixture seed, HTTPS redirect/routing, the durable worker, and its
restricted database role without rebuilding the verified image.

Individual commands are documented in the verification section of the demo
runbook. The checked-in candidate measurement for implementation commit
`2e59a9b89e65c81ac030d943e9f9e7c51cbfcab3`
used the exact **Alex Synthetic** fixture (4/4 expected cards), 20 warmups and
100 local HTTPS samples on the recorded arm64 host: median `3.667 ms`, p95
`4.077 ms`, and p99 `4.275 ms`. The schema/body, Compose config, exact backend
image revision and image digest are in
[`docs/evidence/glance-benchmark.json`](./docs/evidence/glance-benchmark.json).
It measures the precomputed read path, not a model call. Documentation-only
commits after that implementation commit do not retroactively change the
evidence; rerun the full command for every new implementation release and
hardware target.

To produce a commit-bound delivery after a successful full gate, keep the
evidence outside the worktree, add a `release-candidate.txt` summary to that
directory, generate the three-page brief from the same evidence, and package
only while Git is clean. The PDF-only prerequisites are the pinned
`docs/pdf-requirements.txt` plus the `rsvg-convert` executable from librsvg:

```bash
release_evidence="$(mktemp -d /var/tmp/nightingale-release.XXXXXXXX)"
NIGHTINGALE_RELEASE_EVIDENCE_DIR="$release_evidence" \
  ./scripts/verify-release.sh --e2e --benchmark --ffmpeg

# release-candidate.txt must name the exact release-commit.txt SHA, verified
# image ID, test counts, Glance result, and container FFmpeg version.
NIGHTINGALE_EVIDENCE_DIR="$release_evidence" \
  python3 scripts/build_technical_brief_pdf.py
NIGHTINGALE_RELEASE_EVIDENCE_DIR="$release_evidence" \
  ./scripts/package-release.sh
```

`verify-release.sh` rejects a dirty source tree and a reused evidence
directory, captures its own full log, and writes a completion marker only
after every requested gate and cleanup succeeds. `package-release.sh`
cross-checks the commit and image through the completion marker, benchmark,
FFmpeg record, candidate summary, raw log, and PDF evidence binding. It also
rejects a dirty worktree, missing PDF/video/evidence, and an existing output
path. The result contains a source archive, evidence, technical brief, demo
video, per-file SHA-256 manifest, final ZIP, and ZIP checksum outside the
repository.

## Production boundary

The no-`-f` command is deliberately the local development path because Docker
Compose automatically loads `compose.override.yml`; its demo-auth HTTP/HTTPS
ports bind to `127.0.0.1` only. Production explicitly selects only the base and
deployment files and publishes 80/443:

```bash
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

Production Compose refuses an implicit `backend:latest`. Migration, API, and
worker all receive the exact image ID that passed the full gate, so another
checkout or a later rebuild cannot replace the release between verification,
migration, and startup.

The base configuration sets `FASTAPI_ENV=production` and
`ENABLE_DEMO_AUTH=false`; production prestart runs role bootstrap and Alembic
but does **not** create synthetic personas or a clinic. Provision the first
clinic, Admin membership, and Worker membership explicitly with the
environment-only command in
[`deployment-docker-compose.md`](./deployment-docker-compose.md). Replace every
tracked development secret, persist and back up the field-encryption key, and
review provider/model/license terms before enabling any egress or optional
profile.

The provisioning command prints the new `clinic_id`. Open `/login` and use the
**Clinic account** form with that ID, the provisioned Admin email, and the
operator-supplied password. This form uses the same secure HttpOnly-cookie path
as the rest of the browser UI and never persists the returned bearer-compatible
response token. Development persona buttons are deliberately disabled by the
production API.

The supported Docker Compose deployment alone owns the non-cancelling
`production-main` concurrency lane. The entry-metadata Alembic revision retains
explicit `legacy_review_required`/timestamp server defaults as an
expand-compatible bridge for an old API process during migration; a later
contract migration may remove them only after old binaries are retired.

The Docker Compose workflow runs full live release gates without production
secrets, archives the exact OCI image that passed them, and after approval loads
that same artifact on the self-hosted runner rather than rebuilding it. It is
the only complete supported deployment. The FastAPI Cloud workflow is an
unprivileged verification-only check: it has no production environment,
secrets, or production concurrency lock because a one-process deployment does
not start the durable `python -m app.ai_worker` required for voice and AI jobs.
Repository operators must configure required reviewers and a main-only branch
rule on the GitHub `production` environment before enabling Compose deployment.

## Repository map

```text
backend/                 FastAPI, SQLModel, Alembic, workers, pytest
frontend/                React/Vite/Tiptap, Vitest, Playwright
docs/                    voice boundary, demo runbook, measured evidence
scripts/                 start/reset/verification/evidence utilities
compose*.yml             base, development, production, and opt-in profiles
ATTRIBUTION.txt          upstream attribution
THIRD_PARTY_NOTICES.md   dependency and design-reference register
THIRD_PARTY_LICENSES/    distributable full license texts and image notices
```

Nightingale is MIT licensed. It began from FastAPI Full Stack Template commit
`68adb40d`; see [`ATTRIBUTION.txt`](./ATTRIBUTION.txt),
[`THIRD_PARTY_NOTICES.md`](./THIRD_PARTY_NOTICES.md), and
[`THIRD_PARTY_LICENSES`](./THIRD_PARTY_LICENSES), and [`LICENSE`](./LICENSE).
