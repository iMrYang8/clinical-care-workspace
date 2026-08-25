#!/usr/bin/env bash

set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
kind="${1:-}"
case "$kind" in
  verify|release|test) ;;
  *) echo "Usage: scripts/temporary-project-name.sh verify|release|test" >&2; exit 2 ;;
esac

fingerprint="$("$root/scripts/demo-project-name.sh" --fingerprint)"
if ! command -v openssl >/dev/null 2>&1; then
  echo "openssl is required for collision-resistant temporary project names." >&2
  exit 1
fi
suffix="$(openssl rand -hex 8)"
printf 'nightingale-%s-%s-%s\n' "$kind" "$fingerprint" "$suffix"
