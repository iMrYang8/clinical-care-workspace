#!/usr/bin/env bash

set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
project="$("$root/scripts/demo-project-name.sh")"
timeout="${NIGHTINGALE_START_TIMEOUT:-300}"

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

cat <<EOF

Nightingale synthetic demo is ready:
  https://localhost

Compose project: $project
The local Traefik certificate is generated/self-signed; the first browser visit
will require accepting a certificate warning. Production does not use the
development demo-auth/fixture override.
EOF
