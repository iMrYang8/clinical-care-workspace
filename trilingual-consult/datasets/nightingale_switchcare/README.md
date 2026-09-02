# NightingaleSwitchCare (sandbox gold)

Synthetic, evaluation-only consults for SEA trilingual code-switching. **Not** clinical validation and **not** Nightingale runtime output.

Inspired by AfriSwitchCare (simulated CS consults, `[[EN]]` span tags, CMI) and ViMedCSS (English medical terms as embedded islands). Source scripts are rewritten from PriMock57 mock GP consultations (CC BY 4.0). Keep PriMock57 attribution on any redistributed derivative.

## Family

| Id | What it proves |
|---|---|
| `consult-01` | Reporting specimen. Clinician EN metformin; patient ZH penicillin denial; family MS penicillin allergy (hearsay). Conflict does not auto-resolve. Publish blocked. |
| `consult-02-unlabeled` | Same dialogue with `speaker_role` withheld. Attribution must infer clinician / patient / family. |
| `consult-03-asr-noise` | Simulated ASR (`penisilin`, `kechil`) on the family turn. Canonical key is still `penicillin`. Quotes may follow the hypothesis, not the gold wording. |
| `consult-04-hokkien` | Empty/unsupported Hokkien (`ok lah`) must not become NKDA. A POJ cue (`tui penicillin koe-bin`) may extract an allergy. |
| `consult-05-dose-correction` | Clinician says metformin 5000 mg then 500 mg. Out-of-range + dose conflict. Same-speaker “correction” still does not auto-resolve. |
| `consult-06-overlap` | Shared `overlap_group_id`. Family role unlabeled. Every fact from the overlap group is `review_required`. |
| `consult-07-intrasentential` | **Q6 sentence.** One family utterance: Malay matrix + English `penicillin` + Hokkien `tui … koe-bin`. Spans must include `ms`, `en`, and `nan`. Hearsay, `review_required`, no false NKDA. |

## consult-01 (specimen)

| Speaker | Role | Matrix language | What they say |
|---|---|---|---|
| SPEAKER_00 | clinician | English | Continue metformin 500 mg twice daily |
| SPEAKER_01 | patient | Mandarin | Denies penicillin allergy; stomach discomfort |
| SPEAKER_02 | family | Malay | Third-person: she had a penicillin allergy as a child |

Drug names stay English inside Malay/Chinese grammar (Matrix Language Frame). Family allergy is hearsay: `review_required`, conflicts with the patient denial, must not auto-resolve, must not vanish from the patient-language summary.

## CMI / switch points

Token-level Code-Mixing Index after Das & Gambäck (2014): share of tokens whose language tag is not the utterance majority, plus adjacent-token switch counts. Computed from `tagged_text` `[[EN]]…[[/EN]]` wrappers. Quoted numbers are gold-set statistics, not ASR quality.

Eval (`python -m trilingual_consult.eval`) scores role accuracy, fact precision/recall, conflict recall, and fail-closed invariants against `expected/`. It is a gold-text extraction eval, not WER, not PolyWER.

PolyWER (`python -m trilingual_consult.eval_audio`) is optional and fail-closed: it needs synthetic TTS of *these* scripts plus an ASR hypothesis. Missing wavs → `TTS_UNAVAILABLE`. Missing ASR → `ASR_UNAVAILABLE`. It does not score PriMock57 or invent a WER.

## What this set is not

- Not Singapore Hokkien audio (Taiwanese Common Voice / TAT ≠ SG Hokkien).
- Not IMDA NSC or SEAME (those are conversational CS, not medical consults).
- Not a public ms/en/zh/nan medical speech corpus — none exists; that absence is a finding.
