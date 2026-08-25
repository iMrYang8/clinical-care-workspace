#!/usr/bin/env bash

set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
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
  --e2e        start an isolated temporary TLS demo and run Playwright
  --benchmark  run the precomputed Glance p95 <= 300 ms gate
  --ffmpeg     archive the backend container's actual ffmpeg -version record

Environment:
  BUN_BIN                    explicit Bun executable
  NIGHTINGALE_SKIP_INSTALL=1 require existing frozen Python/JS environments
  COMPOSE_PROJECT_NAME       rejected; verification always chooses scoped project names
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
need python3
docker compose version >/dev/null 2>&1 || { echo "docker compose is required" >&2; exit 1; }
if [[ -n "${COMPOSE_PROJECT_NAME:-}" ]]; then
  echo "verify-release chooses isolated project names; unset COMPOSE_PROJECT_NAME." >&2
  exit 2
fi

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
export NIGHTINGALE_SOURCE_COMMIT="$(git rev-parse HEAD)"
export NIGHTINGALE_CHECKOUT_FINGERPRINT="$(./scripts/demo-project-name.sh --fingerprint)"

section "Frozen dependency locks"
uv lock --check
if [[ "${NIGHTINGALE_SKIP_INSTALL:-}" != "1" ]]; then
  uv sync --frozen --package app
  "$bun_bin" install --frozen-lockfile
fi

section "Compose rendering"
./scripts/test-demo-script-safety.sh
./scripts/test-script-safety.sh
docker compose config --quiet
docker compose config --format json | \
  python3 scripts/assert_compose_ports.py development
DOMAIN="${DOMAIN:-nightingale.invalid}" \
NIGHTINGALE_BACKEND_IMAGE="nightingale-backend:${NIGHTINGALE_SOURCE_COMMIT}" \
  docker compose -f compose.yml -f compose.deploy.yml config --quiet
DOMAIN="${DOMAIN:-nightingale.invalid}" \
NIGHTINGALE_BACKEND_IMAGE="nightingale-backend:${NIGHTINGALE_SOURCE_COMMIT}" \
  docker compose -f compose.yml -f compose.deploy.yml config --format json | \
  python3 scripts/assert_compose_ports.py production

section "Backend static checks"
(
  cd backend
  uv run --frozen --no-sync ruff check app tests
  uv run --frozen --no-sync ruff format app tests --check
  uv run --frozen --no-sync mypy app
  uv run --frozen --no-sync ty check app
)

section "Backend PostgreSQL contracts, coverage, and migration roundtrip"
verify_project="$(./scripts/temporary-project-name.sh verify)"
verify_port="${NIGHTINGALE_VERIFY_DB_PORT:-$(python3 scripts/free-local-port.py)}"
if [[ ! "$verify_port" =~ ^[0-9]+$ || "$verify_port" -lt 1024 || "$verify_port" -gt 65535 ]]; then
  echo "NIGHTINGALE_VERIFY_DB_PORT must be an unprivileged TCP port." >&2
  exit 2
fi
./scripts/assert-compose-project-empty.sh "$verify_project"
verify_created=false
cleanup_verify() {
  if [[ "$verify_created" != true ]]; then return 0; fi
  if ! ./scripts/assert-demo-project-ownership.sh "$verify_project" --temporary; then
    echo "Refusing cleanup for unverified Compose project $verify_project" >&2
    return 1
  fi
  DEV_DB_PORT="$verify_port" docker compose --project-name "$verify_project" \
    -f compose.yml -f compose.override.yml -f compose.dev-tools.yml down \
    --volumes --remove-orphans
  verify_created=false
}
trap 'cleanup_verify || true' EXIT INT TERM
verify_created=true
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
git ls-files --error-unmatch frontend/openapi.json >/dev/null
BUN_BIN="$bun_bin" ./scripts/generate-client.sh
git diff --exit-code -- frontend/openapi.json frontend/src/client

live_project=""
live_created=false
live_http_port=""
live_https_port=""
benchmark_output=""
cleanup_live() {
  if [[ -n "$live_project" && "$live_created" == true ]]; then
    if ! ./scripts/assert-demo-project-ownership.sh "$live_project" --temporary; then
      echo "Refusing cleanup for unverified Compose project $live_project" >&2
      return 1
    fi
    LOCAL_HTTP_PORT="$live_http_port" LOCAL_HTTPS_PORT="$live_https_port" \
      docker compose --project-name "$live_project" \
        -f compose.yml -f compose.override.yml \
        down --volumes --remove-orphans
    live_created=false
  fi
  if [[ -n "$benchmark_output" ]]; then
    rm -f "$benchmark_output"
  fi
}

if [[ "$run_e2e" == true || "$run_benchmark" == true ]]; then
  section "Isolated synthetic TLS demo for optional live gates"
  live_project="$(./scripts/temporary-project-name.sh release)"
  live_http_port="$(python3 scripts/free-local-port.py)"
  live_https_port="$(python3 scripts/free-local-port.py)"
  while [[ "$live_https_port" == "$live_http_port" ]]; do
    live_https_port="$(python3 scripts/free-local-port.py)"
  done
  export LOCAL_HTTP_PORT="$live_http_port" LOCAL_HTTPS_PORT="$live_https_port"
  ./scripts/assert-compose-project-empty.sh "$live_project"
  trap 'cleanup_live || true' EXIT INT TERM
  live_created=true
  docker compose --project-name "$live_project" \
    -f compose.yml -f compose.override.yml \
    up --build --detach --wait proxy db prestart backend ai-worker mailpit
  ./scripts/assert-demo-project-ownership.sh "$live_project" --temporary
fi

if [[ "$run_e2e" == true ]]; then
  section "Playwright Scenario A-F"
  docker compose --project-name "$live_project" \
    -f compose.yml -f compose.override.yml build playwright
  docker compose --project-name "$live_project" \
    -f compose.yml -f compose.override.yml run --rm --no-deps -e CI=1 \
    playwright bunx playwright test --fail-on-flaky-tests \
      --trace=retain-on-failure --repeat-each=3 --workers=1
fi

if [[ "$run_benchmark" == true ]]; then
  section "Warm precomputed Glance latency"
  benchmark_output="$(mktemp "${TMPDIR:-/tmp}/nightingale-glance.XXXXXX.json")"
  uv run --frozen --no-sync --package app python scripts/benchmark_glance.py \
    --base-url "https://localhost:${live_https_port}" --insecure \
    --compose-project "$live_project" --output "$benchmark_output"
  rm -f "$benchmark_output"
  benchmark_output=""
fi

if [[ -n "$live_project" ]]; then
  cleanup_live
  trap - EXIT INT TERM
  live_project=""
fi

if [[ "$run_ffmpeg" == true ]]; then
  section "Container FFmpeg release evidence"
  ./scripts/capture_ffmpeg_inventory.sh
fi

section "Release verification complete"
