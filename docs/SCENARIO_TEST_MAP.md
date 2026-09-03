# Scenario-to-test map

An index from the sixteen clinic scenarios to the automated tests that cover
them. Every test name below was collected from this checkout; the map is
verified by `backend/tests/test_scenario_map.py`, which fails if a referenced
test stops existing.

Verdicts repeat the self-assessment. **Survives** means the mechanism is
implemented and asserted by a test. **Partial** means part of the scenario is
covered and the uncovered part is named. No scenario is claimed on
documentation alone.

## How to run

```bash
cd backend && uv run pytest                      # 517 passed, 1 skipped
cd trilingual-consult && uv run pytest           # code-switching and ASR bench
cd frontend && bun run test                      # unit suite
cd frontend && bunx playwright test              # browser suite (needs the local stack)
BASE_URL=https://localhost node scripts/demo/record_scenarios.mjs --check
```

The last command is the video gate: it walks each recorded scenario's click
path with capture off and asserts that every declared proof string is on
screen. A scenario that cannot prove itself produces no footage.

To run one scenario's backend tests:

```bash
cd backend && uv run pytest tests/test_clinical_blockers.py -k nurse_allergy
```

## Summary

| # | Scenario | Verdict | Tests | Video |
|---|---|---|---|---|
| 1 | Patient with no email | Partial | 4 | — |
| 2 | One line changes in a route handler | Survives | 5 | ✓ |
| 3 | Log hygiene | Partial | 5 | — |
| 4 | Redaction before the model | Survives (text) | 5 | — |
| 5 | Clinic B onboards Monday | Survives | 2 | ✓ |
| 6 | Trilingual code-switched sentence | Partial | 12 | — |
| 7 | Allergy at minute 2 | Survives (mechanism) | 3 | — |
| 8 | 45-second model hang | Partial | 4 | — |
| 9 | Provider 503 for an hour | Survives | 2 | ✓ |
| 10 | Two clinicians type at 09:14 | Survives | 8 | ✓ |
| 11 | Link generated, never received | Partial | 6 | — |
| 12 | Wrong dosage in a patient summary | Survives | 7 | ✓ |
| 13 | Nurse allergy vs patient "no allergies" | Survives | 6 | ✓ |
| 14 | A number that means something | Survives | 9 | ✓ |
| 15 | Learning loop bias, tired dismissal | Survives (floor) / Partial (exposure) | 10 | — |
| 16 | Highlight cites an edited note | Survives (backend) / Partial (UI) | 7 | — |

Paths below are relative to the repository root. Backend tests live under
`backend/tests/`, code-switching tests under `trilingual-consult/tests/`,
browser tests under `frontend/tests/`.

---

## 1 · Patient with no email — Partial

`Patient` carries no email column; portal access is a phone-only credential
with a claim code, and enrol, OTP, verify and login all key on a portal id.

- `test_access_onboarding_delivery.py::test_shared_phone_enrollment_resend_revocation_and_recovery` — asserts the created account has `email is None` and covers a shared household phone
- `test_rls_migration.py::test_patient_access_credentials_allow_shared_phone_hmac`
- `test_messaging_contracts.py::test_twilio_sms_and_whatsapp_provider_contracts`
- `test_access_onboarding_delivery.py::test_twilio_status_callback_uses_exact_public_url_and_official_signature`

**Not covered.** No Twilio credentials exist in this tree, so no OTP has ever
left the building. Enrolment survives because the claim code is handed over in
band; login is the hard dependency. Treat delivery as unevidenced.

## 2 · One line changes in a route handler — Survives

The question is how many patients become visible when a route's clinic filter
is deleted. The answer is zero, enforced below the route layer.

- `test_rls_migration.py::test_actor_and_patient_rls_fail_closed_without_route_filters` — issues bare unscoped selects and asserts zero cross-boundary rows
- `test_rls_migration.py::test_every_tenant_table_has_forced_rls_required_policies_and_grants` — discovers tenant tables from the live schema, so a new one cannot silently escape
- `test_rls_migration.py::test_platform_oversight_tables_are_isolated_from_clinic_sessions`
- `test_rls_migration.py::test_runtime_guard_rejects_owner_connection`
- `frontend/tests/scenarios.spec.ts` — `[Scenario D] stale ETag conflicts while independent entries and tenant boundaries hold`

**Video.** `Nightingale_Scenario_02-clinic-isolation.mp4`

**Residual risk.** `clinics` is a global directory table without RLS. It holds
no PHI but is enumerable. The runtime-role assertion is skipped when
`FASTAPI_ENV=development`.

## 3 · Log hygiene — Partial

- `test_phi_safe_observability.py::test_access_log_drops_url_query_body_headers_method_and_exception_text` — fires PHI in path, query, headers and body, then asserts absence from emitted records
- `test_phi_safe_observability.py::test_request_validation_error_drops_rejected_values_and_free_form_messages`
- `test_phi_safe_observability.py::test_patient_search_rejects_get_query_and_logs_neither_query_nor_post_body`
- `test_phi_safe_observability.py::test_audit_free_text_is_encrypted_and_machine_metadata_is_allowlisted`
- `test_phi_safe_observability.py::test_operational_event_repository_enforces_30_day_retention`

**Not covered.** External proxy and APM retention is an attestation, not a
measurement. Onboarding preflight refuses to proceed without a qualified
retention evidence id, but nothing here verifies the downstream sink actually
expires data.

## 4 · Redaction before the model — Survives for text

Ordering is structural: the remote provider is reachable only through a
gateway that reconstructs and re-verifies the digest before calling out.

- `test_redaction_pipeline.py::test_remote_text_provider_contract_and_call_sites_are_gateway_only` — AST guard over every module under `backend/app`
- `test_redaction_pipeline.py::test_gateway_ast_guard_rejects_aliases_and_dynamic_dispatch`
- `test_redaction_pipeline.py::test_typed_egress_gateway_rejects_unqualified_or_tampered_reports`
- `test_redaction_pipeline.py::test_residual_detection_blocks_remote_provider`
- `test_redaction_pipeline.py::test_presidio_exception_fails_closed_and_logs_no_phi`

**Not covered.** Raw audio cannot be redacted before the model; remote audio
requires a deployment flag, a clinic policy row and per-session consent, all
fail-closed. The `LocalNormalizedText` nominal type was planned and not landed,
so the local fallback still accepts `str` — the invariant holds by wiring and
by the AST guard, not by the type system.

## 5 · Clinic B onboards Monday — Survives

- `test_access_onboarding_delivery.py::test_second_clinic_preflight_onboarding_is_data_only_audited_and_idempotent` — preflight blocking, creation, worker identity, invitation, staff acceptance, idempotent replay, 409 on changed intent
- `test_formulary_admin.py::test_platform_preflight_reports_versioned_formulary_template_readiness`

**Video.** `Nightingale_Scenario_05-clinic-b-onboarding.mp4`

**Not covered.** The new administrator's invitation rides the same untested
transport as scenario 1.

## 6 · Trilingual code-switched sentence — Partial

Language is determined per span with a recorded detection source and a 0.85
confidence threshold. Below it the span stays addressable but downstream
extraction fails closed to review.

Backend:
- `test_clinical_voice_quality.py::test_within_segment_code_switch_preserves_addressable_language_spans`
- `test_clinical_voice_quality.py::test_no_punctuation_code_switch_keeps_match_level_languages_and_facts`
- `test_clinical_voice_quality.py::test_mixed_language_span_falls_back_to_und_but_keeps_qualified_fact_languages`
- `test_clinical_voice_quality.py::test_low_confidence_provider_hint_is_addressable_but_requires_review`
- `test_clinical_voice_quality.py::test_multilingual_medication_regimen_is_complete_and_source_addressable`
- `test_clinical_hardening_unit.py::test_trilingual_allergy_normalization_preserves_scope_and_source`
- `test_clinical_hardening_unit.py::test_unsupported_language_and_not_documented_never_become_no_allergy`
- `test_clinical_hardening_acceptance.py::test_one_code_switched_live_statement_reaches_conflict_glance_and_gate`

Code-switching package:
- `trilingual-consult/tests/test_pipeline.py::test_consult_07_one_utterance_has_ms_en_nan_and_family_hearsay`
- `trilingual-consult/tests/test_pipeline.py::test_english_island_in_malay_matrix_canonicalises_metformin`
- `trilingual-consult/tests/test_pipeline.py::test_empty_hokkien_is_unsupported_and_not_nkda`
- `trilingual-consult/tests/test_polywer.py::test_polywer_tagged_hypothesis_is_per_language`

**Not covered.** No real recorded ms/en/nan consult has been evaluated. Every
multilingual test is a deterministic fixture, so error rates under genuine
code-switching are unknown. Hokkien is the weakest leg: the `nan` patterns
assume POJ romanisation and Whisper does not emit POJ, so that branch is
effectively unreachable from a real recording. There is no public
Malay/English/Hokkien medical consult corpus to evaluate against.

## 7 · Allergy at minute 2 — Survives as a mechanism

Server-side voice activity detection is on, so the provider emits a completed
item per utterance rather than one item at stop.

- `test_clinical_hardening_acceptance.py::test_twenty_minute_live_stream_surfaces_minute_two_alert_by_second_125` — drives a fake twenty-minute stream with an allergy statement at t=120s
- `test_clinical_voice_quality.py::test_live_provider_uses_server_vad_and_completes_before_final_commit`
- `test_clinical_hardening_unit.py::test_completed_live_segment_persists_addressable_provisional_alert`

**Not covered.** The live path is off by default. Timing against a real
provider under real network conditions has not been measured, and the
output-silence watchdog interacts with turn cadence in ways only a real
session will expose.

## 8 · 45-second model hang — Partial

- `test_text_job_deadline.py::test_45_second_remote_hang_is_delayed_and_marked_timed_out` — the hang is cut at the 15-second stage cap and the pipeline re-runs deterministically
- `test_text_job_deadline.py::test_sequential_text_stages_share_one_whole_job_deadline`
- `test_clinical_voice_quality.py::test_remote_audio_45_second_hang_maps_to_typed_timeout`
- `test_clinical_hardening_unit.py::test_live_first_result_and_output_silence_have_independent_deadlines`

**Not covered.** The voice path has no whole-job deadline, only a 600-second
per-call timeout for local inference. The review screen shows an indefinite
spinner with no elapsed time and no way to retry a transcript-less session.
`VOICE_JOB_TIMEOUT_SECONDS`, the elapsed-time badge and the retry button were
dropped from this freeze. This is the least-improved scenario.

## 9 · Provider 503 for an hour — Survives

- `test_provider_outage_acceptance.py::test_one_hour_503_outage_persists_recovery_and_review_only_glance`
- `test_clinical_blockers.py::test_retryable_audio_failure_keeps_processing_and_projects_audio_circuit`

**Video.** `Nightingale_Scenario_09-provider-outage.mp4`

## 10 · Two clinicians type at 09:14 — Survives

Lost-update prevention under thread contention, with the second writer warned
before it saves rather than after.

- `test_concurrent_edits.py::test_same_entry_has_one_success_and_one_deterministic_409`
- `test_concurrent_edits.py::test_different_entries_can_be_updated_independently`
- `test_collaboration_api.py::test_comment_creation_requires_current_entry_etag`
- `test_collaboration_api.py::test_assignment_if_match_serializes_racing_updates`
- `test_collaboration_api.py::test_resolution_if_match_serializes_race_and_detects_second_mutation_after_load`
- `test_collaboration_api.py::test_editor_presence_is_scoped_content_free_and_expires`
- `frontend/tests/hardening.spec.ts` — `two editors must load latest and explicitly reconcile before saving`
- `frontend/tests/scenarios.spec.ts` — `[Scenario D] stale ETag conflicts while independent entries and tenant boundaries hold`

**Video.** `Nightingale_Scenario_10-concurrent-edits.mp4`

## 11 · Link generated, never received — Partial

- `test_messaging_contracts.py::test_receipt_state_transitions_include_resubmission_and_acknowledgement`
- `test_messaging_contracts.py::test_queue_notification_recovers_matching_insert_race_and_rejects_conflict`
- `test_messaging_contracts.py::test_dispatch_guard_exhaustion_failure_and_due_batch`
- `test_notification_route_contracts.py::test_create_resend_revoke_and_acknowledge_reject_invalid_states` — a delivered SMS cannot be recalled, except for enrolment and one-time codes where revocation is how the live token is invalidated
- `test_notification_route_contracts.py::test_twilio_callback_rejects_missing_envelope_bad_sid_and_attempt_mismatch`
- `test_clinical_hardening_acceptance.py::test_medication_gate_and_correction_survive_delivery_failure_then_acknowledge`

**Not covered.** Live delivery. The deterministic stub reports success, so a
queued message looks delivered — the honest default, not the state machine, is
what fails here.

## 12 · Wrong dosage in a patient summary — Survives

Four-axis dose attestation validated on client and server, formulary screening
at publication, and supersede-and-recall on correction.

- `test_clinical_hardening_acceptance.py::test_medication_publication_cannot_be_superseded_without_correction`
- `test_clinical_hardening_acceptance.py::test_medication_gate_and_correction_survive_delivery_failure_then_acknowledge`
- `test_safety_edge_coverage.py::test_screening_reports_every_missing_or_invalid_regimen_dimension`
- `test_clinical_voice_quality.py::test_versioned_formulary_fails_closed_for_unknown_range_and_allergy`
- `test_formulary_admin.py::test_admin_formulary_version_lifecycle_is_audited_fail_closed_and_single_active`
- `test_trusted_decision_semantics.py::test_medication_dose_route_frequency_conflicts`
- `trilingual-consult/tests/test_agents.py::test_dose_mismatch_conflicts_and_blocks_publish`

**Video.** `Nightingale_Scenario_12-medication-gate.mp4`

**Boundary.** The formulary vocabulary is deliberately small and fails closed
on anything outside it. `MedicationReviewCandidate` is rebuilt per request
rather than persisted.

## 13 · Nurse allergy vs patient "no allergies" — Survives

Scope- and polarity-aware conflict detection, critical severity, no
source-precedence auto-resolution, and a card that names each side's origin,
role, section and language.

- `test_clinical_blockers.py::test_nurse_allergy_and_patient_via_ai_nkda_are_critical_without_mutating_anchor`
- `test_clinical_blockers.py::test_patient_may_contradict_the_record_and_the_conflict_is_kept` — the publication gate exempts a patient writing in their own channel while still blocking outbound sharing
- `test_clinical_blockers.py::test_patient_authored_note_can_create_its_own_provenance`
- `test_clinical_hardening_acceptance.py::test_generic_nkda_conflict_is_critical_in_both_insertion_orders`
- `test_clinical_hardening_unit.py::test_specific_negation_is_not_misclassified_as_global_nka`
- `test_trusted_decision_semantics.py::test_human_ai_voice_conflicts`

**Video.** `Nightingale_Scenario_13-allergy-vs-nkda.mp4`

The last three tests exist because filming this scenario found two defects the
suite never reached: a patient note that produced a clinical fact returned a
500 (the row-level write check validated a provenance pointer by querying for
that pointer), and the unresolved-conflict gate refused the patient's own
statement instead of only blocking outbound sharing.

## 14 · A number that means something — Survives

A model-derived highlight with no `DecisionAssessment` is unqualified, not
absent. Confidence is a Wilson lower bound from a harness whose negative
result gates the system.

- `test_clinical_blockers.py::test_model_derived_highlight_without_assessment_never_reaches_priorities`
- `test_clinical_blockers.py::test_incomplete_ai_assessment_coverage_keeps_job_review_required`
- `test_clinical_blockers.py::test_partially_assessed_ai_entry_cannot_publish`
- `test_clinical_blockers.py::test_human_priorities_without_assessment_remain_displayable`
- `test_trusted_decision_semantics.py::test_calibration_report_model_and_hash_match`
- `test_trusted_decision_semantics.py::test_provider_confidence_is_not_used_directly`
- `test_trusted_decision_semantics.py::test_abstained_highlight_never_enters_ready_glance`
- `test_trusted_decision_semantics.py::test_model_cannot_lower_deterministic_risk_floor`
- `test_rls_migration.py::test_calibration_report_sample_accounting_is_database_enforced`

**Video.** `Nightingale_Scenario_14-meaningful-numbers.mp4`

**Measured, negative, kept.** Fact extraction calibrates to `low` at a 0.0437
accuracy lower bound over 176 judged facts; voice calibrates to `low` with a
0.200 word error rate over 2,206 segment decisions. Those are why the system
abstains on every AI fact extraction today.

## 15 · Learning loop bias, tired dismissal — Survives (floor), Partial (exposure)

Clinic-scoped, bounded, auditable weighting. Allergy and medication weights
cannot go negative, in code and by database check constraint.

- `test_self_learning_importance.py::test_allergy_feedback_is_telemetry_only_and_cannot_hide_the_priority`
- `test_self_learning_importance.py::test_medication_feedback_is_telemetry_only_and_cannot_learn_a_negative_weight`
- `test_self_learning_importance.py::test_clinic_feedback_math_is_bounded_and_diminishing`
- `test_self_learning_importance.py::test_feature_weight_bound_is_declared_as_a_database_invariant`
- `test_self_learning_importance.py::test_feature_keys_are_non_phi_bounded_tokens`
- `test_self_learning_importance.py::test_protected_highlight_ignores_negative_learned_score`
- `test_self_learning_importance.py::test_dismiss_feedback_is_negative_idempotent_and_resource_bound`
- `test_self_learning_importance.py::test_pin_learning_is_clinic_scoped_idempotent_clamped_and_patient_safe`
- `test_importance_exposure_qualification.py::test_incomplete_exposure_set_fails_persisted_api_qualification`
- `test_importance_exposure_qualification.py::test_complete_report_qualifies_and_guards_active_mode`

**Not covered.** Exposure is measured and qualified but not corrected: no
randomised exploration, no inverse-propensity weighting. Impressions remain
telemetry and are not consumed by the scoring formula, so this build must not
claim an exploration experiment.

## 16 · Highlight cites an edited note — Survives (backend), Partial (UI)

Anchors are pinned to an entry version and frozen by database trigger.
Resolution re-verifies offsets, exact text, digest and context; a source edit
marks dependents historical automatically.

- `test_highlight_provenance.py::test_provenance_resolves_against_immutable_version_after_edit`
- `test_highlight_provenance.py::test_invalid_anchor_is_explicitly_orphaned_and_never_guessed`
- `test_clinical_hardening_acceptance.py::test_source_edit_requires_support_review_while_original_pointer_resolves`
- `test_clinical_hardening_acceptance.py::test_patient_owned_source_edit_also_invalidates_clinical_highlight_support`
- `test_rls_migration.py::test_highlight_anchor_fields_are_database_immutable`
- `test_clinical_blockers.py::test_candidate_fingerprint_is_database_immutable`
- `frontend/tests/scenarios.spec.ts` — `[Scenario A] Glance opens the exact immutable timeline span`

**Not covered.** The original-versus-current diff pane and the
`/highlight-support-reviews` listing consumer were not landed, so a clinician
sees that support needs review without seeing what changed.

---

## Scenarios not on video

Seven scenarios are filmed. The other nine are not, for stated reasons:

- **1, 11** depend on live message delivery, which is unconfigured here.
- **3, 4** are invariants below the interface — a log allowlist and an egress
  gateway. There is nothing to see on screen that would prove either.
- **6** has no real recorded consult to play.
- **7** requires a live voice session, which is off by default and needs
  remote-audio consent.
- **8** would film an indefinite spinner, which is the defect, not the feature.
- **15, 16** are demonstrable and were not filmed in this freeze.
