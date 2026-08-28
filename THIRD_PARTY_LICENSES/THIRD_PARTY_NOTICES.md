# Third-party notices and adoption register

This register separates material already incorporated in the baseline from
components that may be evaluated later. It is not evidence that an item is
installed, redistributed, or approved for production use. Before adding a
package, model, binary, source file, or asset, pin its version and add its
applicable license and model terms to the release notice.

## Incorporated baseline

| Component | Status | Source | License / notice |
| --- | --- | --- |
| FastAPI Full Stack FastAPI Template | **Direct — incorporated baseline.** This repository is adapted from upstream commit [`68adb40d`](https://github.com/fastapi/full-stack-fastapi-template/commit/68adb40d). | [fastapi/full-stack-fastapi-template](https://github.com/fastapi/full-stack-fastapi-template) | MIT; attribution and license retention are recorded in [`ATTRIBUTION.txt`](../ATTRIBUTION.txt), [`LICENSE`](../LICENSE), and [`FastAPI-Template-MIT.txt`](./FastAPI-Template-MIT.txt). |

## Direct frontend dependencies

| Component | Version | Role | License / notice |
| --- | --- | --- | --- |
| [Tiptap core, React, ProseMirror bridge, and Starter Kit](https://github.com/ueberdosis/tiptap) | `3.30.3` (all four packages) | Open-source rich-text editing for care-note content. | MIT. This repository does not use Tiptap Pro, Cloud Comments, or Versioning. |
| [Serene in Serenade Tiptap Comment Extension](https://github.com/sereneinserenade/tiptap-comment-extension) | `0.2.0` | Adds the selected-text `commentId` mark only; Nightingale owns persistence, immutable anchoring, review state, mentions, assignment, and audit behavior. | MIT. Used as an npm dependency through `CommentAnchorAdapter`; no source is vendored or modified. |
| [idb](https://github.com/jakearchibald/idb) | `8.0.3` | Typed IndexedDB wrapper for encrypted, reload-resumable voice chunks. | ISC. The package license is retained in the installed dependency. |
| [fake-indexeddb](https://github.com/dumbmatter/fakeIndexedDB) | `6.2.5` (development/test only) | IndexedDB contract tests. | Apache-2.0. Not shipped in the production bundle. |

## Direct backend dependencies

| Component | Locked version | Role | License / notice |
| --- | --- | --- | --- |
| [Presidio Analyzer](https://github.com/data-privacy-stack/presidio) | `2.2.364` | Embedded PII/PHI analysis behind Nightingale's deterministic SG recognizers and fail-closed residual scan. | MIT, Copyright (c) Presidio Contributors. The license and the upstream 2.2.364 third-party NOTICE are retained in `THIRD_PARTY_LICENSES/Presidio-MIT.txt` and `THIRD_PARTY_LICENSES/Presidio-NOTICE.txt`. No standalone unauthenticated Presidio service is deployed. |
| [Presidio Anonymizer](https://github.com/data-privacy-stack/presidio) | `2.2.362` | Locked anonymization dependency; Nightingale currently uses stable project-owned placeholders so immutable evidence mapping remains auditable. | MIT, covered by the retained Presidio license and project NOTICE. |
| [spaCy](https://github.com/explosion/spaCy) | `3.8.16` (transitive through Presidio) | NLP engine interface for configured Presidio language models. | MIT. Missing or broken configured models fail closed before remote egress. |
| [spaCy `en_core_web_sm`](https://github.com/explosion/spacy-models/releases/tag/en_core_web_sm-3.8.0) | `3.8.0` — locked optional `presidio-nlp` group, omitted from the default image. | Local English PERSON/PHONE/EMAIL/ID analysis for the Presidio remote-text boundary. | MIT model package. Application code never downloads it. CI installs the frozen lock and requires a load/remote-boundary smoke; release images include it only when built with `INSTALL_PRESIDIO_NLP=true`. This does not validate clinical recall or other language models. |
| [python-zstandard](https://github.com/indygreg/python-zstandard) | `0.25.0` | Bounded zstd compression before AES-256-GCM cold archive encryption. | BSD 3-Clause; the installed wheel includes its license text. |

## Hosted model services — accessed, not redistributed

Hosted APIs are configuration boundaries, not bundled dependencies or model
distributions. Provider access is governed by the clinic/operator account and
the provider's applicable service terms. Nightingale stores encrypted API
credentials only in the configured clinic or deployment environment and does
not commit them.

| Service | Repository use and measured evidence | Distribution boundary |
| --- | --- | --- |
| OpenAI API | Optional, disabled-by-default text, review, transcription, and realtime adapters. Actual hosted API inference over mock/synthetic evaluation inputs produced two committed derived reports: ACI-Bench fact extraction with `gpt-5.1` (176 decisions, 40 consultations, **Low**) and PriMock57 transcription with `gpt-4o-transcribe-diarize` (2,206 segment decisions, 17 consultations, **Low**). Both Low results cause abstention and are not clinical validation. `gpt-5-mini` remains only an editable default without committed quality evidence; the realtime adapter has mocked transport coverage only. | OpenAI supplies these models as hosted services. This repository redistributes no OpenAI model, weight, tokenizer, raw provider response, or credential, and grants no onward right to do so. Only model identifiers, repository-authored derived metrics, and version-binding metadata are committed. |

## Optional integrations

| Component | Status | Intended role | Licensing note |
| --- | --- | --- | --- |
| [FFmpeg](https://ffmpeg.org/) | **Direct container runtime.** Installed from the Debian repository in the Python 3.12 base build. Each image writes its actual version, compiler, configuration flags, and library versions to `/usr/share/doc/nightingale/ffmpeg-build.txt`. The 2026-08-26 local release-candidate record at `docs/evidence/ffmpeg-container-version.txt` is FFmpeg `7.1.5-0+deb13u1`, Debian arm64, `--enable-gpl`, file SHA-256 `b934420d8be52ec97333d79e6714ef2697c05892262602b388aede4d929dc020`. | Argument-array subprocess for 16 kHz mono PCM, high-pass/loudness processing, and measurable review signals. | The observed container is GPL-enabled. FFmpeg licensing depends on the exact build and enabled libraries; release operators must regenerate the record for rebuilt images and retain the corresponding package/source offer and notices. |
| [faster-whisper](https://github.com/SYSTRAN/faster-whisper) | **Declared optional `local-asr` dependency group; locked `1.2.1`, not default-installed.** `compose.local-asr.yml` installs the group only in an explicitly rebuilt image and mounts a pre-cached local model read-only. | CPU/int8 speech-to-text with `local_files_only=True`. | MIT for the library; no weights are bundled, downloaded at runtime, or claimed as generally validated. |
| [CTranslate2](https://github.com/OpenNMT/CTranslate2) | Locked transitive optional dependency `4.8.1`; omitted from the default image. | CPU/int8 inference engine for the optional faster-whisper adapter. | MIT; full text is retained at `THIRD_PARTY_LICENSES/CTranslate2-MIT.txt`. Lock resolution passed; runtime/model smoke is **not tested**. |
| [PyAV](https://github.com/PyAV-Org/PyAV) | Locked transitive optional dependency `18.1.0`; omitted from the default image. | Audio container decoding for the optional faster-whisper adapter. | Source is BSD-3-Clause; full text is retained at `THIRD_PARTY_LICENSES/PyAV-BSD-3-Clause.txt`. The selected platform wheel and its bundled FFmpeg license composition are **not tested** and require release-specific review before redistribution. |
| [pyannote.audio](https://github.com/pyannote/pyannote-audio) | **Declared experimental `diarization` dependency group; locked `4.0.7`, not default-installed.** `compose.diarization.yml` requires an explicitly enabled, pre-cached model directory. | Local gated-model readiness and later overlap/diarization experiments. The current default pipeline does not apply it. | Code is MIT, Copyright (c) 2020 CNRS; retained in `THIRD_PARTY_LICENSES/Pyannote-Audio-MIT.txt`. Official citations are retained in `THIRD_PARTY_LICENSES/Pyannote-CITATION.bib`. Model terms are separate: operators must accept the selected Hugging Face model conditions, provide a token only for acquisition, pin the exact model revision, and cache it before enabling the profile. No terms were accepted, token read, model downloaded, or diarization result claimed by default. |

## Design references only

No source code, model, media, design asset, or configuration has been copied
from the following projects into this repository. They are comparison inputs,
not dependencies or upstream code.

| Reference | Status | Source / licensing note |
| --- | --- | --- |
| [open-medical-scribe](https://github.com/BirgerMoell/open-medical-scribe) | **Design reference only.** | MIT upstream; no material is incorporated. |
| [AI-Medical-Scribe](https://github.com/hutchpd/AI-Medical-Scribe) | **Design reference only.** | MIT upstream; no material is incorporated. |
| open-scribe / OpenScribe | **Design reference only.** | The supplied name corresponds to multiple public projects. No material is incorporated; select a canonical upstream and record its license before any adoption. |

## Adoption gate

An item may move from this register into a dependency manifest or distributed
artifact only after a change records: (1) canonical source and immutable
version or digest, (2) direct and transitive license obligations, (3) model or
dataset terms where applicable, and (4) the final runtime packaging method.

The backend container copies this register and `THIRD_PARTY_LICENSES/` to
`/usr/share/doc/nightingale/`; the notices are not represented as a clinical UI
or patient-facing application route.
