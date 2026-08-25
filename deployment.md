# Nightingale deployment boundary

## Fully supported production topology

Nightingale's complete production path is Docker Compose:
[`deployment-docker-compose.md`](./deployment-docker-compose.md). The supported
release starts all of these components from one inspected, content-addressed
backend image:

- migration/prestart owner job;
- FastAPI web/API service;
- durable `python -m app.ai_worker` service for AI and voice jobs;
- PostgreSQL with a restricted runtime role; and
- Traefik HTTPS routing.

The protected `.github/workflows/deploy-docker-compose.yml` workflow is the
production deployment entrypoint. It accepts only `main`, shares the literal
non-cancelling `production-main` concurrency lane, waits for the protected
`production` environment, and refuses to migrate or start an image whose OCI
revision is not the exact verified GitHub SHA.

Before migration, the workflow runs the complete current-checkout release gate:
backend/frontend checks, Scenario A-F with three repetitions, the live Glance
p95 benchmark, container FFmpeg capture, TLS health, worker readiness, and
backend/worker image-revision checks. After startup it uses Docker Compose
`--wait`, validates the public HTTPS health endpoint, and verifies the durable
worker process, restricted database role, and immutable image ID.

## FastAPI Cloud deployment disabled

`.github/workflows/deploy.yml` is intentionally verification-only. It performs
the same current-SHA release gates and crosses the protected environment, but
it does not read production secrets, migrate a database, or execute
`fastapi deploy`.

The single-service FastAPI Cloud command used by the upstream template does not
provision Nightingale's required durable `python -m app.ai_worker` process.
Deploying only the web process would accept voice/AI jobs that never leave the
queue, so that path is fail-closed rather than presented as a supported
production deployment. It may be enabled only after a reviewed platform
configuration deploys web and worker together, binds both to the same immutable
artifact and configuration, and provides independent post-deploy readiness
checks for both processes.

## Release evidence is SHA-bound

Run the full local release gate with an evidence directory outside the Git
worktree:

```bash
export NIGHTINGALE_RELEASE_EVIDENCE_DIR="$(mktemp -d)"
./scripts/verify-release.sh --e2e --benchmark --ffmpeg
```

The Glance evidence includes the running backend image's OCI revision and
rejects any value different from checkout `HEAD`. The FFmpeg record is captured
from a content-addressed image whose revision label must also equal `HEAD`.
Checked-in evidence from an older commit is historical documentation only and
never satisfies a current release gate.

Repository operators must configure required reviewers and a main-only
deployment-branch policy on the GitHub `production` environment. Workflow YAML
can name that environment but cannot create its repository protection rules.
