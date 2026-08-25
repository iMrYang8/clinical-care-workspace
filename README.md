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
failed server logout remains visibly retryable. An authenticated `401` uses the
same path before another persona may sign in. Bearer JWTs remain available for
non-browser API compatibility.

Clinic admins invite an email but never create its global user, choose a
temporary password, or silently attach an identity already used by another
clinic. A 24-hour high-entropy one-time code is stored only as a hash; the
recipient verifies that code and chooses the password before the membership is
activated. Admin removal is serialized and cannot remove the final active
admin.

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
- `/voice/.../live` is an honest capability endpoint. This build has no live
  caption transport/provider and reports unavailable rather than fabricating
  captions.

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

Optional live checks create a collision-resistant, checkout-bound temporary
synthetic demo on dynamically selected loopback ports:

```bash
./scripts/verify-release.sh --e2e --benchmark --ffmpeg
```

`--ffmpeg` builds an image labeled with the current Git commit, rejects a stale
image, and archives its immutable image ID plus **container** `ffmpeg -version`
output at
`docs/evidence/ffmpeg-container-version.txt`. If that command has not been run
for a release image, the container build must be recorded as **not tested**;
host FFmpeg output is not a substitute.

Individual commands are documented in the verification section of the demo
runbook. The measured Glance evidence committed for this checkout is
[`docs/evidence/glance-benchmark.json`](./docs/evidence/glance-benchmark.json);
rerun the benchmark for the actual release commit and hardware.

## Production boundary

The no-`-f` command is deliberately the local development path because Docker
Compose automatically loads `compose.override.yml`; its demo-auth HTTP/HTTPS
ports bind to `127.0.0.1` only. Production explicitly selects only the base and
deployment files and publishes 80/443:

```bash
docker compose -f compose.yml -f compose.deploy.yml build
docker compose -f compose.yml -f compose.deploy.yml run --rm prestart
docker compose -f compose.yml -f compose.deploy.yml up -d
```

The base configuration sets `FASTAPI_ENV=production` and
`ENABLE_DEMO_AUTH=false`; production prestart runs role bootstrap and Alembic
but does **not** create synthetic personas or a clinic. Provision the first
clinic, Admin membership, and Worker membership explicitly with the
environment-only command in
[`deployment-docker-compose.md`](./deployment-docker-compose.md). Replace every
tracked development secret, persist and back up the field-encryption key, and
review provider/model/license terms before enabling any egress or optional
profile.

Production GitHub deployments share one non-cancelling `production-main`
concurrency lane. The entry-metadata Alembic revision retains explicit
`legacy_review_required`/timestamp server defaults as an expand-compatible
bridge for an old API process during migration; a later contract migration may
remove them only after old binaries are retired.

Both deploy workflows run release gates without production secrets, upload an
immutable verified-SHA artifact, and deploy only that exact SHA on `main`.
Repository operators must configure required reviewers and a main-only branch
rule on the GitHub `production` environment before enabling deployment.

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
