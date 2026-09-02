# trilingual-consult

Isolated multi-agent sandbox for the SEA trilingual consult case (Malay + English + Hokkien/Mandarin inside one visit). **Sibling of `nightingale/`** — this package does not import or edit the main app.

This is synthetic gold, not Nightingale runtime, not a clinical-quality claim, and not a public speech corpus. A public ms/en/zh/nan *medical consult* speech set does not exist; that absence is a finding.

## Agents (proposals only)

```text
Scribe → Attribution → Safety Sentinel → Term Anchor → Conflict → Audience summaries → Verifier
```

Every agent writes `ConsultState`. Nothing here opens a database, publishes a note, or bypasses a human gate.

Phase A1 built the pipeline and the `consult-01` specimen. Phase A2 adds a gold family (now including `consult-07-intrasentential`: one Malay + English + Hokkien utterance), unlabeled-role inference, overlap fail-closed, dose conflicts that do not auto-resolve, simulated ASR noise (`asr_hypothesis`), a gold-text extraction eval, and a markdown agent trace. `audio_bench` scores small public multilingual slices when Hugging Face and ASR are actually present; default pytest never downloads.

## Run

```bash
cd trilingual-consult
uv sync --extra dev
uv run pytest
uv run python -m trilingual_consult.cli datasets/nightingale_switchcare/scripts/consult-01.json
uv run python -m trilingual_consult.eval
```

CLI writes `artifacts/switchcare-<id>.json` and `artifacts/switchcare-<id>.md`. Eval writes `artifacts/eval-summary.json`. That eval is gold-text extraction, **not** WER.

PolyWER is a separate runner and **abstains** unless synthetic TTS wavs and an ASR hypothesis both exist:

```bash
datasets/nightingale_switchcare/scripts/synthesize_consult_01_audio.sh
uv run python -m trilingual_consult.eval_audio
```

No WER number is printed when TTS or ASR is missing. PriMock57 English audio is not this eval.

## Gold set

See `datasets/nightingale_switchcare/`. `consult-01` is the reporting specimen: clinician English metformin dose, patient Mandarin penicillin denial, family Malay penicillin allergy (hearsay). `consult-07-intrasentential` is the Q6 *one-sentence* specimen (Malay matrix + `penicillin` + Hokkien `tui … koe-bin`).

Multilingual *audio capability* (not a SEA consult score):

```bash
uv run python -m trilingual_consult.audio_bench --set vimedcss --n 40
```

Missing `datasets` extra, gated HF, or missing `LOCAL_ASR_MODEL_DIR` is recorded as a skip / `ASR_UNAVAILABLE`, not a fabricated WER. See `datasets/external/README.md`.

## Out of scope here

MERaLiON, Nightingale worker/live/UI, LangGraph, real patient audio, pyannote, Twilio. Integration is a later Phase B, only after this gate is green and the main tree is explicitly opened.
