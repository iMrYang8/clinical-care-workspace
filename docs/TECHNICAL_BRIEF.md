# Nightingale — current technical brief

**Current working tree · 2 September 2026 · `release-2026-09-02` · synthetic/mock patient data only**

Nightingale is a clinic-scoped patient-care workspace. It is designed to let a care team answer two questions quickly: **what matters for this patient now, and what exact saved source supports it?** The product combines a five-item Current priorities view, a longitudinal record, role-separated clinical collaboration, reviewable AI-assisted notes, and recoverable voice capture. It does not treat a score, risk badge, or model output as self-validating evidence.

This freeze does **not** claim: live Twilio delivery, offline pyannote quality, real trilingual/noisy clinic ASR, Hokkien recognition, a persisted `MedicationReviewCandidate` table, voice elapsed/retry UI, or `LocalNormalizedText` typing. Missing `DecisionAssessment` rows on model-derived highlights fail closed (`AI_HIGHLIGHT_ASSESSMENT_MISSING`). Backend pytest on this branch: 504 passed, 1 skipped. Frontend vitest: 129 passed.

## 1. Architecture and clinical workflow

![Nightingale system architecture](./architecture.svg)

The browser application uses React, TypeScript, Vite, TanStack Router/Query, shadcn/ui, and Tiptap. FastAPI owns authentication, authorization, validation, the OpenAPI boundary, and same-origin production delivery. SQLModel and Alembic manage PostgreSQL 16. A separate worker claims durable text and voice jobs. Traefik terminates local TLS; Docker Compose provides the reproducible local topology.

The primary workflow is:

1. Staff or Clinician searches the clinic's patient registry and opens a shared record.
2. Current priorities presents at most five precomputed items plus a separate clinical-review queue.
3. **View source** resolves a priority or normalized fact to the exact wording in an immutable saved note version.
4. Human and AI-assisted entries appear together in a longitudinal timeline while retaining their type and review state.
5. Staff and Clinician edit their permitted sections, discuss selected text, mention or assign a colleague, resolve/reopen threads, compare versions, and restore an earlier version as a new version.
6. Staff submits an exact saved note version to the Patient sharing queue. A Clinician reviews that immutable version, passes the provenance/redaction/conflict gates, publishes it, or later withdraws it. Patients use a separate My Care projection that excludes internal comments, raw AI working material, scores, transcripts, and audio; a withdrawal removes the note while retaining a visible receipt and audit history.

The warm Current-priorities path reads an encrypted precomputed snapshot rather than invoking a model. An exploratory run against this **dirty working tree** measured 100 warm reads at median 2.602 ms, p95 **3.881 ms**, and p99 4.151 ms, below the 300 ms target. This is useful current evidence, but it is not a release-candidate measurement until it is rebound to a clean revision and image digest.

## 2. Data model, tenancy, and source integrity

![Nightingale clinic-scoped data model](./schema.svg)

The main evidence chain is:

```text
Clinic → Patient → Entry → immutable EntryVersion
                         ↘ Comment / Highlight / ClinicalFactAssertion
                           → ProvenancePointer → exact quote and saved version
```

`ClinicMembership` assigns one role within a clinic; `PatientUserLink` grants a patient access only to their own record. Patient identifiers use encrypted values plus clinic-scoped HMACs for exact duplicate detection. Tenant rows carry `clinic_id`; tenant-composite foreign keys prevent cross-clinic relationships, and PostgreSQL row-level security reinforces application checks. Clinical text, comments, Glance payloads, identifiers, redaction maps, transcripts, facts, and audio use clinic-bound AES-256-GCM.

Entries do not mutate history. Every save creates an `EntryVersion`; compare-and-swap version checks reject stale writes, and restore creates a new current version. `ProvenancePointer` stores the saved version, offsets, exact quote/context, quote hash, and optional audio time range. The user interface shows the clinically useful source title, author, date, quote, and historical state while hiding internal UUIDs and hashes.

The internal provenance chain is implemented and source jumps were observed in the browser. `EntryPublic` and `PatientTimelineEntry` now expose a required `author_role` plus typed top-level provenance. AI/System entries return `author_role=system`; normal AI runs resolve the immutable input version directly, reviewed voice output uses an explicit entry relation, an archived source is labelled `archived`, and a missing source is honestly returned as `unavailable` rather than treating generated wording as evidence.

## 3. Roles and browser validation

The server resolves the active membership; browser-supplied clinic, role, or actor values are never authority.

| Role | Implemented boundary | Browser observation in the current local build |
| --- | --- | --- |
| Patient | Own patient-facing entries, approved priorities, personal insight/recording; no clinical/admin data | Separate My Care navigation; one approved timeline note and three approved priorities; no raw AI, internal discussion, transcript, or Admin navigation |
| Staff | Search patients, add staff care notes, discuss/assign, record visits; no clinician-section edits or conflict resolution | 303-record registry with search, six today's visits and 297 previous records; exact AI-source jump; Staff note editable while Clinician note is not; high conflict remains review-only |
| Clinician | Staff capabilities plus clinical judgement, AI/voice review, conflict correction, and publication | Jordan Wong shows 22 years, 11 entries, three distinct AI-assisted note types, seven linked facts, an unresolved oral-intake conflict, source quote, decision explanation, and correction control |
| Clinic Admin | Manage membership and clinic AI settings; read-only clinical oversight | Team/invitation/AI settings and database-backed activity log visible; patient record labelled read-only; no add, edit, or resolve controls |

Selected backend P0/P1/Bonus tests reported **101 passed**, the current frontend unit suite reports **19 files / 77 tests passed**, and a fresh-image isolated Chromium run of the core four-role and product-language specs reports **21 passed / 0 failed**. Direct browser checks covered Staff Glance-to-source, longitudinal Clinician review, patient-portal isolation, Admin read-only behavior, and Light/Dark/System appearance. Stale browser selectors for the renamed longitudinal heading and modal invitation flow were corrected. A focused remote-provider regression also reports **3 passed / 0 failed**. The current full Backend + Frontend + Typecheck + Build + Alembic + Playwright release gate still requires one final clean-tree run; these selected results must not be represented as a revision-bound release gate.

## 4. Importance learning: exact behavior and limits

The current mechanism is **deterministic, clinic-level online feature weighting**. It is not an LLM, not a learned neural model, and not a personal user profile.

For each highlight, the base score is:

```text
base = 0.30·critical
     + 0.20·unresolved
     + 0.15·has_clinical_entity
     + 0.15·clinician_confirmed
     + 0.20·exp(-age_days / 90)

learned = mean(clinic feature weights)
final = clamp(base + learned, 0, 1)
```

Features use a bounded non-identifying taxonomy: allergy, medication, diagnosis, follow-up, critical risk, and entry type. Free text and identity material are rejected. Feedback deltas are `pin +0.08`, `accept/manual highlight +0.06`, `comment +0.02`, `edit +0.01`, `reject -0.08`, and `dismiss -0.04`; each feature update is damped by `delta / sqrt(1 + observations)` and clamped to `[-0.20, +0.20]`. Negative learning cannot suppress a critical, unresolved, or clinician-confirmed item.

The database makes the mechanism auditable:

- `importance_feedback_events` records clinic, highlight, actor membership, signal, reason, feature keys, applied delta, idempotency binding, and time.
- `importance_feature_stats` stores the clinic-wide aggregate weight and positive/negative/observation counts for each feature.
- `importance_impressions` records viewer membership, highlight, rank, duration, visibility, exposure value, and deduplicated view event for bias analysis.
- `audit_events` and `domain_events` record visible state changes; snapshots are rebuilt for affected patients after feedback.

The actor/viewer identifiers provide accountability and bias-analysis evidence; they are **not** used to build an individual preference vector. Ranking reads clinic aggregates keyed by `(clinic_id, feature_key)`. Impressions currently remain telemetry and are not consumed by the scoring formula. The current production path performs no randomized exploration or inverse-propensity correction; therefore it must not claim a “10% exploration” experiment. In the UI, **Why this decision?** exposes base components, clinic feedback adjustment, protected status, source, risk floor, confidence state, and what happens when a check fails.

The Clinician browser now supports the brief's exact manual-Highlight interaction: select arbitrary wording inside any AI-assisted note, review the exact quote in a dialog, and create a clinician-confirmed Highlight bound to that immutable version, code-point offsets, and quote context. The priority deliberately retains the selected source wording; a paraphrase or correction must be a separately authored clinical note. Staff, Admin, and Patient do not receive this control. The resulting event updates the same clinic-level bounded feature mechanism and refreshes Current priorities. A clinic-level learning evidence panel for Admin would still make aggregate feature weights and feedback counts easier to inspect without SQL.

## 5. AI trust, real evaluation, voice, and retention

AI content is derived material, never a silent replacement for human documentation. A fact must bind to an immutable source and exact quote before it can enter the decision pipeline. Deterministic risk rules set a floor for allergy, medication status, dose, route, frequency, and severe-condition conflicts; a provider result can raise but not lower that floor. Unsupported, abstained, low/unavailable-confidence, redaction-failed, or unresolved high-risk material goes to clinical review and is blocked from patient publication.

Two real OpenAI evaluation artifacts now exist, and both preserve negative results:

- PriMock57 voice holdout: provider `openai`, model `gpt-4o-transcribe-diarize`, 17 consultations / 2,206 segment decisions, WER 0.2004, medical-entity recall 0.8574, speaker error rate 0.2021, lower-bound accuracy 0.1292 — **Confidence: Low**.
- ACI-Bench fact extraction: provider `openai`, model `gpt-5.1`, 40 consultations / 176 judged facts, 23 true positives, 155 false negatives, lower-bound accuracy 0.0437 — **Confidence: Low**.

These results validate the evaluation-and-abstention path, not clinical model quality. The correct runtime consequence is Low/Unavailable plus review, not a decorative High label. A fixed 500-example redaction evaluation reports 2,500/2,500 expected PHI spans detected, residual PHI 0, and protected clinical-span damage 0; it covers the configured synthetic classes and is not a universal de-identification guarantee.

Voice capture provides encrypted IndexedDB recovery, resumable encrypted chunks, multi-device finalization, bounded FFmpeg preprocessing, provisional captions, immutable final transcript revisions, speaker/timestamp/overlap review, facts linked to transcript/audio ranges, and Clinician publication. The final reviewed transcript remains authoritative; provisional text is ephemeral. Optional local providers remain profile-specific.

Data decay separates retention from deletion. Older eligible bodies can move to encrypted compressed archive storage while their version metadata, checksum, provenance, and audit rows remain. Rehydration verifies the checksum before restoring content. Critical, unresolved, clinician-confirmed, pinned, or otherwise protected material stays active.

## 6. Assumptions, trade-offs, and completion boundary

- All shipped patients, identifiers, messages, recordings, and benchmark transmissions are synthetic or Mock data.
- Precomputed snapshots trade write-time work for a fast, predictable 10-second review path.
- Immutable versions and separate correction records consume more storage but preserve auditability and disagreement history.
- Clinic-level learning avoids covert personal profiling but cannot model individual preferences; telemetry is retained for later fairness/exposure analysis.
- Deterministic risk floors and abstention favor false-positive review over silently suppressing unsafe content.
- Admin-configured provider keys and task-specific fast/careful model routing are operational controls, not proof that a selected model is clinically adequate.
- The current OpenAI evaluations are explicitly Low; patient publication still requires human approval and source checks.

Before final delivery, run and preserve a clean, revision-bound full gate, then bind the final benchmark, browser report, source SHA/tree state, and OCI image digest into one evidence package. The manual AI-phrase Highlight interaction, direct Timeline author/provenance contract, and Staff-request/Clinician-approval/withdrawal workbench are implemented in the current working tree; they must still be included in that final revision-bound browser gate. Historical candidate evidence remains under [`docs/delivery/`](./delivery/) and must not be used to attest to later working-tree changes.
