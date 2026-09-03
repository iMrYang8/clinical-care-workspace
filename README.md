# Nightingale

Nightingale is a shared patient-care workspace for clinical teams to understand current priorities, document care, coordinate follow-up, and review AI-assisted notes against their sources.

## Who it is for

- **Care staff** review current patient priorities, add care notes, and coordinate follow-up.
- **Clinicians** document clinical judgement, review AI-assisted drafts, and confirm supporting sources.
- **Clinic administrators** manage clinic members and review activity without editing clinical content.
- **Patients** use a separate My Care portal to review shared information and send updates to their care team.

## Product workflow

1. Sign in with the clinic code and account supplied by a clinic administrator.
2. Open **Patients**, select a patient, or create a deduplicated patient record as Staff/Clinician.
3. Review **Current priorities** and follow each item to its supporting wording in the **Care timeline**.
4. Add or update a care note in the permitted staff or clinician section.
5. Use **Team discussion** to mention a colleague, assign follow-up, and resolve a thread.
6. Review visit recordings, correct the transcript, confirm clinical findings, and publish the reviewed note.
7. Clinic administrators use **Administration** to invite members and review clinic activity.

Patients sign in separately at `/patient/login` and use `/patient/my-care`. The clinical workspace does not expose patient navigation, and the patient portal does not expose clinical or administrative navigation.

Patient records are created by Staff or Clinicians, then optionally connected to a patient account through a 24-hour email invitation. A separate Platform Administrator workspace at `/platform/login` provides audited, cross-clinic, read-only operational oversight.

## Local development

Requirements: Docker Desktop with Docker Compose, plus free local ports 80, 443, and 8025.

```bash
cp .env.example .env
# Fill the blank secret/password values in .env, then run:
./scripts/demo-up.sh
```

Open [https://localhost](https://localhost). The local reverse proxy uses a generated certificate, so a browser may ask you to accept it on first use.

Local invitation email is available in [Mailpit](http://localhost:8025).

The local clinic code is:

```text
NIGHTINGALE
```

Clinic-team account activation is invitation-only at `/accept-invitation`. Patient portal activation is invitation-only at `/patient/accept-invitation`. The development-only highest-permission account is:

```text
URL: https://localhost/platform/login
Email: platform.admin@nightingale.example
Password: local-platform-owner-only
```

Production creates no default Platform Administrator. Provision it with environment variables and `python -m app.provision_platform_admin`.

To stop the local stack without deleting its database volume:

```bash
docker compose --project-name "$(./scripts/demo-project-name.sh)" down
```

## Architecture

- **Frontend:** React, TypeScript, Vite, TanStack Router and Query, shadcn/ui, Tiptap.
- **Backend:** FastAPI, SQLModel, Alembic, PostgreSQL, background workers.
- **Security:** clinic-scoped membership resolution, role-based authorization, PostgreSQL row-level security, secure same-origin cookies, encrypted clinical payloads, immutable versions, audit events, and provenance pointers.
- **Privacy:** remote text processing is fail-closed behind de-identification checks; the default local configuration uses deterministic processing and does not require a model key.
- **Voice:** resumable encrypted recording uploads, durable processing, transcript revision, confidence review, and fact-to-transcript/audio source navigation.

All client-provided role, actor, and tenant identifiers are treated as untrusted. The server resolves the active membership and clinic for every protected request. Patient response types exclude internal discussion, raw AI material, scoring internals, raw transcripts, and audio.

## Configuration

Copy the local environment template and keep secrets out of Git. Important settings include:

```text
SECRET_KEY
FIELD_ENCRYPTION_MASTER_KEY
POSTGRES_PASSWORD
FIRST_SUPERUSER
FIRST_SUPERUSER_PASSWORD
```

External text or audio processing is optional. When enabled, supply credentials only through the local shell or secret manager and review [`MODEL_INVENTORY.md`](./MODEL_INVENTORY.md) before allowing any remote data flow.

## The sixteen clinic scenarios

This build is assessed against sixteen real-clinic scenarios — a nurse and a
patient disagreeing about an allergy, a provider returning 503 for an hour, two
clinicians typing into the same note at 09:14, a highlight that cites a note
someone has since edited. **Eleven survive, five are partial, none fail
outright.**

[`docs/SCENARIO_TEST_MAP.md`](./docs/SCENARIO_TEST_MAP.md) indexes every
scenario to the automated tests that cover it and names what each one still
does not prove. `backend/tests/test_scenario_map.py` verifies that index
against the source tree, so it cannot rot silently.

Seven scenarios are additionally recorded from the running application. Each
recording is gated: the recorder first walks the click path with capture off
and asserts every declared proof string is on screen, so a scenario that cannot
prove itself produces no footage. See
[`docs/SCENARIO_RECORDINGS.md`](./docs/SCENARIO_RECORDINGS.md).

## Verification

Run the release checks:

```bash
./scripts/verify-release.sh
```

Individual development checks:

```bash
cd backend && uv run pytest
cd trilingual-consult && uv run pytest
cd frontend && bun run test
cd frontend && bun run typecheck
cd frontend && bun run build
```

The frontend suites require Bun, which is what CI pins (1.3.12) and what the
workspace scripts resolve. Running Vitest under Node instead fails on recent
releases — Node 25 exposes its own global `localStorage`, which collides with
the jsdom test environment and breaks every suite that clears storage. Running
it from the repository root fails differently: it sweeps `.worktrees/`, picks
up the Playwright specs, and cannot resolve the `@/` alias.

Browser tests are under `frontend/tests/` and run with `bunx playwright test`
against a running local stack. Security and domain tests cover tenant
isolation, role permissions, version history, concurrent edits, source
traceability, importance feedback, data retention, invitations, and voice
recovery.

## Documentation

- [Technical brief](./docs/TECHNICAL_BRIEF.md) — what the clinic-scenario feedback changed, where this build fails first, and which assumptions did not survive
- [Scenario-to-test map](./docs/SCENARIO_TEST_MAP.md) — all sixteen scenarios, their verdicts, and the tests that cover them
- [Architecture and product overview](./docs/ARCHITECTURE.md)
- [Architecture diagram](./docs/architecture.svg)
- [Scenario recordings](./docs/SCENARIO_RECORDINGS.md)
- [中文最终演示脚本](./docs/DEMO_SCRIPT.zh-CN.md)
- [Voice pipeline](./docs/VOICE_PIPELINE.md)
- [Model inventory](./MODEL_INVENTORY.md)
- [Dataset sources and limits](./datasets/README.md)
- [中文用户验收清单](./docs/USER_ACCEPTANCE_CHECKLIST.zh-CN.md)
- [Local operations and delivery evidence](./docs/delivery/BUILD_DELIVERY.md)
- [Attribution](./ATTRIBUTION.txt)
- [Third-party notices](./THIRD_PARTY_NOTICES.md)

## License

Nightingale is released under the [MIT License](./LICENSE). Third-party components and design references are recorded in [`ATTRIBUTION.txt`](./ATTRIBUTION.txt) and [`THIRD_PARTY_NOTICES.md`](./THIRD_PARTY_NOTICES.md).
