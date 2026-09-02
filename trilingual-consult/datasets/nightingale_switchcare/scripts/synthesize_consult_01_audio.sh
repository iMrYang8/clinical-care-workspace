#!/usr/bin/env bash
# Synthetic TTS for consult-01. Not clinic audio. Not a quality claim.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
out="$root/datasets/nightingale_switchcare/audio/consult-01"
mkdir -p "$out"

say_bin="$(command -v say || true)"
afconvert_bin="$(command -v afconvert || true)"
espeak_bin="$(command -v espeak-ng || command -v espeak || true)"

write_manifest() {
  local status="$1"
  cat > "$out/manifest.json" <<EOF
{
  "consult_id": "consult-01",
  "synthetic": true,
  "not_clinical_validation": true,
  "tts_status": "$status",
  "sample_rate_hz": 16000,
  "turns": [
    {"speaker_id": "SPEAKER_00", "role": "clinician", "language": "en", "file": "SPEAKER_00.wav"},
    {"speaker_id": "SPEAKER_01", "role": "patient", "language": "zh", "file": "SPEAKER_01.wav"},
    {"speaker_id": "SPEAKER_02", "role": "family", "language": "ms", "file": "SPEAKER_02.wav"}
  ]
}
EOF
}

if [[ -n "$say_bin" && -n "$afconvert_bin" ]]; then
  tmp="$(mktemp -d)"
  "$say_bin" -v Samantha -r 160 -o "$tmp/en.aiff" "We'll continue metformin 500 milligrams twice daily."
  "$say_bin" -v Ting-Ting -r 160 -o "$tmp/zh.aiff" "我对盘尼西林不过敏，是胃不舒服。"
  # macOS has no Malay voice; English TTS of the Malay sentence, labelled in the manifest.
  "$say_bin" -v Samantha -r 160 -o "$tmp/ms.aiff" "Dia ada alahan kepada penicillin masa kecil."
  "$afconvert_bin" -f WAVE -d LEI16@16000 "$tmp/en.aiff" "$out/SPEAKER_00.wav"
  "$afconvert_bin" -f WAVE -d LEI16@16000 "$tmp/zh.aiff" "$out/SPEAKER_01.wav"
  "$afconvert_bin" -f WAVE -d LEI16@16000 "$tmp/ms.aiff" "$out/SPEAKER_02.wav"
  rm -rf "$tmp"
  write_manifest "macos_say"
  python3 - <<PY
import json
from pathlib import Path
path = Path("$out/manifest.json")
payload = json.loads(path.read_text())
payload["voice_notes"] = {
  "SPEAKER_00": "Samantha (en)",
  "SPEAKER_01": "Ting-Ting (zh)",
  "SPEAKER_02": "Samantha speaking Malay text; not a Malay voice"
}
path.write_text(json.dumps(payload, indent=2) + "\n")
PY
  echo "wrote $out (macos say)"
  exit 0
fi

if [[ -n "$espeak_bin" ]]; then
  "$espeak_bin" -v en -s 140 -w "$out/SPEAKER_00.wav" "We'll continue metformin 500 milligrams twice daily."
  "$espeak_bin" -v zh -s 140 -w "$out/SPEAKER_01.wav" "我对盘尼西林不过敏，是胃不舒服。"
  "$espeak_bin" -v en -s 140 -w "$out/SPEAKER_02.wav" "Dia ada alahan kepada penicillin masa kecil."
  write_manifest "espeak"
  echo "wrote $out (espeak)"
  exit 0
fi

write_manifest "TTS_UNAVAILABLE"
echo "TTS_UNAVAILABLE: install macOS say or espeak-ng" >&2
exit 0
