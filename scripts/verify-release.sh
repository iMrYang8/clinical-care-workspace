#!/usr/bin/env bash

set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
run_e2e=false
run_benchmark=false
run_ffmpeg=false

usage() {
  cat <<'EOF'
Usage: scripts/verify-release.sh [--e2e] [--benchmark] [--ffmpeg]

Default gates:
  frozen locks; backend Ruff/mypy/ty/pytest/coverage/Alembic; frontend
  type/lint/unit/build; OpenAPI generated-client sync; development and
  production Compose rendering.

Optional gates:
  --e2e        start/reuse the synthetic TLS demo and run Playwright
  --benchmark  run the precomputed Glance p95 <= 300 ms gate
  --ffmpeg     archive the backend container's actual ffmpeg -version record

Environment:
  BUN_BIN                    explicit Bun executable
  NIGHTINGALE_SKIP_INSTALL=1 require existing frozen Python/JS environments
  COMPOSE_PROJECT_NAME       demo project for optional live gates (default nightingale)
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --e2e) run_e2e=true; shift ;;
    --benchmark) run_benchmark=true; shift ;;
    --ffmpeg) run_ffmpeg=true; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

need() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Required command not found: $1" >&2
    exit 1
  fi
}

section() {
  printf '\n==> %s\n' "$1"
}

need git
need uv
need docker
docker compose version >/dev/null 2>&1 || { echo "docker compose is required" >&2; exit 1; }

if [[ -n "${BUN_BIN:-}" ]]; then
  if command -v "$BUN_BIN" >/dev/null 2>&1; then
    bun_bin="$(command -v "$BUN_BIN")"
  else
    bun_bin="$BUN_BIN"
  fi
elif command -v bun >/dev/null 2>&1; then
  bun_bin="$(command -v bun)"
elif [[ -x /private/tmp/nightingale-bun/node_modules/.bin/bun ]]; then
  # Codex/local fallback; clean machines should install Bun normally or set BUN_BIN.
  bun_bin=/private/tmp/nightingale-bun/node_modules/.bin/bun
else
  echo "Bun was not found. Install Bun or set BUN_BIN to its executable." >&2
  exit 1
fi
if [[ ! -x "$bun_bin" ]]; then
  echo "BUN_BIN is not executable: $bun_bin" >&2
  exit 1
fi
export BUN_BIN="$bun_bin"
export UV_CACHE_DIR="${UV_CACHE_DIR:-${TMPDIR:-/tmp}/nightingale-uv-cache}"

cd "$root"

section "Frozen dependency locks"
uv lock --check
if [[ "${NIGHTINGALE_SKIP_INSTALL:-}" != "1" ]]; then
  uv sync --frozen --package app
  "$bun_bin" install --frozen-lockfile
fi

section "Compose rendering"
docker compose config --quiet
DOMAIN="${DOMAIN:-nightingale.invalid}" \
  docker compose -f compose.yml -f compose.deploy.yml config --quiet

section "Backend static checks"
(
  cd backend
  uv run --frozen --no-sync ruff check app tests
  uv run --frozen --no-sync ruff format app tests --check
  uv run --frozen --no-sync mypy app
  uv run --frozen --no-sync ty check app
)

section "Backend PostgreSQL contracts, coverage, and migration roundtrip"
verify_project="nightingale-verify-$$"
verify_port="${NIGHTINGALE_VERIFY_DB_PORT:-55432}"
cleanup_verify() {
  DEV_DB_PORT="$verify_port" docker compose --project-name "$verify_project" \
    -f compose.yml -f compose.override.yml -f compose.dev-tools.yml down \
    --volumes --remove-orphans >/dev/null 2>&1 || true
}
trap cleanup_verify EXIT INT TERM
DEV_DB_PORT="$verify_port" docker compose --project-name "$verify_project" \
  -f compose.yml -f compose.override.yml -f compose.dev-tools.yml \
  up --detach --wait db
(
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
  export FASTAPI_ENV=development
  export DATABASE_URL="postgresql://nightingale_app:${POSTGRES_APP_PASSWORD}@127.0.0.1:${verify_port}/app"
  export MIGRATION_DATABASE_URL="postgresql://postgres:${POSTGRES_PASSWORD}@127.0.0.1:${verify_port}/app"
  cd backend
  uv run --frozen --no-sync bash scripts/prestart.sh
  uv run --frozen --no-sync python app/tests_pre_start.py
  uv run --frozen --no-sync coverage run -m pytest tests
  uv run --frozen --no-sync coverage report --fail-under=90
  uv run --frozen --no-sync alembic downgrade -1
  uv run --frozen --no-sync alembic upgrade head
  uv run --frozen --no-sync alembic current
  uv run --frozen --no-sync alembic check
)
cleanup_verify
trap - EXIT INT TERM

section "Frontend type, lint, unit, and production build"
"$bun_bin" run --filter frontend typecheck
"$bun_bin" run --filter frontend lint
"$bun_bin" run --filter frontend test
"$bun_bin" run --filter frontend build

section "Generated OpenAPI client synchronization"
BUN_BIN="$bun_bin" ./scripts/generate-client.sh
git diff --exit-code -- frontend/openapi.json frontend/src/client

if [[ "$run_e2e" == true || "$run_benchmark" == true ]]; then
  section "Synthetic TLS demo for optional live gates"
  ./scripts/demo-up.sh
fi

if [[ "$run_e2e" == true ]]; then
  section "Playwright Scenario A-F"
  demo_project="${COMPOSE_PROJECT_NAME:-nightingale}"
  docker compose --project-name "$demo_project" build playwright
  docker compose --project-name "$demo_project" run --rm -e CI=1 \
    playwright bun run test:e2e
fi

if [[ "$run_benchmark" == true ]]; then
  section "Warm precomputed Glance latency"
  benchmark_output="$(mktemp "${TMPDIR:-/tmp}/nightingale-glance.XXXXXX.json")"
  trap 'rm -f "$benchmark_output"' EXIT
  uv run --frozen --no-sync --package app python scripts/benchmark_glance.py \
    --insecure --output "$benchmark_output"
  rm -f "$benchmark_output"
  trap - EXIT
fi

if [[ "$run_ffmpeg" == true ]]; then
  section "Container FFmpeg release evidence"
  ./scripts/capture_ffmpeg_inventory.sh --no-build
fi

section "Release verification complete"
