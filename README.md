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

## Verification

Run the release checks:

```bash
./scripts/verify-release.sh
```

Individual development checks:

```bash
cd backend && uv run pytest
node node_modules/vitest/vitest.mjs run
node node_modules/typescript/bin/tsc -p frontend/tsconfig.build.json
node node_modules/vite/bin/vite.js build --config frontend/vite.config.ts
```

Browser tests are under `frontend/tests/`. Security and domain tests cover tenant isolation, role permissions, version history, concurrent edits, source traceability, importance feedback, data retention, invitations, and voice recovery.

## Documentation

- [Architecture diagram](./docs/architecture.svg)
- [Technical brief](./docs/TECHNICAL_BRIEF.md)
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
