#!/usr/bin/env bash

set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
bun_bin="${BUN_BIN:-$(command -v bun || true)}"

if [[ -z "$bun_bin" || ! -x "$bun_bin" ]]; then
  echo "Bun is required. Set BUN_BIN to the Bun executable." >&2
  exit 1
fi
for command in docker ffmpeg ffprobe; do
  if ! command -v "$command" >/dev/null 2>&1; then
    echo "$command is required for the reproducible demo recording." >&2
    exit 1
  fi
done

cd "$root"
"$bun_bin" install --frozen-lockfile
"$root/node_modules/.bin/playwright" install chromium

https_port="${LOCAL_HTTPS_PORT:-443}"
if [[ -z "${PLAYWRIGHT_BASE_URL:-}" ]]; then
  if [[ "$https_port" == "443" ]]; then
    export PLAYWRIGHT_BASE_URL="https://localhost"
  else
    export PLAYWRIGHT_BASE_URL="https://localhost:$https_port"
  fi
fi
if [[ "$https_port" != "443" && -z "${NIGHTINGALE_DEMO_TRUSTED_ORIGIN:-}" ]]; then
  # The local backend deliberately trusts the canonical TLS origin. When a
  # checkout is mapped to a non-standard host port, Playwright mirrors that
  # canonical Origin on mutations; response bodies are never intercepted.
  export NIGHTINGALE_DEMO_TRUSTED_ORIGIN="https://localhost"
fi

if [[ "${NIGHTINGALE_RECORD_KEEP_STATE:-0}" == "1" ]]; then
  "$root/scripts/demo-up.sh"
else
  RESET_NIGHTINGALE_LOCAL_DEMO="$("$root/scripts/demo-project-name.sh" --fingerprint)" \
    "$root/scripts/reset-demo.sh"
fi

"$bun_bin" "$root/scripts/record_demo.ts"

ffprobe -v error \
  -show_entries stream=codec_name,width,height:format=duration,size \
  -of default=noprint_wrappers=1 \
  "$root/output/demo/Nightingale_Demo.mp4"
