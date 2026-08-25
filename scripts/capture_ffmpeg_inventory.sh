#!/usr/bin/env bash

set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
project="${COMPOSE_PROJECT_NAME:-nightingale}"
output="$root/docs/evidence/ffmpeg-container-version.txt"
build=true

usage() {
  cat <<'EOF'
Usage: scripts/capture_ffmpeg_inventory.sh [--no-build] [--output PATH]

Builds the backend image unless --no-build is given, then runs `ffmpeg -version`
inside that image and saves the raw output in the release evidence path.
Host ffmpeg output is intentionally not accepted as release-container evidence.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-build)
      build=false
      shift
      ;;
    --output)
      [[ $# -ge 2 ]] || { echo "--output requires a path" >&2; exit 2; }
      output="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if ! command -v docker >/dev/null 2>&1 || ! docker compose version >/dev/null 2>&1; then
  echo "Docker with Compose is required to capture container evidence." >&2
  exit 1
fi
if [[ ! "$project" =~ ^[a-zA-Z0-9][a-zA-Z0-9_.-]*$ ]]; then
  echo "Invalid COMPOSE_PROJECT_NAME: $project" >&2
  exit 2
fi

cd "$root"
if [[ "$build" == true ]]; then
  docker compose --project-name "$project" build backend
fi

mkdir -p "$(dirname "$output")"
temp_file="$(mktemp "${TMPDIR:-/tmp}/nightingale-ffmpeg.XXXXXX")"
trap 'rm -f "$temp_file"' EXIT

docker compose --project-name "$project" run --rm --no-deps -T backend \
  ffmpeg -version >"$temp_file"

if ! grep -q '^ffmpeg version ' "$temp_file"; then
  echo "Container record did not contain an ffmpeg version header." >&2
  exit 1
fi
mv "$temp_file" "$output"
trap - EXIT

if command -v shasum >/dev/null 2>&1; then
  digest="$(shasum -a 256 "$output" | awk '{print $1}')"
else
  digest="$(sha256sum "$output" | awk '{print $1}')"
fi
printf 'Captured container FFmpeg evidence: %s\nSHA-256: %s\n' "$output" "$digest"
