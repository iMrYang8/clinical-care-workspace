# Nightingale Review Standard

**The evidence required to pass 16 scenarios and two deployment axes**

Base `0c437fe` (exported as `9fffc01`) · September 2026 · to be submitted before reviewing any candidate version

---

## 0 · Purpose

This is a preregistration document. Before any other candidate version is reviewed, it fixes three things: what counts as evidence, what fails automatically, and how the remaining alternatives are compared. Once committed, that commit's SHA identifies this standard. Subsequent changes are revisions, appended to the end of this section with the section changed, the reason, the date, and whether already-assessed candidates were rerun.

This standard does not rank candidate versions. It compares **different implementations at each point of divergence**, then combines the selected implementations. It permits adopting no changes from a candidate or adopting all of them.

Sections 2 and 3 contain the main requirements: 16 scenarios and two deployment axes, with evidence specified for each requirement. The constraints in Section 4 are parameters for those scenarios, not a separate compliance checklist. Each constraint identifies the requirement that needs it; constraints with no dependent requirement are omitted.

The author wrote and improved the base and will now assess alternatives to it. This document is therefore submitted before any candidate is opened, and its SHA identifies the version. Requirements and numerical values in Sections 2 and 3 are not changed after assessment starts without a recorded reason and reruns of previously assessed candidates. The author's own code is candidate #0 at every point of divergence and is subject to the same evidence requirements and rejection criteria. Its assessment is in [`SELF_AUDIT.md`](SELF_AUDIT.md).

All results under this standard use synthetic or public data. None constitutes clinical validation.

---

## 1 · Verdict rules

### 1.1 Two requirement types

Each requirement has one of two types. Each scenario also specifies its core-failure conditions:

- **CORE**: without it, the scenario has no usable capability.
- **COMPLETE**: required for the complete scenario, not optional; its absence still leaves the core capability usable.
- **Core failure**: a specified behaviour that, once observed, cannot be classified as PARTIAL regardless of other benefits.

The classification is fixed in this document. If a constraint in Section 4 makes a COMPLETE requirement CORE, make that change before comparing any candidate and apply it consistently to all candidates.

### 1.2 Verdicts

| Verdict | Exact meaning |
|---|---|
| **SURVIVES** | All applicable CORE and COMPLETE requirements are met with sufficient evidence, and no core failure has been observed. |
| **PARTIAL** | All CORE requirements are met, with no core failure; at least one COMPLETE requirement is confirmed unmet, with enough evidence to identify the gap. |
| **DOES NOT** | A CORE requirement is confirmed absent, or a core failure has been observed. |

“Sufficient evidence” does not mean “all tests pass”. Confirming that an interface is absent is sufficient evidence of a gap; there is no need to claim that the absent interface was tested. An author's statement that the code “should be fine” is not evidence.

### 1.3 Three evidence levels

- **executed**: an input was changed and the resulting behaviour observed; a test input was applied.
- **inspected**: code was read and the conclusion follows from its logic, but it was not run. Code review produces this level of evidence. It is admissible only when the requirement does not involve either deployment axis.
- **not produced**: the counterfactual cannot be applied under current conditions. The reason must be stated.

A test that asserts a constant is not a test input. Asserting `false_nkda is False` on an extractor that cannot produce false NKDA does not test that behaviour. Two such tests passed without testing it for several weeks in this project. This is why this section exists.

For behaviour involving either deployment axis, only **executed evidence from two processes** is accepted.

### 1.4 Missing evidence

Record each requirement's evidence status as `SUFFICIENT` or `UNVERIFIED`, separately from execution status (`PASS` / `FAIL` / `NOT_RUN`). Apply the following order:

1. Constraints or acceptance conditions are not yet determined: record `NOT_READY` and exclude the item from grading.
2. Evidence already shows a core failure or missing CORE capability: record **DOES NOT** immediately.
3. Evidence is incomplete and assessment is still open: retain `UNVERIFIED`. Passed sub-requirements may be shown, but the whole scenario receives neither SURVIVES nor a final PARTIAL verdict.
4. Evidence remains incomplete at assessment close: record **DOES NOT — required evidence missing**, specifying what is missing, which materials were requested and the timeline. Record this separately from an observed test failure.
5. Evidence is complete: assign the verdict under Section 1.2.

This rule prevents failed items from remaining indefinitely UNVERIFIED. It also distinguishes a scenario that no one can test under the available conditions, such as S06, from an observed implementation failure.

---

## 2 · The 16 scenarios

Each scenario specifies the user outcome, numbered requirements, evidence for each requirement, core-failure conditions and dependent constraints. Time limits appear in the requirements that use them, with their stated derivation. Step-by-step procedures are in the appendix as execution materials.

Each scenario's constraint list contains only scenario-specific constraints. A01, P01, V04, R02 and I07 apply to all scenarios and are not repeated. O04, O06, I01, I02, I03, I04, I05 and I06 apply specifically to integration.

### S01 · Patient without email

**User outcome:** A patient without email can access the consultation and patient-facing workflows.

| ID | Type | Requirement | Required evidence |
|---|---|---|---|
| S01-R01 | CORE | A patient without email can establish or match the correct identity and independently complete three core flows: viewing shared notes, submitting their own account, and confirming medication instructions | executed: complete enrolment and all three flows end to end; retain before-and-after states |
| S01-R02 | CORE | Shared, duplicate or changed contact details do not incorrectly merge identities or allow unauthorized access | executed: two patients share one number, then one changes their number; check both identities and the access matrix |
| S01-R03 | CORE | The patient interface shows only content permitted for that role, excluding internal comments, raw model materials, scores, transcripts and audio | executed: read as the patient and compare each field against the allowlist |
| S01-R04 | COMPLETE | Credential expiry and recovery work without reintroducing an email requirement | executed: expire credentials, complete recovery, and record the prompt text at each step |
| S01-R05 | COMPLETE | Claim codes expire, are unusable after expiry, and resist guessing by enumeration | executed: use an expired code; repeatedly try adjacent code values |
| S01-R06 | COMPLETE | Clinicians, staff and patients see content and available actions appropriate to their roles | executed: view the same synthetic record under all three roles |

**Core failure:** An email prerequisite blocks core access, or access is granted under the wrong identity.

**Dependent constraints:** E08, P04, V03, V08, V09

---

### S02 · A one-line change to a route handler

**User outcome:** A route error does not expose patient records to the wrong clinic.

| ID | Type | Requirement | Required evidence |
|---|---|---|---|
| S02-R01 | CORE | Authorized access within a clinic succeeds; cross-clinic access is blocked. Unauthorized rows visible or writable both equal 0 | executed: cross-read and cross-write between clinics A and B, including identical local IDs; retain API responses and database states before and after |
| S02-R02 | CORE | Remaining controls preserve the boundary when one route filter is removed or the wrong tenant context is passed | executed: inject the fault into a temporary test variant and repeat R01. Record the change and the control still enforcing the boundary. Response codes alone are insufficient |
| S02-R03 | CORE | Database enforcement does not depend on correct arguments from the application | executed: connect directly to the database as the runtime role and run cross-clinic queries |
| S02-R04 | COMPLETE | Bulk operations, related queries and export entry points receive equivalent checks, with exposure counts reported per path | executed: request the same resources through each entry point; distinguish an absent entry point from an untested one |
| S02-R05 | COMPLETE | After the fault is injected, at least one existing regression test fails or at least one alert fires, and the report names which; that evidence identifies the point of failure and contains no sensitive data | executed: retain the faulty variant, run regression and alert checks |
| S02-R06 | COMPLETE | Restoring the test variant leaves no residual changes | executed: rerun R01 after restoration |

**Core failure:** Any unauthorized cross-clinic read or write within scope.

**Dependent constraints:** E10, P03, P05

---

### S03 · Logs, errors and observability outputs

**User outcome:** Logs do not expose patient information, and retention is controlled.

| ID | Type | Requirement | Required evidence |
|---|---|---|---|
| S03-R01 | CORE | Normal and error paths do not write prohibited fields to logs, error reports or observability data | executed: inject unique markers into the path, query string, headers and body; assert their absence from output records |
| S03-R02 | CORE | Request-validation failures do not echo rejected values or free-text messages | executed: submit a malformed request containing markers and inspect the error response and logs |
| S03-R03 | CORE | Audit free text is encrypted at rest; machine-readable metadata uses an allowlist | executed: read the ciphertext and metadata fields of audit rows |
| S03-R04 | COMPLETE | Patient searches do not pass parameters through GET query strings, and request bodies are not logged | executed: inspect the search API method and log contents |
| S03-R05 | COMPLETE | Every external output destination—proxy, APM, crash-reporting platform and background job—is listed and verified individually | executed; where only a downstream written statement is available, record not produced and identify the destination |
| S03-R06 | COMPLETE | Retention follows P09–P15, and deletion at expiry is verifiable | executed: run a short-retention experiment; record evidence for the full retention period separately |

**Core failure:** PHI appears in output records.

**Dependent constraints:** O08, P09, P10, P11, P12, P13, P14, P15, V06

---

### S04 · Redaction before model input

**User outcome:** Patient information is not sent to the model with a request.

| ID | Type | Requirement | Required evidence |
|---|---|---|---|
| S04-R01 | CORE | Model inputs contain no PHI | executed: use a fixed sample set and inspect each actual outgoing request |
| S04-R02 | CORE | Redaction performance is measured, reporting both missed PHI and incorrectly redacted clinical spans | executed: use a fixed evaluation set and report both counts |
| S04-R03 | CORE | Tasks fail closed when the redaction component is unavailable; they do not continue sending after switching to best-effort redaction | executed: make the component unavailable and check whether requests are still sent |
| S04-R04 | COMPLETE | Covered PHI categories are reported without claiming general de-identification capability | written: category list and coverage limitations |
| S04-R05 | COMPLETE | The audio path is also handled, or explicitly declared outside coverage | executed, or a written scope statement |

**Core failure:** PHI appears in a model request.

**Dependent constraints:** P08, P13, R05, V02, V05, V06, V07

---

### S05 · Clinic B opens on Monday

**User outcome:** The new clinic can operate that day without receiving any content from other clinics.

| ID | Type | Requirement | Required evidence |
|---|---|---|---|
| S05-R01 | CORE | Preflight lists unmet requirements individually; satisfying them changes readiness status and enables the corresponding actions | executed: move from not ready to ready and retain both preflight outputs |
| S05-R02 | CORE | A new clinic does not inherit another clinic's keys, patients, members or permissions | executed: after creation, attempt to access A's resources as B |
| S05-R03 | CORE | Every preflight item includes an evidence ID or reason code, not just a Boolean | executed: inspect each item's evidence fields |
| S05-R04 | COMPLETE | Observed configuration can be reported as observed, even when values exceed recommended ranges | executed: inject out-of-range configuration; preflight must report it accurately rather than fail |
| S05-R05 | COMPLETE | Retrying interrupted initialization preserves boundaries and is auditable | executed: interrupt initialization and retry; confirm that existing clinics are unaffected |

**Core failure:** Another clinic's keys, patients or permissions are inherited.

**Source of S05-R04:** This project previously constrained observed values to recommended ranges, causing the API to crash when reporting an out-of-range configuration. Any configuration that can be observed must be reportable as observed.

**Dependent constraints:** P03, P06

---

### S06 · Trilingual code-switching in one sentence

**User outcome:** Downstream reasoning remains valid when a sentence contains Malay, English and Hokkien.

| ID | Type | Requirement | Required evidence |
|---|---|---|---|
| S06-R01 | CORE | Every segment carries three fields — language label, source of the decision, confidence — and fails if any is absent; label values fall within the language set in A08 | executed: use a public code-switching dataset and check all three fields on every segment |
| S06-R02 | CORE | Low confidence or an unidentified language triggers review rather than a definite negative assertion. Fabricated NKDA count is 0 | executed: check invariants, including inputs in unsupported languages |
| S06-R03 | CORE | The same drug name across languages maps to one key; citation-offset integrity is 100% | executed: count retained terms and check citation integrity, including text with diacritics |
| S06-R04 | CORE | Speaker roles are preserved; family-reported information is not treated as equivalent to a clinician's assertion | executed: enter the same fact as a clinician, patient and family member |
| S06-R05 | COMPLETE | Drug names are extracted from simplified and traditional Chinese and Malay sentences, not limited to a few preset names | executed: Chinese non-penicillin drugs, traditional characters, and Han-script drug names in Malay sentences |
| S06-R06 | COMPLETE | Drug names recovered from approximate spelling are flagged rather than silently accepted | executed: enter misspelled drug names and check that review is required |
| S06-R07 | COMPLETE | Evaluation uses real consultation audio in the target three languages | not produced; see E01 for the reason |

**Core failure:** A definite denial of allergy is inferred from an unidentified language.

**No accuracy threshold is set.** No public corpus exists for this language combination, so assigning an accuracy figure would be invented. R01–R03 use invariants rather than accuracy because invariants can be checked on independently annotated data.

**Dependent constraints:** A08, A09, E01, E02, E03, E04, E05, E06, P02, R04, R11, V01, V05

---

### S07 · An allergy mentioned at minute 2

**User outcome:** In a 20-minute consultation, an allergy mentioned at minute 2 reaches the clinician before prescribing.

| ID | Type | Requirement | Required evidence |
|---|---|---|---|
| S07-R01 | CORE | The allergy assertion reaches the clinician before prescribing in the same consultation. Time from the end of the utterance to card display is ≤ 10 seconds at p95 | executed: compare transcript-segment and card-render timestamps for 30 utterances, repeated three times |
| S07-R02 | CORE | Unresolved conflicts block publication | executed: create a conflict and attempt publication |
| S07-R03 | CORE | Information recorded early is not removed or deprioritized by content from the following 18 minutes | executed: enter the information at minute 2 and check that it remains among the priorities at consultation end |
| S07-R04 | COMPLETE | A conflict card links to the original utterance, with matching quotation text and offsets | executed: follow the card link and compare the text |
| S07-R05 | COMPLETE | The interface identifies who is responsible when processing ownership is not obtained or the task moves to manual handling | executed: inspect interface text after handover |

**Basis for 10 seconds:** Clinicians read about two to three words per second; 10 seconds is roughly one sentence. The requirement comes from the scenario's “before prescribing”, not a service-level agreement.

**Core failure:** The workflow reaches prescribing before the allergy information has been surfaced.

**Dependent constraints:** A05, A06, A07, E11, P02

---

### S08 · The model hangs for 45 seconds

**User outcome:** When the model stalls, the clinician knows what is happening and can continue working.

| ID | Type | Requirement | Required evidence |
|---|---|---|---|
| S08-R01 | CORE | Waiting is bounded: text stages at 15 / 30 / 75 seconds; voice overall at no more than 180 seconds | executed: inject 45-second and 200-second pauses and assert that deadlines apply |
| S08-R02 | CORE | The interface shows current status and elapsed time from 5 seconds onward | executed: capture the interface during the hang |
| S08-R03 | CORE | The user can retry | executed: trigger a retry after the hang |
| S08-R04 | CORE | After timeout, the task has a definite state, such as `timed_out`, rather than remaining indeterminate | executed: read the task state after timeout |
| S08-R05 | COMPLETE | Retrying does not duplicate external side effects | executed: retry after an external operation has occurred but before its acknowledgement returns |
| S08-R06 | COMPLETE | Serial stages share one overall deadline rather than restarting the timer for each stage | executed: create a multistage task and inject pauses |

**Basis for the values:** The 15 / 30 / 75-second values were derived and tested in the existing implementation. The 180-second voice limit is three times the upper expected duration: 45 seconds is the specified fault, and after 180 seconds the clinician has left the screen.

**Core failure:** Indefinite waiting with no status displayed.

**Dependent constraints:** E10, E11, O07, R04, V11, V12

---

### S09 · Provider returns 503 for one hour

**User outcome:** During an hour without model service, the clinician can still read existing content and continue working.

| ID | Type | Requirement | Required evidence |
|---|---|---|---|
| S09-R01 | CORE | Stored priorities and notes remain readable and are labelled stale | executed: inject sustained 503 responses and inspect the interface |
| S09-R02 | CORE | Failures are not silent, and degraded operation does not report false success | executed: inspect states and message text |
| S09-R03 | CORE | Stale labels include generation time, not just the word “stale” | executed: inspect the label contents |
| S09-R04 | COMPLETE | Every minimum function listed in V11 remains available | executed: complete each function |
| S09-R05 | COMPLETE | Circuit-breaker state is observable, and service resumes automatically after provider recovery | executed: restore the provider and check automatic recovery |
| S09-R06 | COMPLETE | For a new patient with no history, the interface explicitly reports unavailability rather than showing a blank screen | executed: repeat R01 for a patient with no history |

**Core failure:** Stale data is presented as current.

**Dependent constraints:** E10, O07, R06, V02, V11, V12

---

### S10 · Two clinicians edit simultaneously at 9:14

**User outcome:** When two people edit the same note, neither person's changes disappear silently.

| ID | Type | Requirement | Required evidence |
|---|---|---|---|
| S10-R01 | CORE | The number of lost accepted edits is 0 | executed: run the concurrent-editing harness three times |
| S10-R02 | CORE | Conflicts are visible to users and their input is retained | executed: inspect the interface and retained drafts |
| S10-R03 | CORE | No automatic merging occurs | executed: test same-field and different-field edits separately |
| S10-R04 | CORE | Late requests based on old versions are rejected without discarding user input | executed: B submits an old version; inspect the response and draft |
| S10-R05 | COMPLETE | The conflict interface shows three versions: local, remote and common base | executed: inspect the interface structure |
| S10-R06 | COMPLETE | Derived content, including comments and highlights, still refers to the correct version after a conflict | executed: inspect reference targets after conflict resolution |

**Core failure:** An accepted edit is silently discarded.

**Dependent constraints:** O01, R10

---

### S11 · A link was generated but never received

**User outcome:** Staff can see that delivery failed and know what to do next.

| ID | Type | Requirement | Required evidence |
|---|---|---|---|
| S11-R01 | CORE | Delivery status is accurate. The number of queued messages shown as delivered is 0 | executed: use a stub provider that drops messages and inspect interface status |
| S11-R02 | CORE | Resending and revocation follow V10 | executed: attempt to revoke an OTP and a clinical message separately |
| S11-R03 | CORE | Receipt state transitions are valid; invalid transitions are rejected | executed: attempt invalid transition sequences |
| S11-R04 | COMPLETE | Provider callbacks are checked for signature and ownership; forged callbacks are rejected | executed: submit callbacks with invalid signatures and mismatched ownership |
| S11-R05 | COMPLETE | After a delivery failure, three flows can still be completed: medication correction, note publication, and conflict handling | executed: complete all three flows after delivery failure |
| S11-R06 | COMPLETE | The actual delivery chain has been verified | not produced; see E08 for the reason |

**Core failure:** A queued message is shown as delivered.

**Dependent constraints:** E08, V03, V08, V10

---

### S12 · Wrong dosage in a patient summary

**User outcome:** Contradictory medication instructions are not sent to the patient.

| ID | Type | Requirement | Required evidence |
|---|---|---|---|
| S12-R01 | CORE | Conflicting medication instructions cannot be published | executed: create a conflict and attempt publication |
| S12-R02 | CORE | Both sources can be traced to their original records | executed: follow each source from the conflict card |
| S12-R03 | CORE | Staff cannot close the conflict themselves; the role specified in A03 must handle it | executed: attempt closure as staff |
| S12-R04 | CORE | Unresolved conflicts do not appear in the patient projection | executed: read as the patient |
| S12-R05 | COMPLETE | Doses and units are checked against the formulary, and out-of-range values are blocked | executed: submit an out-of-range dose |
| S12-R06 | COMPLETE | Earlier recipients can see the correction after a corrected version is published | executed: publish a corrected version and view it as the patient |

**Core failure:** A conflicting dose is published.

**Dependent constraints:** A02, A03, A04, A07, A09, A10, E06, E07, E12, R11

---

### S13 · A nurse records an allergy; the patient denies it

**User outcome:** Both contradictory allergy statements are retained for clinician adjudication. The system does not decide for the clinician.

| ID | Type | Requirement | Required evidence |
|---|---|---|---|
| S13-R01 | CORE | Both assertions are retained, each labelled with source, role and language | executed: enter statements as nurse and patient and inspect the conflict card |
| S13-R02 | CORE | Source priority does not automatically determine the outcome | executed: check whether either assertion is automatically accepted |
| S13-R03 | CORE | The NKDA priority rule is visible to the clinician | executed: inspect interface text |
| S13-R04 | CORE | Patients can state information about themselves in their own channel without being blocked by the publication gate for unresolved conflicts | executed: submit as the patient while a conflict remains unresolved |
| S13-R05 | COMPLETE | The clinician's adjudication is audited, including a reason field | executed: read the audit row after adjudication |
| S13-R06 | COMPLETE | A denial that names no drug is not promoted to a definite “no allergies” assertion | executed: enter a denial without naming a drug |

**Core failure:** Either assertion is silently discarded.

**Source of S13-R04:** This project previously applied the publication gate to patient statements, preventing patients from writing in their own channel. The gate is intended to prevent clinical content from being sent to patients; patients writing their own information is the reverse direction.

**Dependent constraints:** A03, A05, A06, E07, E12, P03, P07

---

### S14 · A meaningful number

**User outcome:** The clinician can judge whether a displayed confidence value is trustworthy.

| ID | Type | Requirement | Required evidence |
|---|---|---|---|
| S14-R01 | CORE | The confidence value declares its qualification status; unevaluated means unqualified | executed: construct a highlight with no evaluation |
| S14-R02 | CORE | Unevaluated highlights are neither rendered as ready nor included in the patient projection | executed: inspect the list and patient interface |
| S14-R03 | CORE | Model-derived content is not labelled human-confirmed | executed: inspect provenance labels and fingerprints |
| S14-R04 | COMPLETE | An explanation states why the judgement was made, how it can be falsified, and its consequences | executed: inspect the explanation |
| S14-R05 | COMPLETE | The confidence value's sample count is visible | executed: check whether the interface shows the denominator |
| S14-R06 | COMPLETE | The “needs clinical review” queue is protected against bulk clearing | executed: attempt a bulk operation on the queue |

**Core failure:** An unqualified number is presented as trustworthy.

**Dependent constraints:** A02, A09, A11, R12

---

### S15 · Bias in the learning loop

**User outcome:** Learning from use does not lower the ranking of safety items because a tired clinician ignored one.

| ID | Type | Requirement | Required evidence |
|---|---|---|---|
| S15-R01 | CORE | A single update does not exceed the bound set in constraint A12 and is rejected if it does; every update is recorded in the audit log | executed: submit an update above the bound and check rejection; read the audit rows |
| S15-R02 | CORE | Allergy and medication items have protected minimum rankings, with 0 violations | executed: inspect ranking after repeated ignores |
| S15-R03 | CORE | Learning is limited to the clinic, with no cross-clinic aggregation | executed: generate activity in clinic A and inspect clinic B's ranking |
| S15-R04 | COMPLETE | Audits are append-only; reversal adds a new record | executed: attempt to modify historical records |
| S15-R05 | COMPLETE | New weights do not affect live ranking until explicitly approved | executed: produce new weights and check whether live ranking changes |
| S15-R06 | COMPLETE | Exposure bias is measured and reported by stratum, and measured bias is down-weighted or excluded in the next ranking pass. Recording without acting does not satisfy this requirement | executed: currently measured but not corrected; record the gap |

**Core failure:** A protected minimum ranking is violated.

**Dependent constraints:** A03, P12, P18, R10

---

### S16 · A highlight cites an edited note

**User outcome:** A citation continues to refer to the version it originally cited.

| ID | Type | Requirement | Required evidence |
|---|---|---|---|
| S16-R01 | CORE | Anchors are version-bound; editing the source does not change the citation target | executed: edit the source and inspect the citation target |
| S16-R02 | CORE | Version history is append-only; published versions cannot be modified in place | executed: attempt to modify a published version directly |
| S16-R03 | COMPLETE | The interface can show the cited and current versions side by side | executed: check whether the view exists |
| S16-R04 | COMPLETE | Edited content re-enters review under A05 | executed: edit approved content |
| S16-R05 | COMPLETE | If a cited version is archived or deleted, the citation shows an explicit state rather than a blank result | executed: archive the source version and inspect the citation |
| S16-R06 | COMPLETE | Checksums are verified when archived content is restored | executed: restore an archive and inspect the verification step |

**Core failure:** A citation points to changed text.

**Dependent constraints:** A02, A03, A05, P10, R12

---

## 3 · The two deployment axes

These axes must be assessed for each change that affects them. Each such change must answer the questions in Section 3.3.

### 3.1 D01 · Multi-replica scale-out

| ID | Type | Requirement | Required evidence |
|---|---|---|---|
| D01-R01 | CORE | Ownership and completion semantics are correct during concurrent execution and takeover; replica count does not corrupt state | executed with two processes: hold two workers at a barrier before claiming, then release both |
| D01-R02 | CORE | A holder whose lease has expired or been revoked cannot overwrite valid results. Designs without leases are allowed if they satisfy this behaviour | executed with two processes: A claims and pauses until expiry; B takes over and writes; resume A and attempt submission |
| D01-R03 | CORE | External side effects from retries and interruptions follow the confirmed semantics; an unknown result is not reported as success | executed with two processes: terminate before the external action, after it occurs but before acknowledgement, and after acknowledgement but before completion |
| D01-R04 | CORE | Any replica can serve events, visible within 2 seconds; reconnection and history handling preserve clinic boundaries | executed with two processes: B generates an event, A's client receives it; disconnect and resume through B |
| D01-R05 | CORE | Every cap, quota and cache has the actual scope it declares | executed with two processes: for a deployment-wide cap K, attempt connection K+1 across the two replicas |
| D01-R06 | COMPLETE | After an instance crash, recover, terminate or hand over under O05; users see the actual state | executed with two processes: terminate instances during a job, live audio session and event stream separately |
| D01-R07 | COMPLETE | The stated time limits hold under R07's load and resources, with the measurement scope reported | executed with two processes: run a load test with fixed resources |

**Basis for 2 seconds:** The event stream polls every 1 second. Missing one poll is acceptable; missing two is a defect.

**Core failure:** A deployment-wide cap changes with replica count, or an expired holder's write overwrites a valid result.

**Dependent constraints:** R01, R03, R07, R08, R09, E09

### 3.2 D02 · Cross-clinic isolation

| ID | Type | Requirement | Required evidence |
|---|---|---|---|
| D02-R01 | CORE | APIs and data operations respect clinic and role boundaries without rejecting all legitimate access | executed: cross-read and cross-write against the permission matrix; verify allowed and prohibited cases |
| D02-R02 | CORE | Background jobs, derivation and learning operations retain the correct authorization scope from creation through recovery | executed: create jobs in each clinic and inspect result ownership after takeover, retry and restart |
| D02-R03 | CORE | Caches, indexes and derived content are not reused across clinics | executed: A fills the cache, then B requests the same local ID |
| D02-R04 | CORE | Live streams, resumption and replay preserve boundaries and respect permission changes | executed: try another user's cursor, reconnect across replicas, and continue waiting after permission revocation |
| D02-R05 | CORE | Files, previews and download links do not bypass permissions at retrieval time | executed: copy a link, revoke permission, then retrieve it |
| D02-R06 | CORE | Exports maintain the correct scope from request through download | executed: change permissions during generation, then download as two identities |
| D02-R07 | COMPLETE | Clinic creation, invitations and initialization preserve boundaries and remain auditable on normal, retry and failure paths | executed: see S05-R03 |
| D02-R08 | CORE | Membership revocation, role changes and legitimate multi-clinic employment apply to sessions, jobs and links within 1 second | executed: continue using the original sessions, jobs and links after increasing or reducing permissions |

**Core failure:** Any unauthorized cross-clinic read or write.

**Dependent constraints:** P03, P05, P06, P16, P17, P18

### 3.3 Questions each change must answer

**Multi-replica:**

1. Where does this change store state: process, request, Postgres or an external system?
2. If state is process-local, state whether one copy per replica is the **correct** scope. The test is whether the declared scope matches the actual one: a per-connection limit lives entirely within one process, so process-local is its full scope; a limit declared to cover the whole deployment does not.
3. If the change involves jobs, how does it interact with claiming and commit fencing?
4. If it involves streaming, can any replica serve the stream from database state, and can clients resume by cursor?
5. Evidence: the change's two-process results and a specific test whose outcome would differ if the single-replica assumption did not hold.

**Cross-clinic:**

1. Which tables does the change read and write?
2. For every write path, is there a `WITH CHECK`, not just `USING`, and a test that attempts a write from the wrong clinic?
3. Is each new function `SECURITY DEFINER`? If so, why, and what limits its returned set?
4. Does the conclusion still hold when `FASTAPI_ENV` is not `development`?
5. Evidence: passing RLS coverage tests plus the per-path tests required in question 2.

---

## 4 · Constraints

These are parameters for Sections 2 and 3. Each identifies its dependent requirements; constraints with no dependent requirement are omitted.

Each row is an assertion, not a question. It applies unless changed or crossed out.

**How to complete the decision column:**

| Situation | Entry |
|---|---|
| Applies | Leave blank or write “keep” |
| Needs modification | **Write the complete replacement constraint.** Do not write only “partly applies” or “needs adjustment”. The replacement takes effect; retain the original in the left column for reference |
| Does not apply | Write “cross out”, with a reason and date. Retain the original text |
| Applies to only some assets | Split into `.a` and `.b` rows and decide each separately; do not cross out the entire row |

The four rows marked ⚠ determine scope or support specific conclusions. A01 and P01 define the whole assessment's scope; crossing them out requires reconsidering the other constraints. R08 and R09 are prerequisites for D01 conclusions; crossing them out reverses the corresponding self-audit conclusions.

### 4.1 Use and clinical constraints

| ID | Assumed constraint | Dependent requirements | Decision |
|---|---|---|---|
| A01 | ⚠ This round supports demonstration and research evaluation using synthetic data. It is not a clinical pilot and is not used for actual care | All | |
| A02 | The system is not being submitted for registration as Software as a Medical Device. It presents information for clinician review; it does not issue diagnoses, recommend treatment, or automatically rewrite patient-visible clinical text | S12, S14, S16 | |
| A03 | Confirming clinical facts, publishing notes and sending content to patients all require the clinician role | S12, S13, S15, S16 | |
| A04 | The attending clinician resolves disputes about formulary content | S12-R02 | |
| A05 | Approved content re-enters review after modification | S07, S13, S16-R03 | |
| A06 | Allergy information must reach the clinician before prescribing in the same consultation | S07-R01, S13 | |
| A07 | Unresolved clinical conflicts stop automated processing and trigger human review; the clinician taking over is responsible | S07-R02, S12-R01 | |
| A08 | Service languages: English, Malay, Mandarin (simplified and traditional characters), and Hokkien (romanization and Han characters) | S06 | |
| A09 | Drug, dose and allergy-conflict decisions use a versioned clinic formulary. No external reference dataset is assumed | S06, S12, S14 | |
| A10 | Staff cannot close unresolved medication conflicts themselves | S12-R03 | |
| A11 | Unevaluated model outputs are treated as unqualified, not neutral | S14-R01 | |
| A12 | Bound on a single self-learning update. Proposed for this round: relative weight change no greater than ±0.20, damped by 1/√n. This figure needs confirmation; it comes from the existing implementation rather than a clinical basis | S15-R01 | |

### 4.2 Privacy and permissions

Retention periods are proposed values. The 6-year audit-log period follows clinical record-retention practice, but requires confirmation; I am not certain of the exact regulatory provision supporting it.

| ID | Assumed constraint | Dependent requirements | Decision |
|---|---|---|---|
| P01 | ⚠ Synthetic data only. No real recordings or real medical records. Real materials require ethics approval, which is not assumed to have been obtained for this round | All | |
| P02 | Inform patients and obtain consent before recording; store consent status with the recording | S06, S07 | |
| P03 | Roles: clinician, staff, patient, administrator, worker and platform operator | S02, S05, S13, D02-R01 | |
| P04 | A household may share one phone number as the patient entry point | S01-R02 | |
| P05 | Employment across clinics uses separate memberships, not shared sessions | S02, D02-R08 | |
| P06 | This round has no emergency break-glass access mechanism | S05-R02, D02 | |
| P07 | Patients may view and correct records about themselves; the clinic handles requests and the system provides export | S13, D02-R06 | |
| P08 | Patient data does not leave Singapore, including for transient cross-region processing | S04, V02, V05 | |
| P09 | Audio is retained for 30 days | S03-R03 | |
| P10 | Transcripts and summaries are retained for the lifetime of the note version | S03-R03, S16-R01 | |
| P11 | Operational logs are retained for 30 days | S03-R03 | |
| P12 | Audit logs are retained for 6 years | S03-R03, S15-R01 | |
| P13 | Model inputs and outputs are not retained after task completion | S03-R03, S04 | |
| P14 | Backups are retained for 35 days | S03-R03 | |
| P15 | Deletion is soft initially and hard at expiry | S03-R03 | |
| P16 | Permission revocation affects sessions and links within one request cycle, within 1 second | D02-R08 | |
| P17 | Background jobs recheck authorization scope both when claiming and when submitting results | D01-R02, D02-R02 | |
| P18 | No cross-clinic learning, statistics or model updates | S15, D02 | |

### 4.3 Providers and network

| ID | Assumed constraint | Dependent requirements | Decision |
|---|---|---|---|
| V01 | ASR uses local faster-whisper or a deterministic fixture | S06 | |
| V02 | The language model runs locally or uses a provider behind a circuit breaker. Inference runs on the local machine or within Singapore | S04, S09 | |
| V03 | Messaging uses only the Twilio sandbox | S01, S11 | |
| V04 | Storage uses local Postgres and the filesystem | All | |
| V05 | No provider receives raw audio unless the clinic enables `remote_audio_egress_enabled` | S04-R03, S06 | |
| V06 | Providers are assumed not to retain data, use it for training, or allow human access | S03-R02, S04 | |
| V07 | Evidence for V06 consists of account settings or data-processing agreement terms. Without that evidence, the provider receives no patient data | S04 | |
| V08 | Patient channels are Twilio SMS and WhatsApp; email is optional | S01, S11 | |
| V09 | Patients without email enrol using a claim code delivered in person | S01-R01 | |
| V10 | Delivered one-time passcodes can be revoked; delivered clinical messages cannot | S11-R02 | |
| V11 | Minimum functions during provider outages: read stored priorities and notes, write notes, and queue messages | S08, S09-R03 | |
| V12 | Fallback uses a local model or deterministic rules and never reports false success | S08, S09-R02 | |

### 4.4 Runtime environment

R09 addresses a gap found while preparing this document. Lease expiry depends on wall-clock time. Clock drift between replicas can directly violate D01-R02, but this condition was previously unstated.

| ID | Assumed constraint | Dependent requirements | Decision |
|---|---|---|---|
| R01 | Deployment uses Linux containers under Docker Compose on one machine, without an orchestration system | D01 | |
| R02 | The database is Postgres 16 | All | |
| R03 | Multi-replica tests use two application processes connected to one Postgres instance | All D01 | |
| R04 | Compute resources: CPU-only inference, 16 GB RAM, no GPU | S06, S08 | |
| R05 | The system runs online; outbound connections are permitted only to configured providers | S04, V05 | |
| R06 | This round has no offline mode | S09 | |
| R07 | Target scale: 2 clinics, 20 users, 8 concurrent live sessions and 2 replicas | D01-R05, D01-R07 | |
| R08 | ⚠ All caps are deployment-wide by default. Per-instance caps must be explicitly declared and justified in the code | D01-R05 | |
| R09 | ⚠ All replicas synchronize their system clocks through NTP, with a maximum allowed difference of 1 second | D01-R02 | |
| R10 | The timezone is Asia/Singapore; date-boundary behaviour is evaluated in that timezone | S10, S15 | |
| R11 | UTF-8 throughout, supporting simplified and traditional Chinese, Malay, and diacritics in Hokkien romanization | S06, S12 | |
| R12 | Supported browsers are the latest two major versions of Chrome, Safari and Edge | S14, S16 | |

### 4.5 Operations

| ID | Assumed constraint | Dependent requirements | Decision |
|---|---|---|---|
| O01 | No accepted edit may be lost | S10-R01 | |
| O02 | A live audio session that disconnects partway through ends in `needs_review`; it is not resumed | D01-R06 | |
| O03 | Background jobs use at-least-once delivery with idempotent, fenced commits | D01-R03 | |
| O04 | Migrations are forward-only; applied migrations are not modified | Integration | |
| O05 | Recovery targets after an instance crash: RPO 24 hours, RTO 4 hours | D01-R06 | |
| O06 | A maintenance outage is permitted; this round does not use rolling upgrades | Integration | |
| O07 | Clinic administrators monitor and handle initial incidents; platform operators investigate. There is no 24/7 on-call coverage | S08, S09 | |
| O08 | Report a data breach within 3 calendar days of confirmation and notify affected clinics | S03 | |

### 4.6 Evidence and materials

These statements describe what is and is not available, rather than negotiable preferences. Cross out a statement if the corresponding material can be supplied.

| ID | Assumed constraint | Dependent requirements | Decision |
|---|---|---|---|
| E01 | No real Malay–English–Hokkien medical recordings exist | S06-R04 | |
| E02 | Use ViMedCSS (Vietnamese–English medical content); limitation: not the target language combination | S06-R01 | |
| E03 | Use ASCEND (Mandarin–English); limitation: not medical content | S06-R01 | |
| E04 | Use Common Voice `nan-TW`; limitation: Taiwanese Hokkien, not Singapore Hokkien | S06 | |
| E05 | Decoder tests use synthetic TTS audio; limitations: one speaker, no noise, read speech. Results are an upper bound on robustness, not clinic performance | S06 | |
| E06 | The reviewer writes the reference transcripts and facts for synthetic consultations; this is explicitly disclosed | S06, S12 | |
| E07 | This round has no independent clinical annotator | S12, S13 | |
| E08 | A Twilio sandbox is unavailable | S01-R01, S11-R03 | |
| E09 | A two-process test environment is available locally | All D01 | |
| E10 | Fault injection uses deterministic fixtures already in the code | S02-R02, S08, S09 | |
| E11 | All timing measurements are repeated three times, with all three results reported | S07, S08, D01 | |
| E12 | A reviewer designated by Ira resolves disputes over reference answers | S12, S13 | |

### 4.7 Integration

| ID | Assumed constraint | Dependent requirements | Decision |
|---|---|---|---|
| I01 | Every candidate received `nightingale-base.bundle` (SHA-256 `416488ea…7f52`), exported commit `9fffc01`. Its backend and frontend directories are byte-identical to local `0c437fe` | Integration | |
| I02 | Bundles are full, not incremental | Integration | |
| I03 | No LFS or submodules. Models and datasets are external dependencies, listed separately for each candidate | Integration | |
| I04 | Deliver one Git repository based on the confirmed base | Integration | |
| I05 | Retain the MIT licence. Third-party code introduced by a candidate must have a compatible licence | Integration | |
| I06 | Preserve provenance with `cherry-pick -x` and the change record; retain authorship of adopted code in commit records | Integration | |
| I07 | Ira confirms constraints, the integration target and disputed decisions; Haochen executes and records the work | All | |

---

## 5 · Four change classes and their acceptance evidence

| Class | Definition | Acceptance evidence | Rejection criteria |
|---|---|---|---|
| **Idea** | A design insight without portable code | (a) A test that **fails on the base**, demonstrating the gap; (b) a rationale of no more than one page | The gap cannot be reproduced on the base; the rationale merely repeats a known limitation |
| **Test** | Only adds or modifies tests | (a) Fails without the corresponding fix and passes with it; (b) applies a test input rather than making a static assertion; (c) does not reference the candidate's private implementation | Tautological; passes only on the author's code; asserts the mock's return value |
| **Bounded module** | A replaceable unit with an explicit interface | (a) Written interface definition; (b) tests meeting the Test-class requirements; (c) no new cross-boundary dependency outside the interface; (d) STRIDE enumeration for any trust boundary crossed; (e) porting cost: file, migration and call-site counts | Leaks tenant state; introduces undeclared process-local state; omits porting cost |
| **Architecture** | Changes state location, the tenant model, job model or streaming model | (a) Simplified ATAM assessment: one quality-attribute scenario per deployment axis, with sensitivity and trade-off points; (b) migration paths from **both** the base and current code; (c) executed two-process evidence; (d) comparison with other candidates' implementations at the same point of divergence | No migration path; no two-process evidence; no comparison |

A change spanning classes is assessed under the highest class it involves.

---

## 6 · Rejection criteria

**Hard conditions: any one rejects the change, regardless of benefits.**

- Violates an applicable OWASP ASVS L2 control; changes that write patient-visible records are assessed against L3.
- Makes cross-clinic data reachable, as demonstrated by a test.
- Adds a `SECURITY DEFINER` function without a written justification and scope test.
- Removes or weakens job-lease fencing.
- Claims behaviour without applying a test input.
- Includes a test that also passes on code without its corresponding fix.
- Introduces mutable process-local state on a request path without declaring and explaining its single-replica restriction.
- Modifies an applied migration.
- Sends patient data outside the destinations permitted by P08.

**Soft conditions: record as disadvantages with explicit weights.**

- Porting cost exceeds the benefit at this point of divergence.
- Duplicates a capability already supplied by a selected implementation.
- Adds more separate registration sites instead of consolidating them.

---

## 7 · Comparing implementations

### 7.1 Define what “better” means

Start each change with a testable statement:

> For **[role]**, under **[confirmed environment and fault conditions]**, this change improves **[task outcome]** compared with **[specified version]**, without violating **[relevant CORE requirements]**. Evidence: **[test and run IDs]**. The conclusion applies only to **[tested scope]**.

“Added a cache”, “used a database lock” and “rewrote the interface” describe implementation changes, not user benefits. State the problem solved and show evidence of that result. Removing unnecessary steps, correcting a state, or retaining a reliable implementation are also valid decisions. This standard does not require adopting new code.

### 7.2 Separate two comparisons

**Contribution:** What did the candidate change relative to **its own** base? Fix the candidate ID, base, HEAD, configuration and test version. List additions, deletions, modifications and inherited capability separately. Before attributing a benefit to a fix, confirm the claimed defect on the base using the same specification.

**Selection:** Which available implementation at this point of divergence best meets the same requirement? Compare under common requirements and conditions. A diff against the author's latest version does not replace the contribution inventory. A candidate may contain a useful implementation while failing elsewhere; record the accepted scope rather than ranking the whole repository.

### 7.3 Selection order

1. Confirm conditions under Section 4. Do not make a final decision while they remain unsettled.
2. Check core requirements. Do not recommend an implementation that violates confirmed CORE requirements; speed or added features cannot offset cross-clinic leakage.
3. Assess capability under S01–S16, D01 and D02, giving verdicts and specific gaps rather than pass rates.
4. Verify that the improvement exists under comparable conditions.
5. Check new costs and regressions on both deployment axes; state what was not covered.
6. Give reasons. An implementation may be recommended if it is no worse on tested requirements and clearly better on at least one confirmed priority. Resolve trade-offs using priorities fixed in advance, not weights chosen after seeing results.
7. Record adoption cost separately: distinguish one-time migration cost from ongoing runtime cost. Adoption may be deferred because of cost without changing the quality finding.

At every point of divergence involving candidate #0, first state one advantage of the strongest competing implementation, then give the case for #0. The author has a preference for four designs: agents propose but do not write notes; facts are frozen and the verifier cannot modify them; unidentified languages trigger review; and lease fencing is retained. For decisions involving these four designs, the author provides reasons but does not vote; Ira decides.

Two SURVIVES verdicts do not imply identical value; compare confirmed performance, workload and operational goals further. Do not rank two PARTIAL verdicts by gap count. Consider the affected tasks, roles and consequences of failure.

### 7.4 Establishing that the change caused the improvement

At minimum, record the comparison versions, common requirements and reference answers, environmental differences, actual results, regression results and evidence scope.

Where a change can be isolated, change only that item and its necessary dependencies. Interdependent or architectural changes can be compared as a unit; conclude that the combination is better under the tested conditions, not that one module caused every improvement. For variable results, fix repetitions, data splits and judgement rules in advance; report failures and variation rather than selecting the best run. A designated reviewer supplies clinical reference answers; candidate output is not the reference answer.

---

## 8 · Integration of independently developed bundles

The complete procedure for combining accepted changes is in [`INTEGRATION_PLAN.md`](INTEGRATION_PLAN.md). The main rules follow.

**The base is the author's code before modification.** The export was committed again, so it shares no commit ID with the author's repository even though the application directories are identical in content. Changes in the author's codebase are therefore extracted by content correspondence rather than by taking a commit range between two points.

**Do not run any candidate scripts during intake.** In a separate repository, use `git bundle verify` for format and prerequisite objects, `list-heads` for refs, and `fsck` for object integrity. Record intake failure separately from functional failure.

**Start the integration branch from the confirmed common base, not the author's `main`.** If architecture comparison identifies another candidate as a better starting point, record that decision under I07 and restart there.

**Use five porting procedures according to change structure:** port independent commits selectively and retain provenance; split large commits into reviewable units; for re-exported bases, port patches relative to each candidate's own base; record non-portable architecture changes as “design adopted and rebuilt”, not original code merged; inspect parents and net changes before handling merge commits.

**Run behavioural and integration tests after every port.** A lack of `git` conflicts is insufficient for acceptance: two files can change the same state machine without a text conflict.

**Test four migration cases:** empty-database initialization, upgrade from a populated base, retry after partial failure, and old/new version coexistence where required. Empty-database tests alone do not establish a working upgrade path.

**Trace provenance in both directions:** candidate ID → bundle checksum → original base and commits → change ID → accepted scope and reason → integration commit → conflict resolution → requirement IDs → test run IDs. Distinguish existing candidate failures, adapter errors and failures introduced by integration.

---

## 9 · Literature findings and their effect on this standard

Only references that affect this standard are included.

**9.1 Code review finds fewer defects than reviewers expect.** Bacchelli and Bird (ICSE 2013) found that reviewers **expect** to find defects, but the main **outcomes** are code improvement, knowledge transfer and code understanding. Sadowski et al. (ICSE-SEIP 2018) observed the same at Google. McIntosh et al. (MSR 2014) found an association between low review coverage and post-release defects; this supports review's usefulness, not its sufficiency. *Effect:* Section 1.3 accepts inspected evidence only for requirements outside the deployment axes. Every D01 and D02 requirement calls for executed two-process evidence.

**9.2 N versions require variant management, not ranking.** Zhou, Vasilescu and Kästner (ICSE 2020) found that hard-fork reunification is rare and deliberate. Dubinsky et al. (CSMR 2013) studied clone-and-own development in industrial product lines: longer separation increases reintegration cost. The appropriate approach is to manage variants at each point of divergence rather than choose one winning copy. *Effect:* Section 0 and the integration plan first identify the common core, then compare variants at each point of divergence. Candidates are not scored as whole versions.

**9.3 Architecture requires trade-off analysis.** SEI's ATAM (Kazman, Klein and Clements, CMU/SEI-2000-TR-004) evaluates architecture through quality-attribute scenarios and identifies sensitivity and trade-off points. It records trade-offs rather than assigning scores. *Effect:* The Architecture class in Section 5 requires this simplified assessment.

**9.4 Security requirements for healthcare software.** Use OWASP ASVS L2 for sensitive personal data and L3 where failure could endanger life; enumerate STRIDE threats at every trust boundary (Shostack, 2014). The locally applicable requirements are the PDPA and the Ministry of Health's Healthcare Cybersecurity Essentials. *Effect:* Section 6 makes security a hard rejection condition and treats each new `SECURITY DEFINER` function as a trust boundary.

**9.5 The evaluator is the largest risk.** Norton, Mochon and Ariely (2012) found that people overvalue things they assemble themselves: the IKEA effect. Tomkins, Zhang and Heavlin (PNAS 2017) found that single-blind reviewers favour well-known authors. Nosek et al. (PNAS 2018) present preregistration as a structural response. Under Strathern's formulation of Goodhart's law, a metric selected after seeing the data becomes a target. *Effect:* All eight provisions in Section 8.

**9.6 Leases require fencing.** Gray and Cheriton (SOSP 1989) introduced leases. Kleppmann (DDIA, Chapter 8) explains why leases alone are insufficient: a holder paused beyond expiry may still believe it owns the lease. The resource must therefore reject writes carrying expired tokens. *Effect:* D01-R02 specifies behaviour rather than implementation; Section 6 rejects changes that weaken fencing.

**9.7 Systems misreport their own behaviour.** Shi et al. (Behave-as-Claimed) found that claims and behaviour agree about half the time (mean CFA 0.52). The metric remains stable under training-data leakage, while conventional accuracy rises. *Effect:* Section 1.3 distinguishes applied test inputs from static assertions; only the former count as behavioural evidence.

**9.8 A successful text merge does not establish semantic correctness.** The integration plan requires behavioural and integration tests after each port. Two files can alter the same state machine without a Git conflict.
