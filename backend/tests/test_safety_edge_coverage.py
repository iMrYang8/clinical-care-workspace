from __future__ import annotations

import math
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session

from app.api.deps import RequestContext
from app.api.routes import formulary as formulary_routes
from app.models import (
    CalibrationReport,
    ClinicFormularyConcept,
    ClinicFormularyConceptCreate,
    ClinicFormularyQualificationRequest,
    ClinicFormularyVersion,
    ClinicFormularyVersionCreate,
    ClinicMembership,
    DecisionAssessment,
    EvaluationRun,
    Highlight,
    ImportanceExposureQualificationReport,
    User,
)
from app.services import clinical_formulary
from app.services.clinical_formulary import (
    FORMULARY_VERSION,
    FormularyConfigurationError,
)
from app.services.decisioning import (
    assessment_review_state,
    deterministic_risk,
    qualify_calibration_report,
    requalify_assessment_confidence,
    request_parameters_sha256,
)
from app.services.importance import (
    IMPORTANCE_EXPOSURE_REPORT_VERSION,
    generate_importance_exposure_report,
    importance_report_current_reasons,
)

pytestmark = pytest.mark.unit


class _Rows:
    def __init__(self, values: list[Any]) -> None:
        self.values = values

    def all(self) -> list[Any]:
        return self.values

    def first(self) -> Any | None:
        return self.values[0] if self.values else None


class _ScriptedSession:
    def __init__(self, *responses: list[Any]) -> None:
        self._responses = iter(responses)
        self.added: list[Any] = []
        self.committed = False
        self.rolled_back = False

    def exec(self, _statement: Any) -> _Rows:
        return _Rows(list(next(self._responses)))

    def add(self, value: Any) -> None:
        self.added.append(value)

    def flush(self) -> None:
        return None

    def commit(self) -> None:
        self.committed = True

    def rollback(self) -> None:
        self.rolled_back = True

    def refresh(self, _value: Any) -> None:
        return None


def _concept(
    suffix: str = "a",
    **overrides: Any,
) -> ClinicFormularyConceptCreate:
    payload: dict[str, Any] = {
        "concept_code": f"rxnorm:test-{suffix}",
        "canonical_name": f"test drug {suffix}",
        "multilingual_aliases": {
            "en": [f"test drug {suffix}"],
            "ms": [f"ubat ujian {suffix}"],
            "nan": [f"chhi giok {suffix}"],
            "zh": [f"测试药{suffix}"],
        },
        "dose_unit": "mg",
        "minimum_single_dose": 1.0,
        "maximum_single_dose": 100.0,
        "permitted_routes": ["oral"],
        "contraindicated_allergy_concepts": [f"allergy:test-{suffix}"],
    }
    payload.update(overrides)
    # Some cases deliberately cross the service boundary with values rejected by
    # ordinary request validation.  The service must still fail closed for data
    # loaded from an old row, import, or internal caller.
    return ClinicFormularyConceptCreate.model_construct(**payload)


@pytest.mark.parametrize(
    ("concepts", "error_code"),
    [
        ([], "FORMULARY_CONCEPTS_REQUIRED"),
        ([_concept(concept_code=" bad")], "FORMULARY_CONCEPT_CODE_INVALID"),
        ([_concept(concept_code="!!!")], "FORMULARY_CONCEPT_CODE_INVALID"),
        (
            [_concept("a"), _concept("b", concept_code="rxnorm:test-a")],
            "FORMULARY_CONCEPT_CODE_DUPLICATE",
        ),
        (
            [_concept(multilingual_aliases={"en": ["test"]})],
            "FORMULARY_MULTILINGUAL_ALIASES_INCOMPLETE",
        ),
        (
            [
                _concept(
                    multilingual_aliases={
                        "en": [],
                        "ms": ["ubat"],
                        "nan": ["chhi giok"],
                        "zh": ["测试药"],
                    }
                )
            ],
            "FORMULARY_MULTILINGUAL_ALIASES_INCOMPLETE",
        ),
        (
            [
                _concept(
                    multilingual_aliases={
                        "en": ["!!!"],
                        "ms": ["ubat"],
                        "nan": ["chhi giok"],
                        "zh": ["测试药"],
                    }
                )
            ],
            "FORMULARY_ALIAS_INVALID",
        ),
        (
            [
                _concept(
                    multilingual_aliases={
                        "en": ["same", "same"],
                        "ms": ["ubat"],
                        "nan": ["chhi giok"],
                        "zh": ["测试药"],
                    }
                )
            ],
            "FORMULARY_ALIAS_DUPLICATE",
        ),
        (
            [
                _concept(
                    "a",
                    multilingual_aliases={
                        "en": ["shared alias"],
                        "ms": ["ubat a"],
                        "nan": ["chhi a"],
                        "zh": ["药甲"],
                    },
                ),
                _concept(
                    "b",
                    multilingual_aliases={
                        "en": ["shared-alias"],
                        "ms": ["ubat b"],
                        "nan": ["chhi b"],
                        "zh": ["药乙"],
                    },
                ),
            ],
            "FORMULARY_ALIAS_AMBIGUOUS",
        ),
        (
            [
                _concept("a"),
                _concept("b", canonical_name="test drug a"),
            ],
            "FORMULARY_ALIAS_AMBIGUOUS",
        ),
        ([_concept(dose_unit="mcg")], "FORMULARY_DOSE_UNIT_UNKNOWN"),
        (
            [_concept(minimum_single_dose=0.0)],
            "FORMULARY_DOSE_RANGE_INVALID",
        ),
        (
            [_concept(minimum_single_dose=math.nan)],
            "FORMULARY_DOSE_RANGE_INVALID",
        ),
        ([_concept(permitted_routes=[])], "FORMULARY_ROUTE_UNKNOWN"),
        (
            [_concept(permitted_routes=["oral", "oral"])],
            "FORMULARY_ROUTE_UNKNOWN",
        ),
        ([_concept(permitted_routes=["rectal"])], "FORMULARY_ROUTE_UNKNOWN"),
        (
            [_concept(contraindicated_allergy_concepts=["!"])],
            "FORMULARY_ALLERGY_CONCEPT_INVALID",
        ),
        (
            [_concept(contraindicated_allergy_concepts=["allergy:a", "allergy:a"])],
            "FORMULARY_ALLERGY_CONCEPT_DUPLICATE",
        ),
    ],
)
def test_formulary_configuration_rejects_unsafe_or_ambiguous_content(
    concepts: list[ClinicFormularyConceptCreate], error_code: str
) -> None:
    with pytest.raises(FormularyConfigurationError) as exc_info:
        clinical_formulary.clinic_formulary_content_sha256(concepts)
    assert exc_info.value.code == error_code


def test_fixture_hash_and_unknown_version_contracts_are_explicit() -> None:
    concepts = clinical_formulary.formulary_concepts()
    digest = clinical_formulary.formulary_content_sha256(concepts)

    assert len(digest) == 64
    assert digest == clinical_formulary.formulary_content_sha256(reversed(concepts))
    assert clinical_formulary.formulary_concepts(version="missing") == ()
    assert (
        clinical_formulary.canonicalize_medication("metformin", version="missing")
        is None
    )
    with pytest.raises(FormularyConfigurationError) as exc_info:
        clinical_formulary.validate_formulary_template("missing")
    assert exc_info.value.code == "FORMULARY_TEMPLATE_UNKNOWN"


def test_screening_reports_every_missing_or_invalid_regimen_dimension() -> None:
    missing = clinical_formulary.screen_medication_regimen(
        medication=" ",
        dose_value=cast(Any, "five hundred"),
        dose_unit=None,
        route=None,
        frequency=None,
        version="missing",
    )
    assert missing.state == "review_required"
    assert set(missing.reason_codes) == {
        "FORMULARY_VERSION_UNAVAILABLE",
        "MEDICATION_MISSING",
        "DOSE_MISSING",
        "DOSE_UNIT_MISSING",
        "ROUTE_MISSING",
        "FREQUENCY_MISSING",
    }

    invalid = clinical_formulary.screen_medication_regimen(
        medication="metformin",
        dose_value=-1,
        dose_unit="ml",
        route="intravenous",
        frequency="daily",
    )
    assert invalid.eligible is False
    assert set(invalid.reason_codes) == {
        "DOSE_INVALID",
        "DOSE_UNIT_NOT_PERMITTED",
        "ROUTE_NOT_PERMITTED",
    }


def _version(
    *,
    clinic_id: uuid.UUID | None = None,
    status: str = "active",
    content_sha256: str = "a" * 64,
    locked: bool = True,
    qualified: bool = True,
) -> ClinicFormularyVersion:
    now = datetime.now(UTC)
    return ClinicFormularyVersion(
        clinic_id=clinic_id or uuid.uuid4(),
        version_code="clinic-formulary-v1",
        status=status,
        content_sha256=content_sha256,
        effective_at=now,
        content_locked_at=now if locked else None,
        qualified_at=now if qualified else None,
        qualification_source="clinic_admin" if qualified else None,
    )


def _concept_row(
    version: ClinicFormularyVersion,
    *,
    active: bool = True,
    canonical_name: str = "test drug",
) -> ClinicFormularyConcept:
    return ClinicFormularyConcept(
        clinic_id=version.clinic_id,
        formulary_version_id=version.id,
        concept_code="rxnorm:test",
        canonical_name=canonical_name,
        multilingual_aliases_json={
            "en": ["test drug"],
            "ms": ["ubat ujian"],
            "nan": ["chhi giok"],
            "zh": ["测试药"],
        },
        dose_unit="mg",
        minimum_single_dose=1,
        maximum_single_dose=100,
        permitted_routes_json=["oral"],
        contraindicated_allergy_concepts_json=[],
        active=active,
    )


def test_database_formulary_rows_fail_closed_when_inactive_or_malformed() -> None:
    version = _version()
    malformed = _concept_row(version, canonical_name="")
    inactive = _concept_row(version, active=False)

    assert clinical_formulary._database_concept(malformed) is None
    with pytest.raises(FormularyConfigurationError) as exc_info:
        clinical_formulary._database_concept_create(inactive)
    assert exc_info.value.code == "FORMULARY_INACTIVE_CONCEPT_PRESENT"
    assert clinical_formulary._version_digest([inactive]) == (
        None,
        "CLINIC_FORMULARY_CONTENT_INVALID",
    )


def test_clinic_screening_and_readiness_reject_ambiguous_or_invalid_versions() -> None:
    clinic_id = uuid.uuid4()
    one = _version(clinic_id=clinic_id)
    two = _version(clinic_id=clinic_id)
    ambiguous_session = _ScriptedSession([one, two])
    ambiguous = clinical_formulary.screen_clinic_medication_regimen(
        cast(Session, ambiguous_session),
        clinic_id=clinic_id,
        medication="metformin",
        dose_value=500,
        dose_unit="mg",
        route="oral",
        frequency="daily",
    )
    assert ambiguous.reason_codes == ("CLINIC_FORMULARY_VERSION_AMBIGUOUS",)

    invalid_session = _ScriptedSession([one], [_concept_row(one, canonical_name="")])
    invalid = clinical_formulary.screen_clinic_medication_regimen(
        cast(Session, invalid_session),
        clinic_id=clinic_id,
        medication="metformin",
        dose_value=500,
        dose_unit="mg",
        route="oral",
        frequency="daily",
    )
    assert invalid.reason_codes == ("CLINIC_FORMULARY_CONTENT_INVALID",)

    assert (
        clinical_formulary.clinic_formulary_readiness(
            cast(Session, _ScriptedSession([])), clinic_id
        ).reason_code
        == "clinic_formulary_active_version_missing"
    )
    assert (
        clinical_formulary.clinic_formulary_readiness(
            cast(Session, _ScriptedSession([one, two])), clinic_id
        ).reason_code
        == "clinic_formulary_active_version_ambiguous"
    )
    invalid_readiness = clinical_formulary.clinic_formulary_readiness(
        cast(Session, _ScriptedSession([one], [])), clinic_id
    )
    assert invalid_readiness.ready is False
    assert invalid_readiness.reason_code == "clinic_formulary_active_version_invalid"


def test_formulary_version_projection_marks_digest_or_lock_damage_invalid() -> None:
    version = _version(locked=False)
    row = _concept_row(version)
    projected = clinical_formulary.formulary_version_public(
        cast(Session, _ScriptedSession([row])), version
    )

    assert projected.digest_matches is False
    assert projected.qualification_state == "invalid"
    assert projected.concept_count == 1


def _digest_bound_version(
    *, status: str = "draft", qualified: bool = False
) -> tuple[ClinicFormularyVersion, ClinicFormularyConcept]:
    version = _version(status=status, qualified=qualified)
    row = _concept_row(version)
    version.content_sha256 = clinical_formulary._database_content_sha256([row])
    return version, row


def test_formulary_qualification_lifecycle_rejects_stale_or_tampered_state() -> None:
    clinic_id = uuid.uuid4()
    member_id = uuid.uuid4()
    missing = clinical_formulary.qualify_clinic_formulary_version(
        cast(Session, _ScriptedSession([])),
        clinic_id=clinic_id,
        membership_id=member_id,
        version_id=uuid.uuid4(),
        expected_content_sha256="a" * 64,
    )
    assert missing is None

    active = _version(clinic_id=clinic_id)
    with pytest.raises(FormularyConfigurationError) as not_qualifiable:
        clinical_formulary.qualify_clinic_formulary_version(
            cast(Session, _ScriptedSession([active])),
            clinic_id=clinic_id,
            membership_id=member_id,
            version_id=active.id,
            expected_content_sha256="a" * 64,
        )
    assert not_qualifiable.value.code == "FORMULARY_VERSION_NOT_QUALIFIABLE"

    draft = _version(clinic_id=clinic_id, status="draft", qualified=False)
    with pytest.raises(FormularyConfigurationError) as invalid_expected:
        clinical_formulary.qualify_clinic_formulary_version(
            cast(Session, _ScriptedSession([draft])),
            clinic_id=clinic_id,
            membership_id=member_id,
            version_id=draft.id,
            expected_content_sha256="not-a-digest",
        )
    assert invalid_expected.value.code == "FORMULARY_EXPECTED_DIGEST_INVALID"

    inactive = _concept_row(draft, active=False)
    with pytest.raises(FormularyConfigurationError) as invalid_content:
        clinical_formulary.qualify_clinic_formulary_version(
            cast(Session, _ScriptedSession([draft], [inactive])),
            clinic_id=clinic_id,
            membership_id=member_id,
            version_id=draft.id,
            expected_content_sha256="a" * 64,
        )
    assert invalid_content.value.code == "FORMULARY_CONTENT_DIGEST_INVALID"

    qualified, row = _digest_bound_version(status="draft", qualified=True)
    qualified.qualification_source = "platform_template"
    qualified.qualified_by_membership_id = None
    with pytest.raises(FormularyConfigurationError) as actor_mismatch:
        clinical_formulary.qualify_clinic_formulary_version(
            cast(Session, _ScriptedSession([qualified], [row])),
            clinic_id=qualified.clinic_id,
            membership_id=member_id,
            version_id=qualified.id,
            expected_content_sha256=qualified.content_sha256,
        )
    assert actor_mismatch.value.code == "FORMULARY_ALREADY_QUALIFIED"


def test_formulary_activation_and_template_seed_are_idempotent_only_when_ready() -> (
    None
):
    clinic_id = uuid.uuid4()
    member_id = uuid.uuid4()
    missing = clinical_formulary.activate_clinic_formulary_version(
        cast(Session, _ScriptedSession([])),
        clinic_id=clinic_id,
        membership_id=member_id,
        version_id=uuid.uuid4(),
        expected_content_sha256="a" * 64,
    )
    assert missing == (None, None)

    retired = _version(clinic_id=clinic_id, status="retired")
    with pytest.raises(FormularyConfigurationError) as immutable:
        clinical_formulary.activate_clinic_formulary_version(
            cast(Session, _ScriptedSession([retired])),
            clinic_id=clinic_id,
            membership_id=member_id,
            version_id=retired.id,
            expected_content_sha256=retired.content_sha256,
        )
    assert immutable.value.code == "FORMULARY_RETIRED_VERSION_IMMUTABLE"

    draft = _version(clinic_id=clinic_id, status="draft", qualified=False)
    with pytest.raises(FormularyConfigurationError) as unqualified:
        clinical_formulary.activate_clinic_formulary_version(
            cast(Session, _ScriptedSession([draft], [])),
            clinic_id=clinic_id,
            membership_id=member_id,
            version_id=draft.id,
            expected_content_sha256="a" * 64,
        )
    assert unqualified.value.code == "FORMULARY_ACTIVATION_NOT_QUALIFIED"

    active, row = _digest_bound_version(status="active", qualified=True)
    result = clinical_formulary.activate_clinic_formulary_version(
        cast(Session, _ScriptedSession([active], [row])),
        clinic_id=active.clinic_id,
        membership_id=member_id,
        version_id=active.id,
        expected_content_sha256=active.content_sha256,
    )
    assert result == (active, None)

    with pytest.raises(FormularyConfigurationError) as unknown_template:
        clinical_formulary.seed_clinic_formulary_template(
            cast(Session, _ScriptedSession()),
            clinic_id=clinic_id,
            template="unknown-template",
        )
    assert unknown_template.value.code == "FORMULARY_TEMPLATE_UNKNOWN"

    existing_draft = _version(
        clinic_id=clinic_id,
        status="draft",
        qualified=False,
    )
    existing_draft.version_code = FORMULARY_VERSION
    with pytest.raises(FormularyConfigurationError) as seed_conflict:
        clinical_formulary.seed_clinic_formulary_template(
            cast(Session, _ScriptedSession([existing_draft], [])),
            clinic_id=clinic_id,
            template=FORMULARY_VERSION,
        )
    assert seed_conflict.value.code == "FORMULARY_TEMPLATE_SEED_CONFLICT"

    ready, ready_row = _digest_bound_version(status="active", qualified=True)
    ready.version_code = FORMULARY_VERSION
    seeded = clinical_formulary.seed_clinic_formulary_template(
        cast(Session, _ScriptedSession([ready], [ready], [ready_row])),
        clinic_id=ready.clinic_id,
        template=FORMULARY_VERSION,
    )
    assert seeded is ready


def test_invalid_formulary_version_code_is_rejected_before_database_write() -> None:
    body = ClinicFormularyVersionCreate(
        version_code="bad code",
        concepts=[_concept()],
    )
    with pytest.raises(FormularyConfigurationError) as exc_info:
        clinical_formulary.create_clinic_formulary_draft(
            cast(Session, _ScriptedSession()),
            clinic_id=uuid.uuid4(),
            membership_id=uuid.uuid4(),
            body=body,
        )
    assert exc_info.value.code == "FORMULARY_VERSION_CODE_INVALID"


def _valid_run_and_report() -> tuple[EvaluationRun, CalibrationReport]:
    clinic_id = uuid.uuid4()
    parameters: dict[str, object] = {"schema": "clinical-fact-v2"}
    run = EvaluationRun(
        clinic_id=clinic_id,
        provider="provider-a",
        exact_model_id="model-a",
        task="task-a",
        request_parameters_json=parameters,
        dataset_manifest_sha256="a" * 64,
        code_commit="b" * 40,
        calibration_split="80 samples",
        holdout_split="20 consultations",
        total_sample_count=200,
        calibration_sample_count=80,
        holdout_sample_count=120,
        sample_count=120,
        status="completed",
        metrics_json={"accuracy": 0.95},
    )
    report = CalibrationReport(
        clinic_id=clinic_id,
        evaluation_run_id=run.id,
        provider=run.provider,
        exact_model_id=run.exact_model_id,
        task=run.task,
        request_parameters_sha256=request_parameters_sha256(parameters),
        dataset_manifest_sha256=run.dataset_manifest_sha256,
        code_commit=run.code_commit,
        total_sample_count=run.total_sample_count,
        calibration_sample_count=run.calibration_sample_count,
        holdout_sample_count=run.holdout_sample_count,
        sample_count=run.sample_count,
        consultation_count=20,
        confidence_band="high",
        accuracy_lower_bound=0.9,
        metrics_json={"nested": [0.9, {"bound": 0.8}]},
        expires_at=datetime.now(UTC) + timedelta(days=1),
    )
    return run, report


def test_calibration_qualification_collects_all_malformed_identity_and_count_reasons() -> (
    None
):
    run, report = _valid_run_and_report()
    report.expires_at = datetime.now() + timedelta(days=1)
    report.confidence_band = "invented"
    report.accuracy_lower_bound = math.nan
    report.sample_count = 1
    report.holdout_sample_count = 1
    report.consultation_count = 2
    report.request_parameters_sha256 = "invalid"
    report.dataset_manifest_sha256 = "invalid"
    report.code_commit = "0000000"
    report.metrics_json = {"nested": [math.nan]}
    run.status = "pending"
    run.provider = "provider-b"
    run.exact_model_id = "model-b"
    run.task = "task-b"
    run.dataset_manifest_sha256 = "invalid"
    run.code_commit = "invalid"
    run.sample_count = 200
    run.metrics_json = {"nested": (math.inf,)}

    result = qualify_calibration_report(
        cast(Session, _ScriptedSession([run])),
        report,
        provider="expected-provider",
        exact_model_id="expected-model",
        task="expected-task",
        request_parameters={"expected": True},
        dataset_manifest_sha256="c" * 64,
        code_commit="d" * 40,
        now=datetime.now(),
    )

    assert result.qualified is False
    assert set(result.reasons) >= {
        "CALIBRATION_BAND_INVALID",
        "CALIBRATION_LOWER_BOUND_INVALID",
        "CALIBRATION_SAMPLE_COUNT_INSUFFICIENT",
        "CALIBRATION_CONSULTATION_COUNT_INSUFFICIENT",
        "CALIBRATION_SAMPLE_COUNTS_INCONSISTENT",
        "CALIBRATION_CONFIGURATION_IDENTITY_INVALID",
        "CALIBRATION_DATASET_IDENTITY_INVALID",
        "CALIBRATION_CODE_IDENTITY_INVALID",
        "CALIBRATION_METRICS_NON_FINITE",
        "CALIBRATION_PROVIDER_MISMATCH",
        "CALIBRATION_MODEL_MISMATCH",
        "CALIBRATION_TASK_MISMATCH",
        "CALIBRATION_CONFIGURATION_MISMATCH",
        "CALIBRATION_DATASET_MISMATCH",
        "CALIBRATION_CODE_MISMATCH",
        "CALIBRATION_EVALUATION_RUN_INCOMPLETE",
    }


def test_calibration_requires_its_evaluation_run() -> None:
    _, report = _valid_run_and_report()
    result = qualify_calibration_report(cast(Session, _ScriptedSession([])), report)
    assert result.reasons == ("CALIBRATION_EVALUATION_RUN_MISSING",)


def test_read_time_assessment_requalification_rejects_persisted_mismatches() -> None:
    run, report = _valid_run_and_report()
    assessment = DecisionAssessment(
        clinic_id=report.clinic_id,
        highlight_id=uuid.uuid4(),
        output_type="summary",
        calibration_report_id=report.id,
        calibration_version="stale-report-id",
        confidence_band="medium",
        confidence_lower_bound=0.4,
    )
    result = requalify_assessment_confidence(
        cast(Session, _ScriptedSession([report], [run])),
        assessment,
    )

    assert result.current_state == "unavailable"
    assert set(result.reasons) >= {
        "ASSESSMENT_CALIBRATION_VERSION_MISMATCH",
        "ASSESSMENT_CONFIDENCE_BAND_MISMATCH",
        "ASSESSMENT_CONFIDENCE_BOUND_MISMATCH",
    }


def test_risk_conflicts_and_abstained_assessments_remain_safety_visible() -> None:
    allergy = deterministic_risk(fact_type="allergy", text="rash", conflict=True)
    dose = deterministic_risk(fact_type="dose", text="dose differs", conflict=True)
    assert allergy.effective_risk == "critical"
    assert allergy.rule_ids == ["ALLERGY_CONFLICT"]
    assert dose.effective_risk == "high"
    assert dose.rule_ids == ["DOSE_CONFLICT"]

    highlight = Highlight(
        clinic_id=uuid.uuid4(),
        patient_id=uuid.uuid4(),
        entry_id=uuid.uuid4(),
        entry_version_id=uuid.uuid4(),
        start_offset=0,
        end_offset=1,
        exact_quote_ciphertext=b"x",
        label="review",
    )
    assessment = DecisionAssessment(
        clinic_id=highlight.clinic_id,
        highlight_id=highlight.id,
        abstained=True,
    )
    assert assessment_review_state(assessment, highlight) == "abstained"


def _importance_report(now: datetime) -> ImportanceExposureQualificationReport:
    return ImportanceExposureQualificationReport(
        clinic_id=uuid.uuid4(),
        report_version="stale-version",
        window_start=now - timedelta(hours=1),
        window_end=now + timedelta(minutes=6),
        source_candidate_set_count=0,
        candidate_count=0,
        telemetry_count=0,
        displayed_count=0,
        protected_candidate_count=0,
        protected_displayed_count=0,
        ordinary_candidate_count=0,
        ordinary_displayed_count=0,
        protected_recall=0,
        ordinary_recall=0,
        ordinary_exposure_rate=0,
        missing_telemetry_count=0,
        duplicate_telemetry_count=0,
        qualified=False,
        qualification_reasons_json=["source_report_failed"],
        generated_by_membership_id=uuid.uuid4(),
        expires_at=now - timedelta(seconds=1),
        created_at=now - timedelta(days=1),
    )


def test_importance_report_currentness_rejects_every_stale_boundary() -> None:
    now = datetime.now(UTC)
    reasons = importance_report_current_reasons(_importance_report(now), now=now)
    assert reasons == (
        "importance_exposure_report_version_mismatch",
        "importance_exposure_report_not_qualified",
        "source_report_failed",
        "importance_exposure_report_expired",
        "importance_exposure_report_future_window",
        "importance_exposure_report_window_mismatch",
    )


def test_empty_importance_audit_is_persisted_but_never_qualified() -> None:
    now = datetime.now(UTC)
    session = _ScriptedSession([], [])
    report = generate_importance_exposure_report(
        cast(Session, session),
        clinic_id=uuid.uuid4(),
        generated_by_membership_id=uuid.uuid4(),
        now=now,
    )

    assert report.report_version == IMPORTANCE_EXPOSURE_REPORT_VERSION
    assert report.qualified is False
    assert set(report.qualification_reasons_json) >= {
        "current_priorities_surface_empty",
        "clinical_review_surface_empty",
        "candidate_sets_missing",
        "protected_candidates_missing",
        "ordinary_candidates_missing",
    }
    assert session.added == [report]


def _context(role: str = "admin") -> RequestContext:
    clinic_id = uuid.uuid4()
    user = User(email="clinic.admin@example.com", hashed_password="hash")
    membership = ClinicMembership(
        clinic_id=clinic_id,
        user_id=user.id,
        role=role,
    )
    return RequestContext(user=user, membership=membership)


def test_formulary_route_error_mapping_and_missing_reads_are_typed() -> None:
    invalid = formulary_routes._configuration_error(
        FormularyConfigurationError("FORMULARY_ALIAS_INVALID")
    )
    conflict = formulary_routes._configuration_error(
        FormularyConfigurationError("FORMULARY_CONTENT_DIGEST_INVALID")
    )
    assert (invalid.status_code, invalid.detail) == (
        422,
        {"code": "FORMULARY_ALIAS_INVALID", "review_required": True},
    )
    assert conflict.status_code == 409

    with pytest.raises(HTTPException) as forbidden:
        formulary_routes._require_admin(_context("staff"))
    assert forbidden.value.status_code == 403

    with pytest.raises(HTTPException) as missing:
        formulary_routes._version(
            cast(Session, _ScriptedSession([])), _context(), uuid.uuid4()
        )
    assert missing.value.status_code == 404


def test_formulary_create_route_translates_unique_conflict_and_rolls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _ScriptedSession()

    def _raise_integrity(*_args: Any, **_kwargs: Any) -> None:
        raise IntegrityError("insert formulary", {}, ValueError("duplicate"))

    monkeypatch.setattr(
        formulary_routes,
        "create_clinic_formulary_draft",
        _raise_integrity,
    )
    body = ClinicFormularyVersionCreate(
        version_code="clinic-v1",
        concepts=[_concept()],
    )

    with pytest.raises(HTTPException) as conflict:
        formulary_routes.create_formulary_version(
            body=body,
            session=cast(Session, session),
            context=_context(),
        )
    assert conflict.value.status_code == 409
    assert conflict.value.detail == {
        "code": "FORMULARY_VERSION_ALREADY_EXISTS",
        "review_required": True,
    }
    assert session.rolled_back is True


@pytest.mark.parametrize(
    "endpoint",
    [
        formulary_routes.qualify_formulary_version,
        formulary_routes.activate_formulary_version,
    ],
)
def test_formulary_mutation_routes_return_404_when_service_finds_no_version(
    monkeypatch: pytest.MonkeyPatch,
    endpoint: Callable[..., Any],
) -> None:
    if endpoint is formulary_routes.qualify_formulary_version:
        monkeypatch.setattr(
            formulary_routes,
            "qualify_clinic_formulary_version",
            lambda *args, **kwargs: None,
        )
    else:
        monkeypatch.setattr(
            formulary_routes,
            "activate_clinic_formulary_version",
            lambda *args, **kwargs: (None, None),
        )
    body = ClinicFormularyQualificationRequest(expected_content_sha256="a" * 64)

    with pytest.raises(HTTPException) as missing:
        endpoint(
            version_id=uuid.uuid4(),
            body=body,
            session=cast(Session, _ScriptedSession()),
            context=_context(),
        )
    assert missing.value.status_code == 404
