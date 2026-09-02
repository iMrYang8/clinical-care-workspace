#!/usr/bin/env bash
# Copy the NightingaleSwitchCare package into the worker snapshot.
#
# Source of truth is the in-repo package at trilingual-consult/. A sibling
# checkout beside the repository is still honoured so an existing working copy
# keeps working, but the in-repo copy wins: it is the one a reviewer gets from
# a single clone.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
in_repo="$root/trilingual-consult/src/trilingual_consult"
sibling="$(cd "$root/.." && pwd)/trilingual-consult/src/trilingual_consult"
dst="$root/backend/app/services/voice/_sandbox/trilingual_consult"

if [[ -d "$in_repo" ]]; then
  src="$in_repo"
elif [[ -d "$sibling" ]]; then
  src="$sibling"
else
  echo "sandbox package not found in $in_repo or $sibling" >&2
  exit 1
fi

mkdir -p "$dst"
rsync -a --delete \
  --exclude '__pycache__/' \
  --exclude '*.pyc' \
  --exclude 'cli.py' \
  --exclude 'eval.py' \
  --exclude 'eval_audio.py' \
  --exclude 'report.py' \
  --exclude 'polywer.py' \
  --exclude 'audio_bench.py' \
  --exclude '__main__.py' \
  "$src/" "$dst/"

echo "synced $src -> $dst"
