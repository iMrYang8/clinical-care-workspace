#!/usr/bin/env bash
# Copy the sibling NightingaleSwitchCare package into the worker snapshot.
# Source of truth: ../trilingual-consult/src/trilingual_consult
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
src="$(cd "$root/.." && pwd)/trilingual-consult/src/trilingual_consult"
dst="$root/backend/app/services/voice/_sandbox/trilingual_consult"

if [[ ! -d "$src" ]]; then
  echo "sibling sandbox not found: $src" >&2
  exit 1
fi

mkdir -p "$dst"
rsync -a --delete \
  --exclude '__pycache__/' \
  --exclude '*.pyc' \
  --exclude 'cli.py' \
  --exclude 'eval.py' \
  --exclude 'report.py' \
  --exclude '__main__.py' \
  "$src/" "$dst/"

echo "synced $src -> $dst"
