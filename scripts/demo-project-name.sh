#!/usr/bin/env bash

set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
if command -v shasum >/dev/null 2>&1; then
  fingerprint="$(printf '%s' "$root" | shasum -a 256 | awk '{print substr($1,1,12)}')"
else
  fingerprint="$(printf '%s' "$root" | sha256sum | awk '{print substr($1,1,12)}')"
fi
project="nightingale-demo-${fingerprint}"

case "${1:-}" in
  "") printf '%s\n' "$project" ;;
  --fingerprint) printf '%s\n' "$fingerprint" ;;
  --root) printf '%s\n' "$root" ;;
  *) echo "Usage: scripts/demo-project-name.sh [--fingerprint|--root]" >&2; exit 2 ;;
esac
