#!/usr/bin/env bash

set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
project="$("$root/scripts/demo-project-name.sh")"
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
if [[ -n "${COMPOSE_PROJECT_NAME:-}" ]]; then
  echo "Refusing inherited COMPOSE_PROJECT_NAME; this checkout always selects $project itself" >&2
  exit 2
fi

cd "$root"
commit="$(git rev-parse HEAD)"
if [[ -n "$(git status --porcelain --untracked-files=all)" ]]; then
  echo "FFmpeg release evidence requires a clean Git worktree." >&2
  exit 2
fi
export NIGHTINGALE_SOURCE_COMMIT="$commit"
export NIGHTINGALE_CHECKOUT_FINGERPRINT="$("$root/scripts/demo-project-name.sh" --fingerprint)"
"$root/scripts/assert-demo-project-ownership.sh" "$project"
if [[ "$build" == true ]]; then
  docker compose --project-name "$project" \
    -f "$root/compose.yml" -f "$root/compose.override.yml" build backend
fi

image_ref="$(docker compose --project-name "$project" \
  -f "$root/compose.yml" -f "$root/compose.override.yml" \
  config --images | grep '^backend:latest$' | head -n 1)"
if [[ -z "$image_ref" ]]; then
  echo "Compose did not resolve the expected backend image." >&2
  exit 1
fi
image_id="$(docker image inspect --format '{{.Id}}' "$image_ref" 2>/dev/null || true)"
if [[ -z "$image_id" ]]; then
  echo "No backend image exists for the current checkout." >&2
  exit 1
fi
image_commit="$(docker image inspect --format \
  '{{ index .Config.Labels "org.opencontainers.image.revision" }}' "$image_id")"
immutable_image_id="$(docker image inspect --format '{{.Id}}' "$image_id")"
if [[ "$image_commit" != "$commit" ]]; then
  echo "Refusing stale backend image: label '$image_commit' does not match commit '$commit'." >&2
  exit 1
fi

mkdir -p "$(dirname "$output")"
temp_file="$(mktemp "${TMPDIR:-/tmp}/nightingale-ffmpeg.XXXXXX")"
trap 'rm -f "$temp_file"' EXIT

{
  printf 'nightingale_source_commit=%s\n' "$commit"
  printf 'backend_image_id=%s\n' "$immutable_image_id"
  printf 'backend_image_revision_label=%s\n' "$image_commit"
  # Run the exact content-addressed object inspected above. Never resolve the
  # mutable Compose tag a second time after the revision-label check (TOCTOU).
  docker run --rm --entrypoint ffmpeg "$immutable_image_id" -version
} >"$temp_file"

if ! grep -q '^ffmpeg version ' "$temp_file" || \
   ! grep -q "^nightingale_source_commit=$commit$" "$temp_file"; then
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
