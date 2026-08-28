#!/usr/bin/env bash

set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
cd "$root"

if [[ -n "$(git status --porcelain --untracked-files=all)" ]]; then
  echo "Final demo recording requires a clean Git worktree so the captured image and metadata identify exact source." >&2
  git status --short >&2
  exit 1
fi

for command in docker curl node ffmpeg ffprobe magick python3; do
  if ! command -v "$command" >/dev/null 2>&1; then
    echo "$command is required for the English final demo workflow." >&2
    exit 1
  fi
done
if ! docker compose version >/dev/null 2>&1; then
  echo "docker compose is required." >&2
  exit 1
fi
if [[ ! -f "$root/node_modules/playwright/package.json" ]]; then
  echo "Playwright dependencies are missing; install the frozen workspace dependencies first." >&2
  exit 1
fi

# The capture is deterministic and synthetic. Do not inherit an operator's
# local provider key or remote-egress configuration into the isolated stack.
unset OPENAI_API_KEY OPENAI_EXTRACT_MODEL OPENAI_REVIEW_MODEL OPENAI_TRANSCRIBE_MODEL
export AI_PROVIDER=deterministic
export REMOTE_TEXT_EGRESS_ENABLED=false
export VOICE_TRANSCRIPTION_PROVIDER=disabled
export REMOTE_AUDIO_EGRESS_ENABLED=false
export STRICT_NO_AUDIO_EGRESS=true

revision="$(git rev-parse HEAD)"
short="$(git rev-parse --short=12 HEAD)"
project="nightingale-en-demo-${short}-$$"
http_port="${LOCAL_HTTP_PORT:-$(python3 "$root/scripts/free-local-port.py")}" 
https_port="${LOCAL_HTTPS_PORT:-$(python3 "$root/scripts/free-local-port.py")}" 
mailpit_port="${LOCAL_MAILPIT_PORT:-$(python3 "$root/scripts/free-local-port.py")}" 
while [[ "$https_port" == "$http_port" ]]; do
  https_port="$(python3 "$root/scripts/free-local-port.py")"
done
while [[ "$mailpit_port" == "$http_port" || "$mailpit_port" == "$https_port" ]]; do
  mailpit_port="$(python3 "$root/scripts/free-local-port.py")"
done
base_url="https://localhost:${https_port}"
timeout="${NIGHTINGALE_START_TIMEOUT:-300}"
raw="${NIGHTINGALE_DEMO_RAW_VIDEO:-$root/output/demo/Nightingale_Final_Demo_EN_raw.webm}"
recording_manifest="${NIGHTINGALE_DEMO_RECORDING_MANIFEST:-${raw}.recording.json}"

export LOCAL_HTTP_PORT="$http_port"
export LOCAL_HTTPS_PORT="$https_port"
export LOCAL_MAILPIT_PORT="$mailpit_port"
export NIGHTINGALE_SOURCE_COMMIT="$revision"
export NIGHTINGALE_CHECKOUT_FINGERPRINT="english-demo-${short}-$$"
recording_image="${NIGHTINGALE_RECORDING_IMAGE:-}"
if [[ -n "$recording_image" ]]; then
  image_revision="$(docker image inspect --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}' "$recording_image")"
  if [[ "$image_revision" != "$revision" ]]; then
    echo "Recording image revision $image_revision does not match checkout $revision." >&2
    exit 1
  fi
  export NIGHTINGALE_BACKEND_IMAGE="$recording_image"
else
  export NIGHTINGALE_BACKEND_IMAGE="nightingale-demo-en:${short}"
fi

compose=(
  docker compose --project-name "$project"
  -f "$root/compose.yml" -f "$root/compose.override.yml"
)

cleanup() {
  if [[ "${NIGHTINGALE_DEMO_KEEP_PROJECT:-0}" == "1" ]]; then
    echo "Isolated Compose project preserved: $project" >&2
    return
  fi
  "${compose[@]}" down --volumes --remove-orphans >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

echo "Starting isolated synthetic recording project: $project"
echo "Browser endpoint: $base_url"
if [[ -n "$recording_image" ]]; then
  echo "Reusing verified content-addressed image: $recording_image"
  "${compose[@]}" up --no-build --detach --wait --wait-timeout "$timeout" \
    proxy db prestart backend ai-worker mailpit
else
  "${compose[@]}" up --build --detach --wait --wait-timeout "$timeout" \
    proxy db prestart backend ai-worker mailpit
fi

ready_url="$base_url/api/v1/utils/health-check/"
deadline=$((SECONDS + timeout))
until [[ "$(curl --insecure --silent --show-error --fail --max-time 2 "$ready_url" 2>/dev/null || true)" == "true" ]]; do
  if ((SECONDS >= deadline)); then
    echo "The isolated recording application did not become ready." >&2
    "${compose[@]}" logs --tail=120 proxy backend >&2
    exit 1
  fi
  sleep 1
done

backend_container="$("${compose[@]}" ps -q backend)"
image_digest="$(docker inspect --format '{{.Image}}' "$backend_container")"
if [[ -z "$image_digest" || "$image_digest" != sha256:* ]]; then
  echo "Could not resolve the immutable backend image digest." >&2
  exit 1
fi

node "$root/scripts/demo/generate_english_demo_assets.mjs"
rm -f "$raw" "$recording_manifest"

BASE_URL="$base_url" \
DEMO_MUTATE="${DEMO_MUTATE:-1}" \
DEMO_SPEED="${DEMO_SPEED:-1}" \
STRICT_DEMO="${STRICT_DEMO:-1}" \
NIGHTINGALE_RUNTIME_REVISION="$revision" \
NIGHTINGALE_IMAGE_DIGEST="$image_digest" \
NIGHTINGALE_DEMO_RAW_VIDEO="$raw" \
NIGHTINGALE_DEMO_RECORDING_MANIFEST="$recording_manifest" \
  node "$root/scripts/record_final_demo_en.mjs"

if [[ "${NIGHTINGALE_RENDER_FINAL:-1}" == "1" ]]; then
  speed="${DEMO_SPEED:-1}"
  if [[ "$speed" != "1" ]]; then
    echo "Final rendering requires DEMO_SPEED=1; use NIGHTINGALE_RENDER_FINAL=0 for a fast smoke run." >&2
    exit 2
  fi
  pre_roll="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["pre_roll_seconds"])' "$recording_manifest")"
  NIGHTINGALE_RAW_VIDEO="$raw" \
  NIGHTINGALE_DEMO_RECORDING_MANIFEST="$recording_manifest" \
  NIGHTINGALE_PRE_ROLL="$pre_roll" \
  NIGHTINGALE_IMAGE_DIGEST="$image_digest" \
  NIGHTINGALE_RECORDING_PROJECT="$project" \
    node "$root/scripts/demo/render_english_demo.mjs"
  node "$root/scripts/demo/generate_english_demo_assets.mjs" --check
  if [[ "${NIGHTINGALE_KEEP_RAW:-0}" != "1" ]]; then
    rm -f "$raw" "$recording_manifest"
  fi
fi

echo "English final demo workflow completed."
