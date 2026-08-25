#!/usr/bin/env bash

set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"

for wrapper in "$root/scripts/test.sh" "$root/scripts/test-local.sh"; do
  grep -q 'verify-release.sh' "$wrapper"
  if grep -Eq 'docker(-compose| compose).*down|--volumes|-v[[:space:]]|sudo' "$wrapper"; then
    echo "Unsafe legacy cleanup remains in $wrapper" >&2
    exit 1
  fi
done

if grep -R -n -E 'sudo[[:space:]]+find|rm[[:space:]]+-rf|docker(-compose| compose)[^#]*down[[:space:]]+-v' \
  "$root/scripts" --include='*.sh'; then
  echo "A prohibited broad/destructive test-script pattern remains." >&2
  exit 1
fi

"$root/scripts/test-project-isolation.sh"
echo "Test entrypoints use isolated release verification without host cleanup."
