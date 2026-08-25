# Third-party notices and adoption register

This register separates material already incorporated in the baseline from
components that may be evaluated later. It is not evidence that an item is
installed, redistributed, or approved for production use. Before adding a
package, model, binary, source file, or asset, pin its version and add its
applicable license and model terms to the release notice.

## Incorporated baseline

| Component | Status | Source | License / notice |
| --- | --- | --- |
| FastAPI Full Stack FastAPI Template | **Direct — incorporated baseline.** This repository is adapted from upstream commit [`68adb40d`](https://github.com/fastapi/full-stack-fastapi-template/commit/68adb40d). | [fastapi/full-stack-fastapi-template](https://github.com/fastapi/full-stack-fastapi-template) | MIT; attribution and license retention are recorded in [`ATTRIBUTION.txt`](./ATTRIBUTION.txt) and [`LICENSE`](./LICENSE). |

## Direct frontend dependencies

| Component | Version | Role | License / notice |
| --- | --- | --- | --- |
| [Tiptap core, React, ProseMirror bridge, and Starter Kit](https://github.com/ueberdosis/tiptap) | `3.30.3` (all four packages) | Open-source rich-text editing for care-note content. | MIT. This repository does not use Tiptap Pro, Cloud Comments, or Versioning. |
| [Serene in Serenade Tiptap Comment Extension](https://github.com/sereneinserenade/tiptap-comment-extension) | `0.2.0` | Adds the selected-text `commentId` mark only; Nightingale owns persistence, immutable anchoring, review state, mentions, assignment, and audit behavior. | MIT. Used as an npm dependency through `CommentAnchorAdapter`; no source is vendored or modified. |

## Direct backend dependencies

| Component | Locked version | Role | License / notice |
| --- | --- | --- | --- |
| [Presidio Analyzer](https://github.com/data-privacy-stack/presidio) | `2.2.364` | Embedded PII/PHI analysis behind Nightingale's deterministic SG recognizers and fail-closed residual scan. | MIT. No standalone unauthenticated Presidio service is deployed. |
| [Presidio Anonymizer](https://github.com/data-privacy-stack/presidio) | `2.2.362` | Locked anonymization dependency; Nightingale currently uses stable project-owned placeholders so immutable evidence mapping remains auditable. | MIT. |
| [spaCy](https://github.com/explosion/spaCy) | `3.8.16` (transitive through Presidio) | NLP engine interface for configured Presidio language models. | MIT. `en_core_web_sm` or any other language model is **not** locked, bundled, downloaded by application code, or claimed as validated. Missing models produce `fallback/needs_review` and block remote egress. |
| [python-zstandard](https://github.com/indygreg/python-zstandard) | `0.25.0` | Bounded zstd compression before AES-256-GCM cold archive encryption. | BSD 3-Clause; the installed wheel includes its license text. |

## Optional integrations

| Component | Status | Intended role | Licensing note |
| --- | --- | --- | --- |
| [FFmpeg](https://ffmpeg.org/) | **Host runtime observed, not bundled.** Developer smoke host reported `8.0.1`, Homebrew prefix `/opt/homebrew/Cellar/ffmpeg/8.0.1`, with `--enable-gpl` and codecs including x264/x265. | Audio normalization and conversion in the later voice profile. | This observed build is GPL-enabled. Container/release builds must record their own exact `ffmpeg -version` and satisfy the corresponding license; this host observation does not validate a distributed image. |
| [faster-whisper](https://github.com/SYSTRAN/faster-whisper) | **Declared optional `local-asr` dependency group; locked `1.2.1`, not default-installed.** | Speech-to-text. | MIT for the library; CTranslate2, PyAV, and selected Whisper weights retain separate notices/terms. No weights are bundled or claimed as smoke-tested. |
| [pyannote.audio](https://github.com/pyannote/pyannote-audio) | **Declared optional `diarization` dependency group; locked `4.0.7`, not default-installed.** | Speaker diarization/overlap experiments. | Library and model terms must be checked separately. No gated model, Hugging Face token, cached pipeline, or CPU smoke is claimed. |

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
