#!/usr/bin/env bash

set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
run_e2e=false
run_benchmark=false
run_ffmpeg=false
evidence_dir="${NIGHTINGALE_RELEASE_EVIDENCE_DIR:-}"

usage() {
  cat <<'EOF'
Usage: scripts/verify-release.sh [--e2e] [--benchmark] [--ffmpeg]

Default gates:
  frozen locks; backend Ruff/mypy/ty/pytest/coverage/Alembic; frontend
  type/lint/unit/build; OpenAPI generated-client sync; development and
  production Compose rendering.

Optional gates:
  --e2e        start an isolated TLS demo and run Scenario A-F three times
  --benchmark  run the current-image precomputed Glance p95 <= 300 ms gate
  --ffmpeg     capture the current-image ffmpeg -version record

Environment:
  BUN_BIN                    explicit Bun executable
  NIGHTINGALE_SKIP_INSTALL=1 require existing frozen Python/JS environments
  NIGHTINGALE_RELEASE_EVIDENCE_DIR
                             preserve live benchmark/FFmpeg evidence here
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
need curl
docker compose version >/dev/null 2>&1 || { echo "docker compose is required" >&2; exit 1; }
if [[ -n "${COMPOSE_PROJECT_NAME:-}" ]]; then
  echo "verify-release chooses isolated project names; unset COMPOSE_PROJECT_NAME." >&2
  exit 2
fi
if [[ -n "${NIGHTINGALE_BACKEND_IMAGE:-}" ]]; then
  echo "verify-release builds and selects its own candidate; unset NIGHTINGALE_BACKEND_IMAGE." >&2
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
if [[ -n "$evidence_dir" ]]; then
  mkdir -p "$evidence_dir"
  evidence_dir="$(cd "$evidence_dir" && pwd -P)"
  printf '%s\n' "$NIGHTINGALE_SOURCE_COMMIT" > "$evidence_dir/release-commit.txt"
fi

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
benchmark_output_is_temporary=false
verified_backend_image_id=""
production_project=""
production_created=false
production_http_port=""
production_https_port=""
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
  if [[ -n "$benchmark_output" && "$benchmark_output_is_temporary" == true ]]; then
    rm -f "$benchmark_output"
  fi
}

cleanup_production() {
  if [[ -z "$production_project" || "$production_created" != true ]]; then
    return 0
  fi
  if ! ./scripts/assert-production-project-ownership.sh "$production_project"; then
    echo "Refusing cleanup for unverified production-topology project $production_project" >&2
    return 1
  fi
  docker compose --project-name "$production_project" \
    -f compose.yml -f compose.deploy.yml down --volumes --remove-orphans
  production_created=false
}

assert_live_release_topology() {
  local backend_id backend_image backend_revision worker_id worker_image
  local worker_revision worker_state worker_command health_body
  backend_id="$(docker compose --project-name "$live_project" \
    -f compose.yml -f compose.override.yml ps -q backend)"
  worker_id="$(docker compose --project-name "$live_project" \
    -f compose.yml -f compose.override.yml ps -q ai-worker)"
  [[ -n "$backend_id" && -n "$worker_id" ]] || {
    echo "Live release topology is missing backend or ai-worker." >&2
    return 1
  }

  backend_image="$(docker inspect --format '{{.Image}}' "$backend_id")"
  worker_image="$(docker inspect --format '{{.Image}}' "$worker_id")"
  backend_revision="$(docker image inspect --format \
    '{{ index .Config.Labels "org.opencontainers.image.revision" }}' \
    "$backend_image")"
  worker_revision="$(docker image inspect --format \
    '{{ index .Config.Labels "org.opencontainers.image.revision" }}' \
    "$worker_image")"
  [[ "$backend_image" == "$worker_image" ]] || {
    echo "Backend and worker do not use the same immutable image." >&2
    return 1
  }
  [[ "$backend_revision" == "$NIGHTINGALE_SOURCE_COMMIT" \
     && "$worker_revision" == "$NIGHTINGALE_SOURCE_COMMIT" ]] || {
    echo "Backend/worker image revision is not the checkout HEAD." >&2
    return 1
  }
  verified_backend_image_id="$backend_image"

  worker_state="$(docker inspect --format '{{.State.Running}}' "$worker_id")"
  worker_command="$(docker inspect --format '{{json .Config.Cmd}}' "$worker_id")"
  [[ "$worker_state" == true && "$worker_command" == *"app.ai_worker"* ]] || {
    echo "AI worker process is not running the expected command." >&2
    return 1
  }
  docker compose --project-name "$live_project" \
    -f compose.yml -f compose.override.yml exec -T ai-worker \
    python -c "import app.ai_worker; from app.core.db import engine; from sqlalchemy import text; connection = engine.connect(); connection.execute(text('SELECT 1')); connection.close()"

  health_body=""
  for _ in {1..12}; do
    if health_body="$(curl --fail --insecure \
      "https://localhost:${live_https_port}/api/v1/utils/health-check/")"; then
      break
    fi
    sleep 2
  done
  [[ "$health_body" == true ]] || {
    echo "TLS application health check did not return true." >&2
    return 1
  }
}

assert_production_release_topology() {
  local backend_id worker_id prestart_id proxy_id
  local backend_image worker_image prestart_image
  local backend_env worker_env proxy_command health_body http_status
  local demo_status demo_headers demo_body clinic_count

  backend_id="$(docker compose --project-name "$production_project" \
    -f compose.yml -f compose.deploy.yml ps -q backend)"
  worker_id="$(docker compose --project-name "$production_project" \
    -f compose.yml -f compose.deploy.yml ps -q ai-worker)"
  proxy_id="$(docker compose --project-name "$production_project" \
    -f compose.yml -f compose.deploy.yml ps -q proxy)"
  prestart_id="$(docker compose --project-name "$production_project" \
    -f compose.yml -f compose.deploy.yml ps -aq prestart)"
  [[ -n "$backend_id" && -n "$worker_id" && -n "$proxy_id" && -n "$prestart_id" ]] || {
    echo "Production topology is missing proxy, prestart, backend, or ai-worker." >&2
    return 1
  }

  backend_image="$(docker inspect --format '{{.Image}}' "$backend_id")"
  worker_image="$(docker inspect --format '{{.Image}}' "$worker_id")"
  prestart_image="$(docker inspect --format '{{.Image}}' "$prestart_id")"
  [[ "$backend_image" == "$verified_backend_image_id" \
     && "$worker_image" == "$verified_backend_image_id" \
     && "$prestart_image" == "$verified_backend_image_id" ]] || {
    echo "Production topology did not run the exact verified content-addressed image." >&2
    return 1
  }

  backend_env="$(docker inspect --format '{{range .Config.Env}}{{println .}}{{end}}' "$backend_id")"
  worker_env="$(docker inspect --format '{{range .Config.Env}}{{println .}}{{end}}' "$worker_id")"
  grep -Fxq 'FASTAPI_ENV=production' <<<"$backend_env"
  grep -Fxq 'ENABLE_DEMO_AUTH=false' <<<"$backend_env"
  grep -Fxq 'FASTAPI_ENV=production' <<<"$worker_env"
  grep -Fxq 'ENABLE_DEMO_AUTH=false' <<<"$worker_env"
  proxy_command="$(docker inspect --format '{{json .Config.Cmd}}' "$proxy_id")"
  [[ "$proxy_command" == *'certificatesresolvers.le.acme.tlschallenge=true'* ]] || {
    echo "Production proxy is not running the declared ACME TLS topology." >&2
    return 1
  }

  docker compose --project-name "$production_project" \
    -f compose.yml -f compose.deploy.yml exec -T ai-worker \
    python -c "import app.ai_worker; from app.core.db import assert_restricted_runtime_database; assert_restricted_runtime_database()"

  clinic_count="$(docker compose --project-name "$production_project" \
    -f compose.yml -f compose.deploy.yml exec -T db \
    psql -U postgres -d app -Atc 'SELECT count(*) FROM clinics')"
  [[ "$clinic_count" == 0 ]] || {
    echo "Production prestart unexpectedly seeded demo data." >&2
    return 1
  }

  health_body=""
  for _ in {1..24}; do
    if health_body="$(curl --fail --silent --show-error --insecure --noproxy '*' \
      --resolve "${DOMAIN}:${production_https_port}:127.0.0.1" \
      "https://${DOMAIN}:${production_https_port}/api/v1/utils/health-check/")"; then
      break
    fi
    sleep 2
  done
  [[ "$health_body" == true ]] || {
    echo "Production HTTPS topology health check did not return true." >&2
    return 1
  }

  http_status="$(curl --silent --output /dev/null --write-out '%{http_code}' \
    --noproxy '*' --resolve "${DOMAIN}:${production_http_port}:127.0.0.1" \
    "http://${DOMAIN}:${production_http_port}/api/v1/utils/health-check/")"
  [[ "$http_status" == 301 || "$http_status" == 302 \
     || "$http_status" == 307 || "$http_status" == 308 ]] || {
    echo "Production HTTP entrypoint did not redirect to HTTPS (status $http_status)." >&2
    return 1
  }

  demo_headers="$(mktemp "${TMPDIR:-/tmp}/nightingale-production-demo-headers.XXXXXX")"
  demo_body="$(mktemp "${TMPDIR:-/tmp}/nightingale-production-demo-body.XXXXXX")"
  demo_status="$(curl --silent --show-error --insecure --noproxy '*' \
    --resolve "${DOMAIN}:${production_https_port}:127.0.0.1" \
    --dump-header "$demo_headers" --output "$demo_body" --write-out '%{http_code}' \
    -H "Origin: https://${DOMAIN}" -H 'Content-Type: application/json' \
    --data '{"persona":"clinician"}' \
    "https://${DOMAIN}:${production_https_port}/api/v1/auth/demo-login")"
  if [[ "$demo_status" != 404 ]]; then
    rm -f "$demo_headers" "$demo_body"
    echo "Production demo-login unexpectedly succeeded (status $demo_status)." >&2
    return 1
  fi
  if grep -qi '^set-cookie:' "$demo_headers"; then
    rm -f "$demo_headers" "$demo_body"
    echo "Production demo-login unexpectedly set an authentication cookie." >&2
    return 1
  fi
  rm -f "$demo_headers" "$demo_body"
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
  assert_live_release_topology
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
  if [[ -n "$evidence_dir" ]]; then
    benchmark_output="$evidence_dir/glance-benchmark.json"
  else
    benchmark_output="$(mktemp "${TMPDIR:-/tmp}/nightingale-glance.XXXXXX.json")"
    benchmark_output_is_temporary=true
  fi
  uv run --frozen --no-sync --package app python scripts/benchmark_glance.py \
    --base-url "https://localhost:${live_https_port}" --insecure \
    --compose-project "$live_project" --output "$benchmark_output"
  if [[ "$benchmark_output_is_temporary" == true ]]; then
    rm -f "$benchmark_output"
  fi
  benchmark_output=""
  benchmark_output_is_temporary=false
fi

if [[ -n "$live_project" ]]; then
  cleanup_live
  trap - EXIT INT TERM
  live_project=""
fi

if [[ "$run_ffmpeg" == true ]]; then
  section "Container FFmpeg release evidence"
  if [[ -n "$evidence_dir" ]]; then
    ./scripts/capture_ffmpeg_inventory.sh \
      --output "$evidence_dir/ffmpeg-container-version.txt"
  else
    ./scripts/capture_ffmpeg_inventory.sh
  fi
  if [[ -n "$evidence_dir" ]]; then
    ffmpeg_evidence="$evidence_dir/ffmpeg-container-version.txt"
  else
    ffmpeg_evidence="$root/docs/evidence/ffmpeg-container-version.txt"
  fi
  ffmpeg_image_id="$(sed -n 's/^backend_image_id=//p' "$ffmpeg_evidence")"
  [[ -n "$ffmpeg_image_id" ]] || {
    echo "FFmpeg evidence did not identify its content-addressed backend image." >&2
    exit 1
  }
  if [[ -n "$verified_backend_image_id" \
        && "$verified_backend_image_id" != "$ffmpeg_image_id" ]]; then
    echo "Live gates and FFmpeg evidence did not use the same backend image." >&2
    exit 1
  fi
  verified_backend_image_id="$ffmpeg_image_id"
fi

if [[ "$run_e2e" == true || "$run_benchmark" == true || "$run_ffmpeg" == true ]]; then
  section "Exact-image production Compose topology"
  [[ -n "$verified_backend_image_id" ]] || {
    echo "No content-addressed backend image is available for production verification." >&2
    exit 1
  }
  if [[ -n "$evidence_dir" ]]; then
    printf '%s\n' "$verified_backend_image_id" > \
      "$evidence_dir/verified-backend-image-id.txt"
  fi

  production_project="$(./scripts/temporary-project-name.sh release)"
  production_http_port="$(python3 scripts/free-local-port.py)"
  production_https_port="$(python3 scripts/free-local-port.py)"
  while [[ "$production_https_port" == "$production_http_port" ]]; do
    production_https_port="$(python3 scripts/free-local-port.py)"
  done
  ./scripts/assert-compose-project-empty.sh "$production_project"

  export DOMAIN=nightingale.invalid
  export PROJECT_NAME="Nightingale production release verification"
  export SECRET_KEY="$(openssl rand -hex 32)"
  export FIELD_ENCRYPTION_MASTER_KEY="$(openssl rand -base64 32 | tr -d '\n')"
  export POSTGRES_PASSWORD="$(openssl rand -hex 24)"
  export POSTGRES_APP_PASSWORD="$(openssl rand -hex 24)"
  export SMTP_HOST=smtp.invalid
  export SMTP_USER=""
  export SMTP_PASSWORD=""
  export EMAILS_FROM_EMAIL=nightingale@nightingale.invalid
  export NIGHTINGALE_BACKEND_IMAGE="$verified_backend_image_id"
  export NIGHTINGALE_PRODUCTION_BIND_ADDRESS=127.0.0.1
  export NIGHTINGALE_PRODUCTION_HTTP_PORT="$production_http_port"
  export NIGHTINGALE_PRODUCTION_HTTPS_PORT="$production_https_port"
  export FASTAPI_ENV=production
  export ENABLE_DEMO_AUTH=false

  trap 'cleanup_production || true' EXIT INT TERM
  production_created=true
  docker compose --project-name "$production_project" \
    -f compose.yml -f compose.deploy.yml \
    up --no-build --detach --wait --wait-timeout 180 \
    proxy db prestart backend ai-worker
  ./scripts/assert-production-project-ownership.sh "$production_project"
  assert_production_release_topology
  cleanup_production
  trap - EXIT INT TERM
  production_project=""
fi

section "Release verification complete"
