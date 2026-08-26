#!/usr/bin/env bash

set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
project="$($root/scripts/demo-project-name.sh)"

if [[ -n "${COMPOSE_PROJECT_NAME:-}" ]]; then
  echo "Refusing inherited COMPOSE_PROJECT_NAME; this checkout selects $project." >&2
  exit 2
fi

python3 "$root/scripts/download_test_datasets.py"

cd "$root"
export NIGHTINGALE_SOURCE_COMMIT="$(git rev-parse HEAD)"
export NIGHTINGALE_CHECKOUT_FINGERPRINT="$($root/scripts/demo-project-name.sh --fingerprint)"
docker compose --project-name "$project" \
  -f "$root/compose.yml" -f "$root/compose.override.yml" \
  run --rm --no-deps --build \
  -v "$root/datasets/raw:/app/datasets/raw:ro" \
  backend python -m app.import_evaluation_data "$@"

docker compose --project-name "$project" \
  -f "$root/compose.yml" -f "$root/compose.override.yml" \
  up --detach --no-build backend ai-worker
