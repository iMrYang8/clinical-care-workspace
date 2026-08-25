# Contributing to Nightingale

Nightingale is a clinic-scoped synthetic-data demonstration. Contributions
must preserve its evidence boundaries: deterministic fixtures are not model
results, optional providers are not considered validated until a
release-specific record exists, and no real patient information belongs in the
repository, logs, screenshots, or tests.

## Before changing code

- Work from `main` on a focused branch and describe the affected role, tenant,
  data, and provider boundaries.
- Keep `clinic_id`, membership, and actor identity server-derived. Never accept
  those values from a browser as an authorization decision.
- Use additive Alembic migrations. Historical template migrations are retained
  only to keep the imported baseline reproducible.
- Record copied or substantially adapted third-party code in
  `THIRD_PARTY_NOTICES.md`; keep license headers when required.
- Add only synthetic fixtures and use placeholders for deployment secrets.

## Required checks

Run the complete local gate before requesting review:

```bash
./scripts/verify-release.sh
```

Changes to browser flows should also run the relevant Scenario A-F test, and a
release candidate should run all scenarios against the HTTPS Compose stack:

```bash
./scripts/verify-release.sh --e2e --benchmark
```

Backend changes require tests for role, tenant, patient-DTO, audit, and
encryption boundaries where applicable. API schema changes must regenerate and
commit `frontend/openapi.json` and `frontend/src/client`; CI rejects drift.

## Review expectations

A review should distinguish correctness evidence from planned or unavailable
external validation. Report test commands and exit status, keep performance
records tied to commit and hardware, and mark OpenAI, Hugging Face, or other
external-provider behavior as not tested when credentials or assets were not
actually used.
