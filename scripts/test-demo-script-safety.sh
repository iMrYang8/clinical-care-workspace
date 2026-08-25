#!/usr/bin/env bash

set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
project_one="$("$root/scripts/demo-project-name.sh")"
project_two="$("$root/scripts/demo-project-name.sh")"
fingerprint="$("$root/scripts/demo-project-name.sh" --fingerprint)"
output="$(mktemp "${TMPDIR:-/tmp}/nightingale-demo-safety.XXXXXX")"
cleanup() {
  rm -f "$output"
}
trap cleanup EXIT INT TERM

[[ "$project_one" == "$project_two" ]]
[[ "$project_one" == "nightingale-demo-$fingerprint" ]]
[[ "$fingerprint" =~ ^[0-9a-f]{12}$ ]]

# Wrong project overrides must fail before any Docker command can run.
set +e
COMPOSE_PROJECT_NAME=unrelated-project "$root/scripts/demo-up.sh" \
  >"$output" 2>&1
status=$?
set -e
if [[ "$status" -eq 0 ]]; then
  echo "demo-up accepted an unrelated project override" >&2
  exit 1
fi
[[ "$status" -eq 2 ]]
# The override is rejected before Docker availability or project state is read.
! grep -q 'docker compose' "$output"

# Static destructive-boundary contracts: path-bound label validation, no
# deploy file, and exact non-interactive fingerprint confirmation.
grep -q 'assert-demo-project-ownership.sh' "$root/scripts/demo-up.sh"
grep -q 'assert-demo-project-ownership.sh' "$root/scripts/reset-demo.sh"
grep -q 'com.docker.compose.project.working_dir' "$root/scripts/assert-demo-project-ownership.sh"
grep -q 'com.docker.compose.project.config_files' "$root/scripts/assert-demo-project-ownership.sh"
grep -q 'com.nightingale.checkout_fingerprint' "$root/scripts/assert-demo-project-ownership.sh"
grep -q 'compose.deploy.yml' "$root/scripts/assert-demo-project-ownership.sh"
grep -q 'RESET_NIGHTINGALE_LOCAL_DEMO' "$root/scripts/reset-demo.sh"
grep -q -- '-f "$root/compose.override.yml"' "$root/scripts/reset-demo.sh"

echo "Path-bound local demo reset safety verified for $project_one"
