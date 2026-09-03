# Nightingale demo documentation

The submission demo is the seven per-scenario recordings in
`output/demo/scenarios/voiced/`, one clip per clinic scenario, each with
narration and a matching `.srt`:

| Scenario | Clip | Length |
|---|---|---|
| 2 · Tenant isolation | `Nightingale_Scenario_02-clinic-isolation_Voiced.mp4` | 22s |
| 5 · Clinic B onboarding | `Nightingale_Scenario_05-clinic-b-onboarding_Voiced.mp4` | 29s |
| 9 · Provider 503 | `Nightingale_Scenario_09-provider-outage_Voiced.mp4` | 16s |
| 10 · Concurrent edits | `Nightingale_Scenario_10-concurrent-edits_Voiced.mp4` | 28s |
| 12 · Medication gate | `Nightingale_Scenario_12-medication-gate_Voiced.mp4` | 30s |
| 13 · Allergy vs NKDA | `Nightingale_Scenario_13-allergy-vs-nkda_Voiced.mp4` | 106s |
| 14 · Meaningful numbers | `Nightingale_Scenario_14-meaningful-numbers_Voiced.mp4` | 26s |

Four minutes of footage in total. Every clip is gated before it exists:
`record_scenarios.mjs --check` walks the same click path with capture off and
asserts each declared proof string is on screen, so a scenario that cannot prove
itself produces no video. Narration is added afterwards by
`voice_scenarios.sh` with `-c:v copy`, leaving the video stream of a voiced copy
byte-identical to the gated original. The silent originals stay beside them in
`output/demo/scenarios/`.

Recording commands, the per-clip proof strings, the narration acceptance rules,
and the scope notes — including which scenarios were deliberately not filmed and
why — are in [`SCENARIO_RECORDINGS.md`](./SCENARIO_RECORDINGS.md).

For each scenario's automated test coverage, see
[`SCENARIO_TEST_MAP.md`](./SCENARIO_TEST_MAP.md).

## Superseded

`output/demo/Nightingale_Final_Demo_EN*.mp4` was a thirteen-chapter product
walkthrough recorded on 28 August. Twenty-two commits have landed since,
including the trust fail-closed work, the server-derived confidence state, the
medication supersede rule, and both patient-portal defects that filming
scenario 13 uncovered. It shows none of them and is **not** part of this
submission. It is kept only as a local reference and is excluded from Git.

The earlier Chinese script is likewise a local rollback reference:
[`DEMO_SCRIPT.zh-CN.md`](./DEMO_SCRIPT.zh-CN.md) — four-role core workflow,
trust explanation, and Bonus presentation with explicit claim boundaries.

Challenge-specific automation, release evidence, benchmark commands, and the
historical recording workflow remain under
[`docs/delivery/DEMO_RUNBOOK.md`](./delivery/DEMO_RUNBOOK.md).

For day-to-day product setup, start with the root [`README.md`](../README.md).
