# Nightingale model and provider inventory

This file separates implemented adapters from configured services, installed
libraries, model weights, and measured validation. A provider appearing in
source code is not evidence that a live model call or clinical-quality
evaluation has occurred.

## Release inventory

| Workflow | Mode | Shipped/default state | Required configuration or asset | Verified scope and claim boundary |
| --- | --- | --- | --- | --- |
| Clinical text extraction | Deterministic provider | **Enabled by default.** No network or model weights. | `AI_PROVIDER=deterministic` | Contract, job, provenance, fallback, and idempotency paths are covered by local tests. Output is a deterministic synthetic fixture, not an LLM evaluation. |
| Clinical text extraction | OpenAI Responses adapter | Implemented; **disabled by default**. | `AI_PROVIDER=openai`, `REMOTE_TEXT_EGRESS_ENABLED=true`, `OPENAI_API_KEY`, `OPENAI_EXTRACT_MODEL`; remote-egress image also installs the required Presidio model. | Transport/schema behavior is testable with mocked responses. No model ID is pinned in code, and this repository contains no evidence of a live call, quality benchmark, clinical validation, cost, or latency for a configured model. |
| High-risk text review | Independent OpenAI review adapter | Implemented; **not available without configuration**. | All extraction gates plus `OPENAI_REVIEW_MODEL`. | The pipeline records consistent, disagreement, unavailable, and error outcomes. A second configured call is not proof of correctness and does not replace clinician review. |
| Text de-identification | Project recognizers + Presidio Analyzer/Anonymizer | Core libraries locked and embedded; no unauthenticated Presidio service. | Local patient terms and SG recognizers always participate. Remote egress requires `PRESIDIO_REQUIRED=true`. | Fail-closed behavior, residual scan, and non-reflective logging are tested. Automated detection cannot guarantee complete removal of PHI. |
| Presidio NLP | spaCy `en_core_web_sm` 3.8.0 | Locked optional `presidio-nlp` group; **omitted from default image**. | Build with `INSTALL_PRESIDIO_NLP=true`; configured `PRESIDIO_NLP_MODEL` must load. | Intended for the remote-text boundary. It is not a clinical NER recall claim and is not downloaded by application code. |
| Voice transcription | Disabled | **Default.** | `VOICE_TRANSCRIPTION_PROVIDER=disabled` | Ordinary audio is encrypted and retained with an explicit `needs_review`/provider-disabled state. No transcript is invented. |
| Voice transcript fixture | `code-switch-overlap-v1` synthetic provider | Available only in development demo mode for a session explicitly marked synthetic. | `FASTAPI_ENV=development`, `ENABLE_DEMO_AUTH=true`, `synthetic_fixture=true`, exact fixture ID. | Exercises speaker, timestamp, language, overlap, confidence, review, and provenance UI. It is fixed fixture data, never ASR and never a quality measurement. |
| Voice transcription | OpenAI audio transcription adapter | Implemented; **disabled by default**. | `VOICE_TRANSCRIPTION_PROVIDER=openai`, `REMOTE_AUDIO_EGRESS_ENABLED=true`, `OPENAI_API_KEY`, `OPENAI_TRANSCRIBE_MODEL`; `STRICT_NO_AUDIO_EGRESS` must be false. | Adapter validates diarized/timestamped segments. No live request, accuracy evaluation, supported-language matrix, latency, or cost evidence is committed here. |
| Voice transcription | faster-whisper 1.2.1 / CTranslate2 | Optional `local-asr` profile; not default-installed; no weights bundled. | Build optional group, mount a non-empty pre-cached `LOCAL_ASR_MODEL_DIR`; provider uses CPU/int8 and `local_files_only=True`. | Process timeout/cancellation and adapter contracts are implemented. A particular weight set remains **not tested** until its exact path/digest, license, hardware, and evaluation output are recorded. No diarization is claimed. |
| Speaker/overlap experiment | pyannote.audio 4.0.7 | Experimental optional profile; no model/token bundled. | Accepted model terms and pre-cached `PYANNOTE_MODEL_DIR`, plus explicit enablement. | Current code exposes readiness only; it does not apply or validate a pyannote diarization pipeline. Multi-device/provider overlap is preserved as a review signal, not blind-source separation. |
| Live captions | Live capability gate | Configuration field and capability endpoint exist; **no live provider/transport in this build**. | `LIVE_TRANSCRIPT_ENABLED` alone is insufficient. | Endpoint reports unavailable. The UI must not present fabricated provisional captions. |

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

The committed record was generated from the local release-candidate backend
image on 2026-08-26: FFmpeg `7.1.5-0+deb13u1`, Debian arm64, with
`--enable-gpl`; its file SHA-256 is
`e3379e95264b2189b027f0fae31698e1f6ce48dbac76548a3cb6fc65d1cc7f87`.
This validates that exact local image only. Rebuilds on another platform or
from a newer base image must regenerate the record; a developer machine's
FFmpeg output is not interchangeable with the container record.

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

No GPT-5.x, Whisper weight, or pyannote model name is asserted merely because
it appeared in a planning document. Runtime model identifiers come from the
deployment environment and must be recorded separately for each release.
