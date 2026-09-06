# Nightingale Self-Audit

**Applying the project's review standard to the author's own code before opening any other candidate version**

Codebase `main` · 26 commits, 317 files, +62,573 / −5,005 lines relative to base `0c437fe` · September 2026 · candidate #0 under Section 0 of [`REVIEW_STANDARD.md`](REVIEW_STANDARD.md)

---

## 0 · Method

The technical brief makes many claims. Behave-as-Claimed (Shi et al.) asks a system what it would do if an input changed, makes that change, then compares the stated and observed behaviour. Across the models evaluated in that paper, claims and behaviour agreed about half the time. There is no reason to assume this codebase would do better.

Each row below records a claim from the brief or scenario table, a counterfactual that could falsify it, the verification available, and a conclusion under the standard's three evidence levels in Section 1.3 (**executed** / **inspected** / **not produced**) and verdict rules in Section 1.2.

The two deployment axes were audited by inspecting code, not by running two processes, because the standard's deadline came first. Every conclusion marked “inspected” on these axes must be tested during assessment, including those I expect to pass.

---

## 1 · Claim ledger

| ID | Requirement | Claim | Counterfactual | Available verification | Level | Conclusion |
|---|---|---|---|---|---|---|
| C1 | D01-R02 | Job leases remain valid across replicas | Two workers compete for one job; the loser's submission must be rejected | `claim_job` uses `SKIP LOCKED` and sets `locked_by` to the claim token (`ai_jobs.py:1261`); writes require six checks before submission: state, token, expiry, attempt status, worker membership and worker liveness (`:1345`); expired leases are reclaimed through `WORKER_LEASE_EXPIRED` (`ai_worker.py:152`) | inspected | Holds, subject to constraint R09 (clock synchronization between replicas). Single-process expiry has been tested; two-process contention has not |
| C2 | D01-R04 | Event streaming works across replicas | A client connected to replica A must receive an event written through replica B | `events.py` polls Postgres every second using a `sequence_no` cursor (`:20`), supports `Last-Event-ID`, and sends a heartbeat every 15 seconds; `trust.py:2691` uses the same pattern. There is no in-process broadcast | inspected | Holds. Each open stream costs one query per second; the maximum supported scale has not been measured |
| C3 | D01-R05 · TH05 | The global live-transcript connection cap is deployment-wide | Open 8 connections on each of two replicas; the 9th must be rejected | Clinic and user caps use Postgres advisory locks (`live_limits.py:145`) and are deployment-wide. The global cap uses `asyncio.BoundedSemaphore` (`:109`) and is process-local. The actual cap is 8 × replica count | inspected | **Fails.** Deployment documentation never declared a single-replica restriction, so this is an unstated assumption. It is a core failure under D01-R05 |
| C4 | D01-R06 · O02 | A live session is not lost if it disconnects partway through | Kill the replica during dictation | Both `WebSocketDisconnect` and `CancelledError` set `LIVE_TRANSCRIPT_DISCONNECTED` and persist the session as `needs_review` (`voice_live.py:809`). Reconnection produces the terminal state `replaced` (`test_live_transcript.py:621`) | executed | Holds. Data persists; resumption is deliberately unsupported. Behaviour is the same with one or multiple replicas |
| C5 | D01-R05 | In-memory `ConsultState` makes the agent pipeline fragile across replicas | Run the pipeline on two replicas | `ConsultState` is created for each `run_consult_pipeline` call and discarded afterwards. Here, “in-memory” means not persisted, not shared across requests | inspected | The design holds, contrary to the claim. The actual weakness is C3 |
| C6 | D02-R01 | Row-level security is forced on every tenant table | Add a tenant table without a policy; the test must fail | `test_rls_migration.py:335` queries `pg_class.relrowsecurity` and `relforcerowsecurity` directly and asserts that no table is missing; `:279` asserts `TENANT_TABLES <= policies`; `:152` and `:161` reject clinic-only policies on patient rows and a split-`USING` bypass | executed | Holds |
| C7 | D02-R01 | RLS predicates are correct, not merely present | A patient writes a record in their own channel | This failed until 2 September: the `WITH CHECK` for writing provenance pointers queried a row that did not yet exist (fixed in `e9a7c3b18d24`). All policy-presence tests passed throughout that period | executed | **Previously failed; now fixed.** Presence tests do not detect predicate errors. This is why Section 3.3 of the standard requires tests for each write path |
| C8 | D02-R01 | The scope exposed by `SECURITY DEFINER` functions is bounded | Can any function return data outside the caller's clinic? | 30 functions in three groups: `app_*_context_allows` (RLS predicate helpers), `app_lookup_*` (pre-authentication bootstrap, cross-tenant by design), and `nightingale_*_guard` (immutability triggers). No test enumerates them and asserts their scope | not produced | **Unverified.** This is a gap, not a finding that the functions are safe |
| C9 | D02-R01 | Tenant isolation holds in every environment | Use `FASTAPI_ENV=development` with owner credentials | Development mode skips the restricted-runtime-role assertion | executed | **Fails** (known). A test host left in development mode loses this guarantee |
| C10 | S08 | Waiting is bounded when the model hangs for 45 seconds | The remote service returns only after 45 seconds | Text: stopped at the 15-second stage cap, then uses deterministic fallback (`test_text_job_deadline.py`). Voice: only a 600-second per-call timeout, no overall deadline, an indefinite spinner, and no retry. `VOICE_JOB_TIMEOUT_SECONDS` and the elapsed-time indicator were removed in this freeze | executed | Text holds. **Voice fails**, violating the CORE requirement that the interface display status |
| C11 | S01 | Patients without email can enrol and log in | Remove Twilio | Enrolment works through a claim code delivered in person. Login depends on OTP delivery; the codebase has no credentials and no OTP has ever been sent | executed / not produced | Enrolment holds. **Login is unverified** |
| C12 | S03 | PHI does not enter logs | Put PHI in the path, query string, headers and body | `test_phi_safe_observability.py` exercises all four and asserts that output records contain no PHI. Downstream proxy and APM retention periods are written statements | executed / not produced | Holds within the process. **External retention is unverified** |
| C13 | S11 | Delivery failure is visible to staff | The provider drops a message | The state machine works: resubmission, acknowledgement receipts and revocation rules (`test_messaging_contracts.py`). For actual delivery, the deterministic stub reports success, so queued messages appear delivered | executed | The state machine holds. **Actual delivery fails**, meeting the core-failure condition “queued shown as delivered”. This follows from the stated default behaviour, not a defect in the mechanism |
| C14 | S06 | Extraction works on trilingual code-switched sentences | Real Malay–English–Hokkien clinic recordings | No such corpus exists. On ViMedCSS: 33/33 expert-annotated code-switching terms retained, 0 fabricated NKDA, and 0 citation-offset errors. Failures marked `xfail(strict)`: Chinese non-penicillin drugs, traditional characters, Han-script drug names in Malay sentences, English `NKDA`, and Malay `tidak ada`. Failures even on **correct** transcripts: `alah kepada` and two allergens in one sentence | executed (sub-requirements) / not produced (overall) | **Unverified** overall; sub-requirements are separated as listed |
| C15 | S06 | Seven gold-standard consultations score 1.00 | Remove the author's knowledge of the lexicon | This counterfactual cannot be applied: the same people wrote the lexicon and gold-standard cases | not produced | Circular evidence. Invalid as evidence, as the documentation states |
| C16 | S06 · S12 | Fuzzy drug-name recovery is safe | Use a formulary with thousands of entries rather than four | On a four-entry table, it recovers `penicilin` and rejects `asprin`. The false-positive rate among densely similar names has not been measured | not produced | **Unverified** |

**Counts:** 4 executed and holding · 4 inspected and holding · 5 failing (including 1 fixed) · 5 unverified; 2 rows also contain split outcomes. All rows supported only by inspected evidence concern the two deployment axes.


---

## 2 · Audit coverage

Sections 2 and 3 of the standard contain **109 requirements**: 94 across 16 scenarios and 15 across the two deployment axes. The ledger in Section 1 addresses specific claims in the technical brief. This section describes coverage of those 109 requirements.

Each requirement was checked against 521 backend tests and 134 frontend tests. Classification: “executed” where a test asserts the behaviour; “inspected” where the mechanism exists in code but has no assertion; “not produced” where neither exists.

| Coverage | Description |
|---|---|
| executed | Most requirements. Across the 16 scenarios, the existing suite has 2 to 12 tests per scenario, with the highest density in S06, S15 and S14 |
| inspected | D01-R01 through D01-R04 on the deployment axes, and S07-R03. No two-process run was performed |
| not produced | The six requirements below |

**These six gaps were identified when the requirements were expanded:**

| Requirement | Type | Finding |
|---|---|---|
| S01-R01 Three core patient flows | **CORE** | Enrolment and shared-number handling are tested; of the three flows — viewing shared notes, submitting one's own account, confirming medication instructions — only part has been walked end to end |
| S01-R05 Claim codes expire and resist enumeration | COMPLETE | `claim_code_expires_at` exists, but tests use it only to construct data; none asserts that expired codes are rejected |
| S02-R05 Regression tests or monitoring detect the fault | COMPLETE | No corresponding test |
| S04-R03 Fail closed when the redaction component is unavailable | **CORE** | The `require_presidio` flag exists, but no test covers component unavailability |
| S05-R05 Retry after interrupted initialization preserves boundaries | COMPLETE | No corresponding test |
| S07-R01 Allergy card appears within 10 seconds | **CORE** | The mechanism has been tested; latency has never been measured |
| S09-R06 Explicit unavailability for patients with no history | COMPLETE | No corresponding test |
| S11-R05 Three flows after delivery failure | COMPLETE | Medication correction is covered by `test_medication_gate_and_correction_survive_delivery_failure`; note publication and conflict handling are untested |

S01-R01, S04-R03 and S07-R01 are CORE. Missing evidence for them prevents S01, S04 and S07 from receiving SURVIVES.

**Rechecked after the standard was revised.** Eight requirements in Section 2 of the standard were rewritten in decidable form, and five of those conclusions can now be settled:

| Requirement | Conclusion after rewriting | Basis |
|---|---|---|
| S06-R01 Every span carries language, source and confidence | executed, holds | `AddressableLanguageSpan` has all three fields; `test_clinical_voice_quality.py` asserts `detection_source` |
| S15-R01 A single update stays within the A12 bound and is rejected above it | executed, holds | Database constraint `weight >= -0.20 AND weight <= 0.20`; `test_importance_feature_weight_bound_is_database_enforced` asserts `IntegrityError` at ±0.200001 |
| S15-R05 New weights do not affect live ranking until approved | executed, holds | `importance_mode` defaults to `shadow`, with values constrained in the database |
| S14-R04 Explanation covers source, how it could be wrong, and consequences | executed, holds | `GlanceTopCard` has all three sections; a frontend test opens the panel |
| S01-R01, S11-R05, each naming three flows | **partly not produced** | See the table above |

Making the requirements specific exposed gaps rather than hiding them. The earlier wording — “complete the core patient workflow”, “related clinical actions remain possible” — was too broad to judge; naming three flows each made it visible that only one had been tested.

**One requirement was weakened and has been restored.** S15-R06 originally read “exposure bias is measured and corrected”. This codebase measures but does not correct, which is a clear gap. While making requirements decidable, I rewrote it as “either a recorded corrective action or a recorded reason for not correcting” — and the technical brief happens to record a reason for not correcting, so that rewrite turned a failure into a pass for this codebase. That is relaxing a requirement after seeing the result, which Section 0 of the standard prohibits. It now reads “measured bias is down-weighted or excluded in the next ranking pass; recording without acting does not satisfy this requirement”, and S15-R06 remains a confirmed gap.

**A methodological error was corrected.** The first draft of this section recorded “I did not check” as “not produced” and concluded that no scenario passed. Checking the tests showed that six requirements lacked evidence and the rest had coverage. Section 1.4 of the standard requires missing evidence to be distinguished from observed failure; the first draft violated that rule. Section 4 gives the corrected verdicts.

## 3 · The two deployment axes in this codebase

**D01 Multi-replica.** The supported behaviour has a consistent basis: state is stored in Postgres and all consumers read it there. Leases use claim-token fencing; event streams poll by cursor; the provider circuit breaker is a tenant table (`models.py:1709`); disconnected live sessions move to review rather than continuing silently. Two areas do not hold: the global live-connection cap is a process-local semaphore (C3), a core failure under D01-R05; and event-stream cost grows linearly with connections, with no measurement of the scale at which it becomes unacceptable (D01-R07, not produced). One correct choice still needs an explicit declaration: `_AudioRateLimiter` (`voice_live.py:70`) is per-connection and process-local, which is the correct scope for a per-connection limiter.

**D02 Cross-clinic isolation.** Database tests enforce coverage (C6), and policy **structure** is also tested. Three findings weaken the guarantee: 30 `SECURITY DEFINER` functions have no scope tests (C8, not produced); one predicate was present but wrong for several weeks (C7, fixed); and development mode has a configuration gap (C9). D02-R03 (no cross-clinic cache reuse) and D02-R05 (download links remain subject to permissions when fetched) were not reviewed in this pass; their status is **not produced**.

---

## 4 · Scenario verdicts under this standard

| ID | Scenario | Verdict | Basis |
|---|---|---|---|
| S01 | Patient without email | PARTIAL | R02, R03 and R06 executed and holding; **R01's three core flows are only partly tested**; evidence for R04 login and R05 claim-code expiry not produced |
| S02 | Route handler | PARTIAL | R01–R04 and R06 executed and holding, including database enforcement and cross-clinic checks through multiple entry points; evidence for R05 fault detectability not produced |
| S03 | Log hygiene | PARTIAL | R01–R04 executed and holding; evidence for R05 external outputs not produced; R06 tested only with a short retention period |
| S04 | Redaction | PARTIAL | R01 and R02 executed on 500 synthetic samples; **R03 is CORE and the fail-closed path has no test** |
| S05 | Clinic B onboarding | PARTIAL | R01–R04 executed and holding; R04 addresses a defect fixed in this project; evidence for R05 interrupted initialization and retry not produced |
| S06 | Trilingual code-switching | PARTIAL | R01–R04 and R06 executed and holding, R01 verified against the rewritten three-field wording; **R05 confirmed failures for Chinese non-penicillin drugs and traditional characters**; R07 has no corpus |
| S07 | Allergy at minute 2 | PARTIAL | R02 and R04 executed and holding; **R01 is CORE and the 10-second latency has never been measured**; R03 inspected only |
| S08 | 45-second hang | **DOES NOT** | **R02, “the interface displays status and elapsed time”, is CORE; the voice path is confirmed not to implement it** |
| S09 | Provider 503 | PARTIAL | R01–R05 executed and holding, including a one-hour outage test and circuit-breaker recovery; evidence for R06 patients without history not produced |
| S10 | Two clinicians editing simultaneously | **SURVIVES** | All six requirements executed and holding: `If-Match` version binding, three-way conflict view, retained input for late requests, and derived-content references |
| S11 | Link not received | PARTIAL | R02–R04 executed and holding, including callback signature verification; R05 covers medication correction only; **R01 actual delivery meets the core-failure condition under the stated default behaviour**; R06 has no sandbox |
| S12 | Wrong dosage | **SURVIVES** | All six requirements executed and holding: formulary dose ranges, unresolved conflicts excluded from the patient projection, and propagation of corrected versions |
| S13 | Allergy vs NKDA | **SURVIVES** | All six requirements executed and holding; R04, allowing patients to state information in their own channel, addresses a defect fixed in this project; R05 adjudication reasons are encrypted at rest |
| S14 | Meaningful number | **SURVIVES** | All six requirements executed and holding; `GlanceTopCard` carries both R04's three sections and the sample count |
| S15 | Learning loop | PARTIAL | R01–R05 executed and holding; R01's ±0.20 bound is database-enforced with an out-of-bound test and R05's shadow mode is the default; **R06 exposure bias is measured but not corrected** |
| S16 | Citation to an edited note | PARTIAL | R01, R02 and R04–R06 executed and holding; **R03 comparison view is confirmed absent** |
| D01 | Multi-replica | **DOES NOT** | **R05 deployment-wide cap changes with replica count, a core failure**; R01–R04 inspected only, with no two-process execution |
| D02 | Cross-clinic isolation | PARTIAL | R01, R02, R04, R06 and R08 executed and holding; R03 cache reuse and R05 download links were not reviewed in this pass; 30 `SECURITY DEFINER` functions lack scope tests |

The technical brief reports 11 SURVIVES and 5 PARTIAL. Reassessment against this standard's 109 requirements gives **4 SURVIVES, 12 PARTIAL and 2 DOES NOT**.

The drop from 11 to 4 reflects stricter requirements, not worse code. The brief assessed whether a mechanism was implemented and tested. This standard asks whether every requirement in a scenario has evidence; each scenario was expanded from 3 requirements to 5–7. Of the seven downgraded scenarios, six lack evidence for added requirements and one (S06) has a confirmed failure.

The four SURVIVES scenarios share one feature: all their requirements concern the database and backend and can be fully verified with the existing suite. Most downgraded scenarios depend on external evidence (delivery, downstream log retention, real recordings) or interface features (comparison view, elapsed-time display).

Both DOES NOT verdicts have explicit grounds. The S08 voice path lacks an elapsed-time display, an explicit CORE requirement. D01 implements the deployment-wide cap separately per replica, an explicit core failure. Both have proposed fixes of about twenty lines; see Section 6.

---

## 5 · Specific gaps in PARTIAL scenarios

| Scenario | What holds | What does not hold | What is needed |
|---|---|---|---|
| S01 | Phone-only credentials; claim code delivered in person; shared household numbers | No OTP has ever been sent | One Twilio sandbox account, one sent OTP, and one completed login |
| S03 | PHI removed from all four paths; audit free text encrypted; 30-day in-process retention | Downstream retention is only a written statement | An expiry check against downstream storage, or an explicit scope exclusion |
| S06 | Per-segment language detection, 0.85 threshold, fallback to review; invariants hold on public data | No corpus for this language combination; Hokkien unavailable through Whisper; seven regex defects, two triggered even by correct transcripts | See the Q6 brief; the two defects on correct transcripts require small changes |
| S08 | Text path bounded at 15 / 30 / 75 seconds | Voice: 600-second per-call cap, no overall deadline, no elapsed-time display, no retry | The three removed features were designed but are absent from the code |
| S11 | Receipt state machine; resubmission; exceptions to revocation rules | Stub reports success | The same sandbox account as S01 |

---

## 6 · What I would rebuild

Ordered by impact per line of code:

1. **Use advisory-lock counting for the global live-connection cap**, as the clinic and user caps already do in the same file. About twenty lines. This changes C3 from failing to holding and D01 from DOES NOT to PARTIAL.
2. **Write one parameterized test** that enumerates every `SECURITY DEFINER` function and asserts that cross-clinic queries return no rows. This addresses C8.
3. **Add a cross-clinic test for each write path using `WITH CHECK`.** C7 passed all presence tests while its predicate was wrong.
4. **Restore the three removed voice features:** overall deadline, elapsed-time indicator, and retry. These correspond to S08-R01, R02 and R03, so completing them moves S08 from DOES NOT to PARTIAL; R05, “retries produce no duplicate external side effects”, needs its own evidence before SURVIVES.
5. **Disable development mode outside localhost.** This addresses C9.
6. **Consolidate the drug registry.** Its eleven registration sites cause three of the five `xfail` cases.

Items 1, 2 and 5 each take less than an hour. If completed before assessment starts, the ledger must be rerun and regraded. Section 0 of the standard requires the revision to be recorded and previously assessed candidates to be rerun.

---

## 7 · What this audit did not verify

The two deployment axes: inspected, not executed. Actual delivery, external log retention and real trilingual audio: evidence cannot be produced under current conditions, including on the base. D02-R03 and D02-R05: not reviewed in this pass. TH01 allergy latency: not measured.

Under Section 1.4 of the standard, each is currently `UNVERIFIED`. If evidence is not produced during assessment, it becomes **DOES NOT — required evidence missing** at the end. The recorded reason is missing evidence, not an observed failure of the implementation; the distinction is explicit.
