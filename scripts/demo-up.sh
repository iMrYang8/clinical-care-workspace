#!/usr/bin/env bash

set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
project="${COMPOSE_PROJECT_NAME:-nightingale}"
timeout="${NIGHTINGALE_START_TIMEOUT:-300}"

if ! command -v docker >/dev/null 2>&1; then
  echo "docker is required (Docker Desktop with Compose)." >&2
  exit 1
fi
if ! docker compose version >/dev/null 2>&1; then
  echo "docker compose is required." >&2
  exit 1
fi
if [[ ! "$project" =~ ^[a-zA-Z0-9][a-zA-Z0-9_.-]*$ ]]; then
  echo "Invalid COMPOSE_PROJECT_NAME: $project" >&2
  exit 2
fi
if [[ ! "$timeout" =~ ^[0-9]+$ ]] || [[ "$timeout" -lt 1 ]]; then
  echo "NIGHTINGALE_START_TIMEOUT must be a positive integer." >&2
  exit 2
fi

cd "$root"
docker compose --project-name "$project" up \
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
