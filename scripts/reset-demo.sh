#!/usr/bin/env bash

set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
project="${COMPOSE_PROJECT_NAME:-nightingale}"
expected="RESET $project"

if [[ ! "$project" =~ ^[a-zA-Z0-9][a-zA-Z0-9_.-]*$ ]]; then
  echo "Invalid COMPOSE_PROJECT_NAME: $project" >&2
  exit 2
fi

if [[ "${NIGHTINGALE_RESET_CONFIRM:-}" != "YES" ]]; then
  if [[ ! -t 0 ]]; then
    echo "A non-interactive reset requires NIGHTINGALE_RESET_CONFIRM=YES." >&2
    exit 2
  fi
  cat <<EOF
This recreates only Docker Compose project '$project':
  - its project containers and networks
  - its declared/anonymous Compose volumes, including the synthetic demo DB

It does not delete repository files, other Compose projects, or host disks.
Type exactly: $expected
EOF
  read -r confirmation
  if [[ "$confirmation" != "$expected" ]]; then
    echo "Confirmation did not match; nothing changed." >&2
    exit 2
  fi
fi

cd "$root"
docker compose --project-name "$project" down --volumes --remove-orphans
COMPOSE_PROJECT_NAME="$project" "$root/scripts/demo-up.sh"
