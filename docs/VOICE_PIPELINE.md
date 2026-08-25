# Nightingale voice capture and review

## Truthful operating boundary

Nightingale stores every accepted chunk before processing and never treats a
deterministic fixture as speech recognition. An ordinary recording with no
configured ASR provider retains encrypted audio and finishes in
`needs_review` with `ASR_PROVIDER_DISABLED` (or another explicit configuration
code). The only deterministic transcript is `code-switch-overlap-v1`, and the
API accepts it only when the server is in development demo mode and the session
was created with `synthetic_fixture=true`.

The implementation detects concurrent device tracks and provider-reported
overlap as review signals. It does not claim blind-source separation or precise
clock synchronization. Multi-device mixing aligns track starts. Browser upload
recovery works after reload while the application is open; it does not claim OS
background sync after the page is closed.

## Persisted state machine

```text
created -> recording -> finalizing -> assembling -> preprocessing
        -> transcribing -> redacting -> extracting
        -> ready | needs_review -> published
```

Worker transitions lock the clinic-scoped session and compare the expected
state before writing the next state. Jobs use a renewable/bounded PostgreSQL
job lease, attempt token, retry budget, and worker membership binding. Voice
retries always return to the durable worker rather than running FFmpeg or ASR
inside an API request. One session has at
most one assembled `AudioAsset`; transcript corrections create a new immutable
`TranscriptRevision`; reanalysis creates another immutable revision. A retry
therefore cannot duplicate an asset, transcript revision, or published entry.

The following rows use PostgreSQL RLS and clinic-composite foreign keys:

- `voice_sessions`, `voice_devices`, `audio_chunks`, `audio_assets`
- `transcript_revisions`, `transcript_segments`, `clinical_facts`

Chunks, assembled assets, revisions, and segments have append-only database
triggers. Facts retain immutable evidence fields in application flow while
their audited review status can move from `proposed` to `accepted` at
publication. Audio payloads and transcript/fact text use the same
clinic-derived AES-256-GCM envelope as other clinical fields.

## Capture and upload

The browser selects MediaRecorder formats in this order:

1. `audio/webm;codecs=opus`
2. `audio/mp4` (Safari fallback)
3. `audio/webm`

Approximately every two seconds, the plaintext browser chunk is hashed, then
encrypted with a non-extractable WebCrypto AES-GCM key and stored in IndexedDB.
It is decrypted only for authenticated upload. The server independently checks
the hash and encrypts the accepted bytes. A repeated `(device, chunk_index)`
with the same hash and identical media/time metadata is acknowledged; changed
bytes or metadata return a conflict. Each device seals its final index before
the session-wide barrier can finalize. Finalization requires declarations for
every joined device and returns `MISSING_AUDIO_CHUNKS` with exact indices when a
gap exists. The API caps a session at eight devices, 21,601 two-second chunks
per device, 12 hours of declared time, 8 MiB per chunk, and 512 MiB total.

The browser key and ciphertext share the authenticated application origin.
This prevents plaintext chunks from being written to IndexedDB, but it is not
an XSS or compromised-origin boundary. A capture remains locally recoverable
until both the final chunk uploads and finalization are acknowledged; successful
finalization deletes its local key and queue rows.

FFmpeg is invoked with an argument array, `-nostdin`, a timeout, and `0600`
temporary files inside a `0700` directory. It produces 16 kHz mono PCM with a
high-pass filter and loudness normalization. Silence, clipping, and low-level
noise are measured on each decoded track before normalization; multi-device
overlap remains a conservative track-level review signal. All are persisted as
review warnings and can force `needs_review`. The container writes
its exact `ffmpeg -version` output at build time:

```bash
docker compose run --rm backend \
  cat /usr/share/doc/nightingale/ffmpeg-build.txt
```

## Transcription providers

| Mode | Required configuration | Network/model behavior |
| --- | --- | --- |
| Disabled (default) | `VOICE_TRANSCRIPTION_PROVIDER=disabled` | No ASR; encrypted audio is retained with an explicit pending/review state. |
| Synthetic fixture | Development demo plus explicit fixture checkbox | Fixed speaker/timestamp/code-switch/overlap fixture only; never selected for ordinary audio. |
| OpenAI final transcription | `VOICE_TRANSCRIPTION_PROVIDER=openai`, `REMOTE_AUDIO_EGRESS_ENABLED=true`, `OPENAI_API_KEY`, `OPENAI_TRANSCRIBE_MODEL` | Sends the normalized audio only when every gate is true. `STRICT_NO_AUDIO_EGRESS=true` overrides all remote settings. Model IDs come from the environment. Calls have a bounded ASR timeout. |
| faster-whisper | `compose.local-asr.yml`, a pre-cached `LOCAL_ASR_MODEL_DIR` | CPU/int8 and `local_files_only=True`; no runtime model download. No diarization is claimed. |
| pyannote experimental | `compose.diarization.yml`, accepted model terms, cached `PYANNOTE_MODEL_DIR` | Default off. Current code exposes a local readiness gate; it does not silently fetch or apply a gated model. |

Local no-egress overlay:

```bash
export LOCAL_ASR_MODEL_DIR=/absolute/path/to/cached-ctranslate2-model
docker compose -f compose.yml -f compose.override.yml \
  -f compose.local-asr.yml up --build
```

Experimental pyannote readiness overlay:

```bash
export PYANNOTE_MODEL_DIR=/absolute/path/to/licensed-cached-model
docker compose -f compose.yml -f compose.override.yml \
  -f compose.diarization.yml up --build
```

No Hugging Face token or weight is committed. A missing dependency, missing
directory, or rejected model access cannot block the core synthetic demo.

## Review and publication

Clinical Review Mode shows transcript, summary, and facts in desktop columns
and mobile tabs. It exposes speaker, timestamp, language, confidence, overlap,
low-confidence filtering, and honest `stale`, `needs_review`, `fallback`,
`ready`, and `published` states. Clicking a fact scrolls to its transcript
segment and seeks authorized audio.

A correction creates a new transcript revision and disables publication with
`DOWNSTREAM_RESULTS_STALE`. Reanalysis creates a later revision. Facts are
persisted only when all of the following validate:

- the exact quote matches the transcript offsets and SHA-256;
- the fact maps to a segment in that revision;
- the audio interval is inside the segment and assembled asset duration.

Publication is clinician-only and only runs from a stable `ready` or
`needs_review` session. It explicitly accepts the evidence-validated facts and
records the actor/time before creating an immutable encrypted transcript
source entry, a separate immutable derived summary entry, a relation between
them, and append-only provenance pointers that bind the transcript span to the
audio asset and millisecond interval. It never overwrites a human care note.

Patient routes use only `VoiceSessionPublic`; that response omits raw transcript,
facts, internal warnings, internal errors, and the current transcript revision
identifier. The raw transcript endpoint rejects Patient and Admin roles.
The patient UI never requests audio, and the audio endpoint rejects every
Patient role. Raw normalized audio is restricted to Staff and Clinician review.

## API surface

```text
POST /api/v1/voice/sessions
POST /api/v1/voice/sessions/{id}/devices
PUT  /api/v1/voice/sessions/{id}/devices/{device_id}/chunks/{index}
POST /api/v1/voice/sessions/{id}/devices/{device_id}/seal
GET  /api/v1/voice/sessions/{id}/chunks/status
POST /api/v1/voice/sessions/{id}/finalize
GET  /api/v1/voice/sessions/{id}
GET  /api/v1/voice/sessions/{id}/transcript
POST /api/v1/voice/sessions/{id}/transcript/correct
POST /api/v1/voice/sessions/{id}/reanalyze
POST /api/v1/voice/sessions/{id}/publish
GET  /api/v1/voice/sessions/{id}/audio
GET  /api/v1/voice/sessions/{id}/live
```

`/live` is a capability endpoint. This build has no live provider/transport and
therefore returns `unavailable` even when the deployment gate is enabled; it
never fabricates provisional captions.

## Verification

```bash
cd backend
pytest -q tests/test_voice_chunks.py tests/test_voice_permissions.py \
  tests/test_voice_worker.py tests/test_transcript_audio_provenance.py \
  tests/test_voice_providers.py
coverage run -m pytest -q && coverage report --fail-under=90
alembic upgrade head && alembic current && alembic check
ruff check app tests && ruff format app tests --check
mypy app && ty check app

cd ../frontend
bun run typecheck
bun run lint
bun run test
bun run build
```
