import pytest

from app.main import app
from app.models import RiskReason, normalize_risk_reason

EXPECTED_RISK_REASONS = {
    "critical",
    "unresolved",
    "clinician_confirmed",
    "clinical_entity",
    "clinic_feedback",
    "recency",
    "clinician_accepted",
    "care_plan_conflict",
    "clinician_confirmed_follow_up",
    "medication_status_conflict",
    "open_medication_reconciliation",
    "scheduled_follow_up",
    "synthetic_dataset_recent_encounter",
    "unavailable_review_required",
}


@pytest.mark.unit
def test_risk_reason_enum_is_exhaustive_and_unknown_values_fail_closed() -> None:
    assert {reason.value for reason in RiskReason} == EXPECTED_RISK_REASONS
    assert all(
        normalize_risk_reason(value).value == value for value in EXPECTED_RISK_REASONS
    )
    assert (
        normalize_risk_reason("legacy_or_corrupt_value")
        == RiskReason.UNAVAILABLE_REVIEW_REQUIRED
    )


@pytest.mark.unit
def test_openapi_exposes_one_shared_risk_reason_enum() -> None:
    schemas = app.openapi()["components"]["schemas"]
    assert set(schemas["RiskReason"]["enum"]) == EXPECTED_RISK_REASONS
    for public_schema in ("HighlightPublic", "ClinicalGlanceCard"):
        assert schemas[public_schema]["properties"]["risk_reason"] == {
            "$ref": "#/components/schemas/RiskReason"
        }
