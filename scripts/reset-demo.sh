#!/usr/bin/env bash

set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
project="$("$root/scripts/demo-project-name.sh")"
fingerprint="$("$root/scripts/demo-project-name.sh" --fingerprint)"
expected="RESET $project FROM $root"

if [[ -n "${COMPOSE_PROJECT_NAME:-}" ]]; then
  echo "Refusing inherited COMPOSE_PROJECT_NAME; this checkout always selects $project itself" >&2
  exit 2
fi

if [[ "${RESET_NIGHTINGALE_LOCAL_DEMO:-}" != "$fingerprint" ]]; then
  if [[ ! -t 0 ]]; then
    echo "A non-interactive reset requires RESET_NIGHTINGALE_LOCAL_DEMO=$fingerprint." >&2
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
export NIGHTINGALE_CHECKOUT_FINGERPRINT="$fingerprint"
"$root/scripts/assert-demo-project-ownership.sh" "$project"

docker compose --project-name "$project" \
  -f "$root/compose.yml" -f "$root/compose.override.yml" \
  down --volumes --remove-orphans
"$root/scripts/demo-up.sh"
