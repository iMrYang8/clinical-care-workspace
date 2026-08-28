# Nightingale model and provider inventory

This file separates implemented adapters from configured services, installed
libraries, model weights, and measured validation. A provider appearing in
source code is not evidence that a live model call or clinical-quality
evaluation has occurred.

Two committed quality reports are explicit, evidence-bound exceptions to that
general warning: actual hosted OpenAI API inference was run on mock/synthetic
evaluation inputs, never patient data. ACI-Bench fact extraction used
`gpt-5.1`; PriMock57 voice transcription used
`gpt-4o-transcribe-diarize`. Both measured results are **Low**, so the runtime
abstains instead of presenting them as clinically reliable. OpenAI remains a
hosted service: this repository records model identifiers and derived metrics
but does not contain or redistribute OpenAI model weights or raw provider
responses.

## Clinic-scoped routing and secret handling

Clinic administrators can store one OpenAI credential and choose separate
model identifiers for fast extraction, careful review, and transcription. The
credential is encrypted with the clinic field-encryption context; ordinary API
responses expose only whether a key is configured and its last four
characters. Runtime resolution prefers a clinic credential and falls back to
the deployment environment. Global egress, provider, Presidio, and strict
no-audio-egress gates still apply, so saving a key does not by itself enable a
remote request.

The Clinic Admin settings form, and a newly created clinic-setting row, start
with `gpt-5-mini` for fast extraction, `gpt-5.1` for careful review, and
`gpt-4o-transcribe-diarize` for transcription. These are editable UI/row
defaults, not a statement that a release actually used those models and not a
quality claim. At runtime, the effective identifier comes from the selected
clinic row or the deployment fallback and is recorded with the decision. A
displayed confidence band additionally requires an unexpired calibration
report whose provider, exact model, task, parameters, and dataset manifest
match that effective runtime decision. Provider self-reported confidence is
not used directly.

## Release inventory

| Workflow | Mode | Shipped/default state | Required configuration or asset | Verified scope and claim boundary |
| --- | --- | --- | --- | --- |
| Clinical text extraction | Deterministic provider | **Enabled by default.** No network or model weights. | `AI_PROVIDER=deterministic` | Contract, job, provenance, fallback, and idempotency paths are covered by local tests. Output is a deterministic synthetic fixture, not an LLM evaluation. |
| Clinical text extraction | OpenAI Responses adapter | Implemented; **disabled by default**. | `AI_PROVIDER=openai`, `REMOTE_TEXT_EGRESS_ENABLED=true`, a clinic or environment API key, and a fast/careful model selection; remote-egress image also installs the required Presidio model. | Mocked transport covers the adapter contract. Actual hosted OpenAI API inference over mock/synthetic ACI-Bench inputs binds `gpt-5.1` to 176 decisions across 40 consultations: accuracy 0.073864, lower bound 0.043671, and **Low** confidence. The negative result is preserved; it triggers abstention rather than a clinical-quality claim. No latency, cost, or clinical validation is claimed. |
| High-risk text review | Independent OpenAI review adapter | Implemented; **not available without configuration**. | All extraction gates plus a clinic careful-model selection or `OPENAI_REVIEW_MODEL`. | The pipeline records consistent, disagreement, unavailable, and error outcomes. The configured careful model can only add review evidence; it cannot lower deterministic risk floors, override abstention, or replace clinician approval. |
| Text de-identification | Project recognizers + Presidio Analyzer/Anonymizer | Core libraries locked and embedded; no unauthenticated Presidio service. | Local patient terms and SG recognizers always participate. Remote egress requires `PRESIDIO_REQUIRED=true`. | Fail-closed behavior, residual scan, and non-reflective logging are tested. The committed 500-case gold-span fixture reports PHI recall 1.0, residual PHI 0, and clinical-span damage 0 for the supported synthetic classes. This is a fixed-fixture result, not a guarantee for unseen clinical data. |
| Presidio NLP | spaCy `en_core_web_sm` 3.8.0 | Locked optional `presidio-nlp` group; **omitted from default image**. | Build with `INSTALL_PRESIDIO_NLP=true`; configured `PRESIDIO_NLP_MODEL` must load. | Intended for the remote-text boundary. It is not a clinical NER recall claim and is not downloaded by application code. |
| Voice transcription | Disabled | **Default.** | `VOICE_TRANSCRIPTION_PROVIDER=disabled` | Ordinary audio is encrypted and retained with an explicit `needs_review`/provider-disabled state. No transcript is invented. |
| Voice transcript fixture | `code-switch-overlap-v1` synthetic provider | Available only in development demo mode for a session explicitly marked synthetic. | `FASTAPI_ENV=development`, `ENABLE_DEMO_AUTH=true`, `synthetic_fixture=true`, exact fixture ID. | Exercises speaker, timestamp, language, overlap, confidence, review, and provenance UI. It is fixed fixture data, never ASR and never a quality measurement. |
| Voice transcription | OpenAI audio transcription adapter | Implemented; **disabled by default**. | `VOICE_TRANSCRIPTION_PROVIDER=openai`, `REMOTE_AUDIO_EGRESS_ENABLED=true`, a clinic or environment API key, and a transcription-model selection; `STRICT_NO_AUDIO_EGRESS` must be false. | Adapter validates diarized/timestamped segments. Actual hosted OpenAI API inference over the mock PriMock57 holdout binds `gpt-4o-transcribe-diarize` to 2,206 segment decisions across 17 consultations: WER 0.200397, medical-entity recall 0.857407, speaker error 0.202121, accuracy lower bound 0.129246, and **Low** confidence. The runtime therefore abstains; no clinical validation, supported-language matrix, latency, or cost claim is made. |
| Voice transcription | faster-whisper `1.2.1`, CTranslate2 `4.8.1`, PyAV `18.1.0` | Optional `local-asr` profile; all three are lock-resolved but not default-installed; no weights bundled. | Build optional group, mount a non-empty pre-cached `LOCAL_ASR_MODEL_DIR`; provider uses CPU/int8 and `local_files_only=True`. | Process timeout/cancellation and adapter contracts are implemented. CTranslate2/PyAV runtime import and a particular weight set remain **not tested**. PyAV wheel FFmpeg composition is release-platform-specific. No diarization is claimed. |
| Speaker/overlap experiment | pyannote.audio 4.0.7 | Experimental optional profile; no model/token bundled. | Accepted model terms and pre-cached `PYANNOTE_MODEL_DIR`, plus explicit enablement. | Current code exposes readiness only; it does not apply or validate a pyannote diarization pipeline. Multi-device/provider overlap is preserved as a review signal, not blind-source separation. |
| Live captions | Disabled | **Default.** | `LIVE_TRANSCRIPT_ENABLED=false` or `LIVE_TRANSCRIPT_PROVIDER=disabled` | The capability endpoint reports unavailable and the UI does not fabricate provisional captions. |
| Live captions | Deterministic synthetic fixture | Development-only, explicit synthetic session only. | `FASTAPI_ENV=development`, `ENABLE_DEMO_AUTH=true`, `LIVE_TRANSCRIPT_ENABLED=true`, `LIVE_TRANSCRIPT_PROVIDER=deterministic`, and the session's synthetic fixture flag. | Fixed provisional pieces exercise the authenticated WebSocket/UI contract. They are not ASR and are not a quality or latency measurement. |
| Live captions | OpenAI Realtime transcription adapter | Implemented; **disabled by default**. | `LIVE_TRANSCRIPT_ENABLED=true`, `LIVE_TRANSCRIPT_PROVIDER=openai`, `REMOTE_AUDIO_EGRESS_ENABLED=true`, `STRICT_NO_AUDIO_EGRESS=false`, `OPENAI_API_KEY`, and an explicit `OPENAI_LIVE_TRANSCRIBE_MODEL` beginning with `gpt-live-transcribe`. | Sends rate-, frame-, session-, and concurrency-bounded 24 kHz PCM16 only after all gates pass. The adapter/transport are covered with an injected mock transport; this repository contains no real call, accuracy, code-switch, clinical-quality, cost, or latency evidence for the live model. |

## Non-model processing dependency

FFmpeg performs bounded decoding, 16 kHz mono conversion, filtering, loudness
normalization, and signal measurements. It is not an ASR or diarization model.
The backend image writes its exact build output to:

```text
/usr/share/doc/nightingale/ffmpeg-build.txt
```

Archive the container record for the release with:

```bash
./scripts/capture_ffmpeg_inventory.sh
```

The repository evidence path is:

```text
docs/evidence/ffmpeg-container-version.txt
```

The evidence generator now requires a backend image labeled with the current
Git commit and records that image ID. A reused global `backend:latest` is
rejected. The committed record was generated from the local release-candidate
backend image on 2026-08-26: FFmpeg `7.1.5-0+deb13u1`, Debian arm64, with
`--enable-gpl`; its file SHA-256 is
`b934420d8be52ec97333d79e6714ef2697c05892262602b388aede4d929dc020`.
This validates that exact local image only. Rebuilds on another platform or
from a newer base image must regenerate the record; a developer machine's
FFmpeg output is not interchangeable with the container record.

## Committed evaluation artifacts

The repository keeps derived metrics and version-binding metadata, not API
keys, raw provider responses, or patient data:

| Artifact | Bound provider/model/task | Result boundary |
| --- | --- | --- |
| `artifacts/evaluation/fact-calibration.json` | OpenAI / `gpt-5.1` / clinical fact extraction | ACI-Bench mock consultations; 176 decisions, 40 consultations, **Low** confidence and abstention. |
| `artifacts/evaluation/voice-calibration.json` | OpenAI / `gpt-4o-transcribe-diarize` / voice transcription | PriMock57 mock consultations; 2,206 decisions, 17 consultations, **Low** confidence and abstention. |
| `artifacts/evaluation/redaction-v2.json` | `nightingale-redaction-v2` | 500 fixed synthetic cases; supported-class PHI recall 1.0, residual PHI 0, clinical-span damage 0. |

The two provider reports share dataset manifest SHA-256
`09fb98f0f00629095327ddf59c89f4b8d8a4cd8bb3c21efaabe385b3f453f28a`.
Changing the model, task, parameters, code, report expiry, or manifest binding
makes confidence unavailable. All remote evaluation inputs were mock/synthetic.
The two OpenAI reports came from actual hosted API execution; only the derived
metrics and binding metadata are committed. They do not redistribute the
hosted models, their weights, or provider responses.

## Release evidence checklist

For every externally configured model, add a release-specific record without
committing secrets:

1. provider and exact model identifier;
2. dependency and weight versions/digests;
3. applicable code/model/data licenses and gated terms;
4. egress flags and redaction gate state;
5. fixture or dataset provenance and synthetic/real-data boundary;
6. hardware, date, commit SHA, latency, failure, and quality measurements;
7. observed capability state and fallback behavior.

`gpt-5.1` and `gpt-4o-transcribe-diarize` are asserted only where the exact
identifiers are bound to the committed mock/synthetic evaluation reports above.
`gpt-5-mini` remains an editable Clinic Admin form/new-row default without
committed quality evidence or proof that a release used it at runtime.
Whisper weights and pyannote models are not asserted merely because they appear
in configuration or planning text; each release must record the actual runtime
identifier, assets, gates, and measured result separately.
