# Per-scenario demo recordings

The rendered mp4 files live under `output/demo/scenarios/`, which is a generated
directory and is not committed. Regenerate them with the commands below.

Seven silent screen recordings, one per clinic scenario, all captured from the
running application on `feat/voice-multi-agent-pipeline` against a freshly
seeded stack. No voiceover and no burned-in subtitles; the written script for
each clip sits beside it as a `.md` file.

Every clip is gated. `scripts/demo/record_scenarios.mjs --check` runs the exact
same click path with video off and asserts that each declared proof string is
actually on screen. A scenario that cannot prove itself produces no video, so
the footage cannot drift from the claim.

| Scenario | Clip | Length | What it proves |
|---|---|---|---|
| 2 · Tenant isolation | `Nightingale_Scenario_02-clinic-isolation.mp4` | 22s | The same MRN search returns a record in one clinic and `0 matching patient records` in the other; platform oversight is `Read-only clinic view` |
| 5 · Clinic B onboarding | `Nightingale_Scenario_05-clinic-b-onboarding.mp4` | 29s | Preflight names the unmet requirement (`calibration: calibration gate required`), flips `Action required` → `Ready`, and the action becomes `Create clinic` |
| 9 · Provider 503 | `Nightingale_Scenario_09-provider-outage.mp4` | 16s | Stored priorities stay readable and are labelled `stored priorities remain visible` + `Last generated N minutes ago` |
| 10 · Concurrent edits | `Nightingale_Scenario_10-concurrent-edits.mp4` | 28s | The second session is warned before it saves; `Version conflict` with three panes and `No automatic merge was applied` |
| 12 · Medication gate | `Nightingale_Scenario_12-medication-gate.mp4` | 30s | Two source-linked medication instructions disagree, `Status: unresolved`, both sources addressable, staff cannot dismiss |
| 13 · Allergy vs NKDA | `Nightingale_Scenario_13-allergy-vs-nkda.mp4` | 106s | The nurse documents penicillin allergy, the patient denies any allergy in her own portal, and the clinician sees `Penicillin Allergy` `critical` with `Source: staff` vs `Source: patient` and the NKDA precedence rule |
| 14 · Meaningful numbers | `Nightingale_Scenario_14-meaningful-numbers.mp4` | 26s | Confidence states its own qualification, `Why this decision?` explains falsifiability and consequence, protected `Needs clinical review` queue |

Format: 1440x900, H.264, 30fps, zero audio streams (asserted by ffprobe at
render time).

## Reproducing

```bash
RESET_NIGHTINGALE_LOCAL_DEMO="$(./scripts/demo-project-name.sh --fingerprint)" ./scripts/reset-demo.sh
BASE_URL=https://localhost node scripts/demo/record_scenarios.mjs --check   # gate
BASE_URL=https://localhost node scripts/demo/record_scenarios.mjs           # record
```

`--only=<id>` records a single scenario.

## Scope and honesty notes

- **Scenario 13 films the real patient-versus-nurse path.** Building this
  recording surfaced two defects that blocked it, both since fixed: a patient
  note that produced a clinical fact returned a 500 because the provenance
  write check queried the row it was inserting, and the unresolved-conflict
  gate refused the patient's own statement instead of only blocking outbound
  sharing. Both have regression tests.
- **Scenario 9 needs a fixture.** No seeded outage exists and there is no
  env flag or test-only route, so the take opens the provider circuit directly,
  exactly as a real 503 would, and closes it again afterwards. The per-card
  `Rule-derived · review required` badge additionally needs a real rule-derived
  assessment and is not shown.
- The clips cover scenarios the build genuinely survives. Scenarios 1, 3, 4, 6,
  7, 8, 11, 15 and 16 are partial or not visually demonstrable and were
  deliberately not filmed.
