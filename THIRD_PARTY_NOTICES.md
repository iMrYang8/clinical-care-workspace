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

## Optional integrations

| Component | Status | Intended role | Licensing note |
| --- | --- | --- | --- |
| [Presidio](https://github.com/data-privacy-stack/presidio) | **Optional integration — not installed.** | PII/PHI detection and anonymization. | MIT. The project transitioned from the Microsoft organization; review the selected package/image release at adoption. |
| [FFmpeg](https://ffmpeg.org/) | **Optional runtime tool — not bundled.** | Audio normalization and conversion. | FFmpeg builds may be LGPL or GPL depending on enabled configuration; use a documented build and satisfy its corresponding obligations. |
| [faster-whisper](https://github.com/SYSTRAN/faster-whisper) | **Optional integration — not installed.** | Speech-to-text. | MIT for the library; its CTranslate2 dependency and selected model artifacts retain their own notices and terms. |
| [pyannote.audio](https://github.com/pyannote/pyannote-audio) | **Optional integration — not installed.** | Speaker diarization. | MIT for the library. Selected pretrained pipelines/models may have separate model cards, access conditions, and terms that must be recorded before download or redistribution. |

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
