# Nightingale — technical brief

**`main` · 3 September 2026 · synthetic data only**

Measured on this tree: backend pytest **521 passed / 1 skipped**; frontend
vitest **134 passed**; ruff, ruff-format, mypy and ty clean. **11 of 16 clinic scenarios survive, 5 are
partial, none fail outright.** Seven are demonstrated on video, each gated by
an automated check that asserts the on-screen evidence before any footage is
kept. Per-scenario coverage is indexed in
[`SCENARIO_TEST_MAP.md`](./SCENARIO_TEST_MAP.md), which a test verifies against
the source tree. Architecture detail is in
[`ARCHITECTURE.md`](./ARCHITECTURE.md).

Where a claim rests on something I could not measure, it is listed as not
evidenced rather than described as working.

## 1 · What the feedback changed

The feedback's central move was to stop asking whether a feature exists and
start asking what a clinician sees when it misbehaves. Re-reading the build
through that lens produced four patterns, none of which I expected.

**The safety machinery was consistently ahead of the surfaces consuming it.**
The evaluation harness had really run and its negative result really did gate
the system; anchors were frozen by trigger. What was missing, repeatedly, was
the last hop: a reason-label map covering two of six backend values, a
`fallback_kind` badge that was unreachable dead code, a
`current_confidence_state` computed on the server and then re-derived — wrongly
— in the browser, a diff endpoint with no caller. A reviewer reading only the
backend would have scored this build higher than a clinician using it.

**Absence was being read as innocence.** Four trust defects reduced to one
mistake: a missing `DecisionAssessment` row was treated as "nothing to qualify"
rather than "not qualified". That reading let a job report qualified confidence
from a partial subset, let an unassessed model-derived highlight render as a
ready priority and reach the patient projection, and let publication record a
model claim as human-asserted. The fix is one explicit rule — a highlight is
model-derived if it carries a candidate fingerprint, and a model-derived
highlight with no assessment is unqualified — threaded through job confidence,
glance review state and the publication gate by two shared helpers instead of
three divergent checks.

**The same mistake reappeared in an unrelated subsystem.** The row-level
security write check on provenance pointers validated a row by querying for
that row, which cannot be satisfied before the row exists. A not-yet-present
thing treated as a fact about the world rather than as a gap in what is known.

**State machines right in the model and wrong at the boundary.** A delivered
SMS could be marked revoked, which is a false record — except for enrolment and
one-time codes, where revocation is precisely how the live token is
invalidated. Clinic preflight returned a 500 on exactly the out-of-range
retention configuration it exists to report.

## 2 · Scored build parts

**Survives — seven of twelve.** *Medical terminology and dosage confirmation:*
a versioned RxNorm-coded clinic formulary screened at publication, with
four-axis human attestation enforced on client and server; the vocabulary is
deliberately small and fails closed outside it. *Immutable, version-bound
provenance:* triggers freeze anchors and seal version ciphertext, and
append-only tables `REVOKE UPDATE, DELETE` from the runtime role. *Fact
extraction under negation, correction and conflict:* three-valued polarity,
four negation-scope guards, and unknown never becomes reassuring. *Real-time
collaborative editing:* lost-update prevention proven under thread contention,
with comment mutations carrying revisions and requiring `If-Match`. *AI
regeneration preserving human state:* candidate fingerprints make regeneration
reuse rather than duplicate, and confirmed state is protected by trigger in
scoring, decay and cold storage. *Contradictory human, patient and AI
assertions:* no source-precedence auto-resolution, and the conflict card names
each side's origin, role, section and language. *Distinct clinician, staff and
patient outputs:* server-side read and write gating, different response schemas
per audience, a separate patient route tree.

**Partial — five of twelve.** *Streaming and noisy-environment ASR* uses a real
SNR estimate rather than the earlier RMS proxy, but there is no denoiser and no
measurement on real noisy recordings. *Diarization* runs pyannote in a killable
subprocess with typed unavailability codes; no weights are cached here, so
quality is unmeasured. *Within-statement code-switching* determines language per
span at a 0.85 threshold and fails closed below it, but only against synthetic
spans. *Multilingual downstream processing* gained Malay and Chinese dose
patterns plus a language-agnostic numeric fallback, closing an English-only
publication-gate bypass; there is still no translation step and no real-speech
evaluation. *Self-learning* is clinic-scoped and bounded — ±0.20 clamp by
database check, 1/√n damping, append-only audit enforced by REVOKE, shadow mode
by default, allergy and medication floors in both code and schema — but
exposure is evaluated and **not corrected**: no exploration, no propensity
weighting.

**One deliberate omission inside a survivor.** Nothing here generates for
reading level. The patient summary is written by the publishing clinician.
Automatic rewriting would put unreviewed model wording into the patient-visible
record, contradicting the rule that human confirmation gates everything
outbound. The audience separation is real; audience-adapted generation is not
claimed.

## 3 · Where this fails first

**Message delivery.** No Twilio credentials exist here, so the effective
provider is a deterministic stub that reports success. Scenarios 1, 5 and 11
all terminate at the same unproven hop. One sandbox account moves three
scenarios at once; without it, a queued message looks delivered, which is the
failure mode that matters.

**The voice path under a hang.** Text jobs are bounded at 15/30/75 seconds and
fall back deterministically. Voice has only a 600-second per-call timeout, so a
stuck transcript shows an indefinite spinner with no elapsed time and no retry.
This is the least-improved scenario and the one most likely to be hit in a real
clinic.

**Real multilingual speech.** Every code-switching result here is a
deterministic fixture. Hokkien is the weakest leg: the `nan` lexicon assumes
POJ romanisation and Whisper does not emit POJ, so that branch is effectively
unreachable from a real recording. There is no public Malay/English/Hokkien
medical consult corpus to evaluate against.

**Development-mode escape hatches.** The restricted-runtime-role assertion is
skipped when `FASTAPI_ENV=development`. A staging box left in development mode
with owner credentials loses the tenant-isolation guarantee that the rest of
the design depends on.

## 4 · What I tried that did not work

**Fixing tests before understanding them.** The full backend suite gave 140
failures. Columns had been added to already-applied migrations, so the database
was stamped at head while lacking them. Rebuilding the schema from empty
reduced 140 failures to 8 — 132 environmental, 8 real signal. The rule that
came out of it: no further edits to an applied migration.

**Adding `entity:medication` to the protected-visibility predicate.** It
belongs in the learning safety floor, and it is now there with a matching
database check constraint. Putting it in the visibility predicate as well would
have reclassified every medication highlight into the clinical-review surface
and silently changed dismissal permissions. Learning suppression and review
classification are different concerns that happened to share a predicate.

**A demo proof string that proved nothing.** My first scenario-13 assertion was
`"Penicillin Allergy"`, which passed while the conflict card did not exist at
all: Playwright's substring match is case-insensitive and had matched the note
body I had just typed. Proof strings are now unique to the card under test.

**Dropped from this freeze:** `VOICE_JOB_TIMEOUT_SECONDS` with elapsed-time and
retry UI; the `LocalNormalizedText` nominal type; the original-versus-current
diff pane; a persisted `MedicationReviewCandidate` table; exposure-bias
correction. Each is named rather than quietly omitted.

## 5 · Assumptions

**Still standing.** Provenance must be version-bound and immutable; this held
under every probe. The database, not the route layer, is where tenant isolation
belongs — deleting a route's clinic filter still yields zero cross-boundary
rows. Redaction ordering must be structural, so tampered text fails by
construction rather than by convention. Shadow-mode-by-default for learning,
which the exposure-bias critique confirmed rather than undermined. Human
confirmation gates patient-facing dosage.

**No longer standing.**

*That a green suite means a user-reachable path works.* The suite was green
while the patient portal returned a 500 on its primary action, because no test
exercised a patient writing clinically meaningful text. The bug was found by
trying to film the feature. Coverage measured the paths I thought to write, and
my blind spot in the tests matched my blind spot in the code.

*That a stubbed provider is an acceptable default.* It reports success, so a
queued message looks delivered. The state machine was never the problem; the
honest default was.

*That a declared config value is an enforced one.* Retention days, messaging
channels and a worker-enabled flag were stored, plumbed and unread. A setting
that exists only to satisfy a test reads as a control during review and is
worse than no setting.

*That backend correctness implies clinical correctness.* Correct data reaching
the wire and never rendered is not a feature. The dead badge and the unreachable
diff pane are the same failure as a missing check.

*That "no assessment" is neutral.* The assumption behind all four trust defects
and behind the provenance write check.

*That the demo environment reflects the code.* It did not, silently, for eight
hours, because an old image and an old schema agreed with each other.

## 6 · The demo is a test that produces footage

Each scenario is defined once with its click path and the strings that must be
visible for it to be true. A check phase walks that path with capture off and
asserts every string; the record phase replays it with capture on. A scenario
that cannot prove itself produces no video, so footage cannot drift from the
claim.

Filming scenario 13 found three defects, none reachable from the suite as
written. Conflict cards showed UUID-sort ordinals — "first" versus "conflicting"
assertion — instead of nurse versus patient, because origin was checked before
section. A patient insight mentioning an allergy writes a provenance pointer,
and that table's write check validated a pointer by querying for that pointer:
a self-referential `EXISTS`, unsatisfiable before the row is visible, so **any
patient submitting clinically meaningful text through their own portal got a
500** — invisible to clinical roles, whose branch short-circuits first. With
that fixed, the patient's "no known drug allergies" was refused as an
unresolved conflict, because the gate stopping clinician content going out to a
patient was also stopping the patient's own input. All three now have
regression tests.

**Evaluation evidence, negative results kept.** Fact extraction calibrates to
`low` at a 0.0437 accuracy lower bound over 176 judged facts; voice to `low`
with a 0.200 word error rate over 2,206 segment decisions; redaction achieves
1.0 PHI recall over 500 samples and passes. The first two are why the system
abstains on every AI fact extraction today. Left exactly as measured.
