#!/usr/bin/env bash
# Add cue-aligned English narration to the seven per-scenario recordings.
#
# The clips in output/demo/scenarios are recorded silent and stay that way:
# this reads them, writes a sidecar SRT and a voiced copy into
# output/demo/scenarios/voiced, and copies the original video stream through
# untouched, so the footage the check gated is the footage that gets narrated.
#
#   ./scripts/demo/voice_scenarios.sh                    all seven
#   ./scripts/demo/voice_scenarios.sh 13-allergy-vs-nkda one clip
#
# Requires macOS `say`, ffmpeg, ffprobe and node. No network, no paid TTS.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

VIDEO_DIR="${NIGHTINGALE_SCENARIO_OUTPUT_DIR:-$ROOT/output/demo/scenarios}"
OUT_DIR="${NIGHTINGALE_SCENARIO_VOICE_DIR:-$VIDEO_DIR/voiced}"
VOICE="${NIGHTINGALE_NARRATION_VOICE:-Samantha}"
RATE="${NIGHTINGALE_NARRATION_RATE:-220}"
PYTHON="${NIGHTINGALE_PYTHON:-$ROOT/.venv/bin/python}"
[ -x "$PYTHON" ] || PYTHON="python3"

for tool in say ffmpeg ffprobe node; do
  command -v "$tool" >/dev/null 2>&1 || {
    echo "Required command is missing: $tool" >&2
    exit 1
  }
done

# One source of truth for spoken line and subtitle line. This fails loudly if a
# clip was re-recorded to a length the cues were not written against.
node scripts/demo/generate_scenario_captions.mjs

ids=("$@")
if [ ${#ids[@]} -eq 0 ]; then
  ids=(
    02-clinic-isolation
    05-clinic-b-onboarding
    09-provider-outage
    10-concurrent-edits
    12-medication-gate
    13-allergy-vs-nkda
    14-meaningful-numbers
  )
fi

cache_root="$(mktemp -d "${TMPDIR:-/tmp}/nightingale-scenario-voice-XXXXXX")"
trap 'rm -rf "$cache_root"' EXIT

for id in "${ids[@]}"; do
  name="Nightingale_Scenario_${id}"
  echo
  echo "=== ${name} ==="
  "$PYTHON" scripts/demo/render_samantha_voiceover.py \
    --video "$VIDEO_DIR/${name}.mp4" \
    --srt "$OUT_DIR/${name}.srt" \
    --output "$OUT_DIR/${name}_Voiced.mp4" \
    --narration-audio "$OUT_DIR/${name}_Voiced.m4a" \
    --metadata "$OUT_DIR/${name}_Voiced_metadata.json" \
    --sha-file "$OUT_DIR/${name}_Voiced_SHA256.txt" \
    --cache-dir "$cache_root/$id" \
    --voice "$VOICE" \
    --rate "$RATE" \
    --subtitle-track "sidecar SRT beside the clip"
done

echo
echo "Voiced clips: $OUT_DIR"
