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
grep -q 'git status --porcelain --untracked-files=all' \
  "$root/scripts/verify-release.sh"
grep -q 'release-verification-complete.txt' "$root/scripts/verify-release.sh"
grep -q 'validate_release_evidence.py' "$root/scripts/package-release.sh"
python3 "$root/scripts/test_release_evidence.py"
echo "Test entrypoints use isolated release verification without host cleanup."
