# Nightingale technical overview

Nightingale is a shared patient-care workspace for clinical teams. It helps care staff and clinicians understand current priorities, document care, coordinate follow-up, and review AI-assisted notes against their supporting sources. Clinic administrators manage access and review activity without editing clinical documentation. Patients use a separate My Care portal.

## Product experience

The clinical workspace follows the way a care team reviews a patient record:

1. Open **Patients** and select a patient.
2. Review up to five **Current priorities**.
3. Open **Source details** to see the supporting note, author, date, exact wording, and whether the source is current or historical.
4. Read and update the **Care timeline** within the user's permitted section.
5. Use **Team discussion** to mention a colleague, assign follow-up, reply, and resolve a thread.
6. Use **Change history** to understand who changed a note and when, compare saved versions, or restore an earlier version without deleting history.
7. Review visit recordings, correct the transcript, confirm clinical findings, and publish the reviewed note.

Patients sign in separately. Their portal receives a deliberately narrower view that excludes internal discussions, AI working material, raw transcripts, audio, and ranking details.

## Architecture

![Nightingale system architecture](./architecture.svg)

The browser application uses React, TypeScript, Vite, TanStack Router and Query, shadcn/ui, and Tiptap. FastAPI serves the application boundary and the production frontend from the same origin. SQLModel and Alembic manage PostgreSQL data. A separate worker processes durable text and voice jobs.

The main data chain is shown below:

![Nightingale clinic-scoped data model](./schema.svg)

Care notes are stored as entries with immutable saved versions. Comments and current-priority cards point to a specific saved version and text span. Restoring an earlier note creates a new current version rather than deleting later history.

## Identity and permissions

Every protected request resolves the signed-in user's active clinic membership on the server. Browser-supplied clinic, role, and author values are not trusted as authority.

- **Care staff** can document care in the staff section and collaborate with the team.
- **Clinicians** can document clinical judgement, review AI-assisted material, and publish reviewed visit notes.
- **Clinic administrators** can manage members and inspect activity but cannot change clinical content.
- **Patients** can read patient-facing information and submit their own insights or recordings through My Care.
- **Background workers** receive narrowly scoped access to create derived records for an assigned clinic job.

Clinic-scoped foreign keys and PostgreSQL row-level security reinforce application authorization. Browser sessions use secure, same-origin cookies, and protected responses are not stored in shared caches.

## Source-supported care

Current priorities are read from a precomputed snapshot, so opening a patient record does not wait for remote processing. Each priority points to an immutable note version and exact supporting wording. The interface presents the source title, author, date, quoted text, and historical status while keeping internal identifiers and integrity metadata out of the clinical workflow.

AI-assisted entries remain distinguishable from human notes. They carry a review message and do not overwrite human documentation. A clinician correction or publication creates a separate record with its own source links.

## Team collaboration and note history

Staff and clinician notes are separate entries, allowing the two sections to progress independently. Concurrent updates to the same entry are compared against the version that was opened. If the note changed in another session, Nightingale preserves the user's draft and asks them to review the latest saved note.

Selected-text discussions retain enough source context to reconnect to the intended wording after later edits. Mentions and assignments use clinic member names and roles in the interface. Administrators can inspect discussions as read-only oversight.

## Visit recordings

The recording workflow supports temporary live captions, encrypted local recovery, resumable upload, final transcription, speaker and timing review, confidence indicators, transcript correction, clinical findings, and publication by a clinician. Temporary captions are never treated as the reviewed record.

The default local configuration keeps remote text and audio processing off. Optional processing integrations are enabled only through explicit deployment configuration. See the [voice pipeline](./VOICE_PIPELINE.md) and [processing inventory](../MODEL_INVENTORY.md) for operational gates and claim boundaries.

## Privacy and data lifecycle

Clinical text, discussions, current-priority payloads, transcripts, findings, and audio are encrypted with clinic-bound authenticated encryption. Text approved for remote processing first passes configured de-identification and residual checks; a failed check stops the remote call and records a review state.

Older eligible payloads can move to encrypted compressed storage while their saved-version metadata, activity record, and source links remain available. Rehydration verifies integrity before returning content to the active record.

## Operations and verification

The root [README](../README.md) contains the product workflow, local startup, configuration, and release checks. Additional references include:

- [Voice pipeline](./VOICE_PIPELINE.md)
- [Deployment guide](../deployment-docker-compose.md)
- [Attribution](../ATTRIBUTION.txt)
- [Third-party notices](../THIRD_PARTY_NOTICES.md)
- [Delivery and historical verification material](./delivery/BUILD_DELIVERY.md)

Delivery-specific scenarios, historical measurements, and recording instructions are isolated under [`docs/delivery/`](./delivery/) so that the primary documentation remains focused on clinical users and operators.
