# Narrated scenario recordings

Narrated copies of the seven silent clips in the parent directory. The originals
are untouched: each `_Voiced.mp4` here carries the same video stream copied
through with `-c:v copy` (byte-identical, verified with an ffmpeg stream MD5)
plus one AAC narration track at 48 kHz stereo.

Regenerate everything in this directory with:

```bash
./scripts/demo/voice_scenarios.sh
```

| Scenario | Voiced clip | Length | Cues | Words |
|---|---|---|---|---|
| 2 · Tenant isolation | `Nightingale_Scenario_02-clinic-isolation_Voiced.mp4` | 22.0s | 5 | 49 |
| 5 · Clinic B onboarding | `Nightingale_Scenario_05-clinic-b-onboarding_Voiced.mp4` | 29.0s | 8 | 65 |
| 9 · Provider 503 | `Nightingale_Scenario_09-provider-outage_Voiced.mp4` | 16.0s | 4 | 30 |
| 10 · Concurrent edits | `Nightingale_Scenario_10-concurrent-edits_Voiced.mp4` | 27.6s | 7 | 58 |
| 12 · Medication gate | `Nightingale_Scenario_12-medication-gate_Voiced.mp4` | 30.4s | 8 | 50 |
| 13 · Allergy vs NKDA | `Nightingale_Scenario_13-allergy-vs-nkda_Voiced.mp4` | 106.1s | 28 | 230 |
| 14 · Meaningful numbers | `Nightingale_Scenario_14-meaningful-numbers_Voiced.mp4` | 26.3s | 7 | 53 |

## Files per scenario

- `..._Voiced.mp4` — the narrated clip
- `..._Voiced.m4a` — the narration track on its own
- `....srt` — the spoken lines, timed; the exact source the voice reads
- `..._Voiced_metadata.json` — cue count, tempo ratios, spoken seconds, SHA-256
  of the source video, SRT, narration and output
- `..._Voiced_SHA256.txt` — the manifest for those four files

## How the voice is made

macOS `say` with the Samantha voice at 220 wpm, one invocation per subtitle cue,
each clip fitted inside its own cue window and laid down at that cue's start.
No network call and no paid TTS. `max_tempo_ratio` is 1.000 for all seven, so no
line was sped up to fit.

Cue text lives in `scripts/demo/scenario_narration.mjs`; see
`docs/SCENARIO_RECORDINGS.md` for the acceptance rules and the honesty notes.
