#!/usr/bin/env bash

set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
first_file="$(mktemp "${TMPDIR:-/tmp}/nightingale-project-one.XXXXXX")"
second_file="$(mktemp "${TMPDIR:-/tmp}/nightingale-project-two.XXXXXX")"
fake_dir="$(mktemp -d "${TMPDIR:-/tmp}/nightingale-fake-docker.XXXXXX")"
cleanup() {
  rm -f "$first_file" "$second_file" "$fake_dir/docker"
  rmdir "$fake_dir" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

"$root/scripts/temporary-project-name.sh" verify >"$first_file" &
first_pid=$!
"$root/scripts/temporary-project-name.sh" verify >"$second_file" &
second_pid=$!
wait "$first_pid" "$second_pid"
first="$(cat "$first_file")"
second="$(cat "$second_file")"
fingerprint="$("$root/scripts/demo-project-name.sh" --fingerprint)"
[[ "$first" =~ ^nightingale-verify-${fingerprint}-[0-9a-f]{16}$ ]]
[[ "$second" =~ ^nightingale-verify-${fingerprint}-[0-9a-f]{16}$ ]]
[[ "$first" != "$second" ]]

cat >"$fake_dir/docker" <<'EOF'
#!/usr/bin/env bash
if [[ "${NIGHTINGALE_FAKE_DOCKER_OCCUPIED:-}" == "1" && "$1" == "network" ]]; then
  printf 'preexisting-network\n'
fi
EOF
chmod +x "$fake_dir/docker"

PATH="$fake_dir:$PATH" NIGHTINGALE_FAKE_DOCKER_OCCUPIED=0 \
  "$root/scripts/assert-compose-project-empty.sh" "$first"
if PATH="$fake_dir:$PATH" NIGHTINGALE_FAKE_DOCKER_OCCUPIED=1 \
  "$root/scripts/assert-compose-project-empty.sh" "$first" \
  >"$first_file" 2>&1; then
  echo "Occupied temporary Compose project was accepted." >&2
  exit 1
fi
grep -q "Refusing to reuse occupied Compose project" "$first_file"

echo "Parallel project names and preoccupied-project refusal verified."
