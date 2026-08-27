#!/usr/bin/env bash

set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
project="$("$root/scripts/demo-project-name.sh")"
timeout="${NIGHTINGALE_START_TIMEOUT:-300}"

# Keep provider credentials outside the tracked fixture .env. Export an
# optional local override before Compose resolves service environment values.
if [[ -f "$root/.env.local" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$root/.env.local"
  set +a
fi

if [[ -n "${COMPOSE_PROJECT_NAME:-}" ]]; then
  echo "Refusing inherited COMPOSE_PROJECT_NAME; this checkout always selects $project itself" >&2
  exit 2
fi

if ! command -v docker >/dev/null 2>&1; then
  echo "docker is required (Docker Desktop with Compose)." >&2
  exit 1
fi
if ! docker compose version >/dev/null 2>&1; then
  echo "docker compose is required." >&2
  exit 1
fi
if ! command -v curl >/dev/null 2>&1; then
  echo "curl is required for the local HTTPS readiness check." >&2
  exit 1
fi
"$root/scripts/assert-demo-project-ownership.sh" "$project"
if [[ ! "$timeout" =~ ^[0-9]+$ ]] || [[ "$timeout" -lt 1 ]]; then
  echo "NIGHTINGALE_START_TIMEOUT must be a positive integer." >&2
  exit 2
fi

cd "$root"
export NIGHTINGALE_SOURCE_COMMIT="$(git rev-parse HEAD)"
export NIGHTINGALE_CHECKOUT_FINGERPRINT="$("$root/scripts/demo-project-name.sh" --fingerprint)"
docker compose --project-name "$project" \
  -f "$root/compose.yml" -f "$root/compose.override.yml" up \
  --build --detach --wait --wait-timeout "$timeout" \
  proxy db prestart backend ai-worker mailpit

# A healthy backend can precede Traefik's dynamic Docker route by a few
# seconds. Do not tell the operator the workspace is ready until the actual
# browser-facing HTTPS route answers through the proxy.
ready_url="https://localhost/api/v1/utils/health-check/"
deadline=$((SECONDS + timeout))
until [[ "$(curl --insecure --silent --show-error --fail --max-time 2 "$ready_url" 2>/dev/null || true)" == "true" ]]; do
  if ((SECONDS >= deadline)); then
    echo "Nightingale did not become reachable at $ready_url within ${timeout}s." >&2
    docker compose --project-name "$project" \
      -f "$root/compose.yml" -f "$root/compose.override.yml" \
      logs --tail=120 proxy backend >&2
    exit 1
  fi
  sleep 1
done

cat <<EOF

Nightingale local workspace is ready:
  https://localhost

Compose project: $project
The local Traefik certificate is generated/self-signed; the first browser visit
will require accepting a certificate warning. Production does not use the
development demo-auth/fixture override.
EOF
