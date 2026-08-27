"""Deterministic synthetic personas used by the local demo and tests."""

import hashlib
import uuid
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlmodel import Session, col, select

from app.core.field_crypto import field_codec
from app.core.security import get_password_hash
from app.models import (
    AIRun,
    AuditEvent,
    CareTask,
    Clinic,
    ClinicalFactAssertion,
    ClinicMembership,
    Comment,
    CommentMention,
    ConflictCase,
    DecisionAssessment,
    Entry,
    EntryVersion,
    Highlight,
    ImportanceFeatureStat,
    ImportanceFeedbackEvent,
    Job,
    JobAttempt,
    Patient,
    PatientGlanceSnapshot,
    PatientIdentifier,
    PatientUserLink,
    PatientVisit,
    PlatformAdministrator,
    ProvenancePointer,
    RedactionEvaluationRun,
    RedactionRun,
    User,
    get_datetime_utc,
)

DEMO_NAMESPACE = uuid.UUID("8b54f8b7-7bca-4513-838c-15ac2b6758ad")


def demo_id(name: str) -> uuid.UUID:
    return uuid.uuid5(DEMO_NAMESPACE, name)


PERSONA_EMAILS = {
    "patient": "patient@nightingale.example",
    "staff": "staff@nightingale.example",
    "clinician": "clinician@nightingale.example",
    "admin": "admin@nightingale.example",
    "worker": "worker@nightingale.example",
    "other_staff": "staff@other-clinic.example",
}

PLATFORM_ADMIN_EMAIL = "platform.admin@nightingale.example"
PLATFORM_ADMIN_PASSWORD = "local-platform-owner-only"
OTHER_CLINICIAN_EMAIL = "clinician@other-clinic.example"


def _seed_redaction_qualification(session: Session, clinic_id: uuid.UUID) -> None:
    row_id = demo_id(f"redaction-evaluation-{clinic_id}")
    if session.get(RedactionEvaluationRun, row_id) is None:
        session.add(
            RedactionEvaluationRun(
                id=row_id,
                clinic_id=clinic_id,
                redactor_version="nightingale-redaction-v2",
                dataset_sha256=hashlib.sha256(
                    b"nightingale-synthetic-redaction-gold-v2"
                ).hexdigest(),
                sample_count=500,
                phi_recall=1.0,
                residual_phi_count=0,
                clinical_span_damage_count=0,
                passed=True,
                metrics_json={"provenance": "deterministic_synthetic_gold"},
            )
        )


def _seed_platform_administrator(session: Session) -> None:
    user = session.exec(select(User).where(User.email == PLATFORM_ADMIN_EMAIL)).first()
    if user is None:
        user = User(
            id=demo_id("user-platform-administrator"),
            email=PLATFORM_ADMIN_EMAIL,
            full_name="Nightingale Platform Administrator",
            hashed_password=get_password_hash(PLATFORM_ADMIN_PASSWORD),
        )
        session.add(user)
        session.flush()
    administrator = session.exec(
        select(PlatformAdministrator).where(PlatformAdministrator.user_id == user.id)
    ).first()
    if administrator is None:
        session.add(
            PlatformAdministrator(id=demo_id("platform-administrator"), user_id=user.id)
        )


def _seed_patient_identity(
    session: Session,
    *,
    clinic_id: uuid.UUID,
    patient_id: uuid.UUID,
    creator_membership_id: uuid.UUID,
    display_name: str,
    date_of_birth: str,
    medical_record_number: str,
    identity_number: str,
) -> None:
    patient = session.get(Patient, patient_id)
    if patient is None:
        return
    patient.date_of_birth_ciphertext = field_codec.encrypt_text(
        clinic_id, "patient.date_of_birth", patient.id, date_of_birth
    )
    patient.identity_match_hash = field_codec.blind_index(
        clinic_id,
        "patient_identity:name_dob",
        f"{display_name.casefold()}|{date_of_birth}",
    )
    patient.external_ref_hash = field_codec.blind_index(
        clinic_id, "patient_identifier:medical_record_number", medical_record_number
    )
    patient.created_by_membership_id = creator_membership_id
    session.add(patient)
    for identifier_type, value in (
        ("medical_record_number", medical_record_number),
        ("dataset_reference", identity_number),
    ):
        identifier_id = demo_id(f"patient-identifier-{patient_id}-{identifier_type}")
        if session.get(PatientIdentifier, identifier_id) is None:
            session.add(
                PatientIdentifier(
                    id=identifier_id,
                    clinic_id=clinic_id,
                    patient_id=patient_id,
                    identifier_type=identifier_type,
                    value_ciphertext=field_codec.encrypt_text(
                        clinic_id,
                        "patient_identifier.value",
                        identifier_id,
                        value,
                    ),
                    value_hmac=field_codec.blind_index(
                        clinic_id, f"patient_identifier:{identifier_type}", value
                    ),
                    masked_suffix=value[-4:],
                    created_by_membership_id=creator_membership_id,
                )
            )


def _fixture_time(value: str) -> datetime:
    return datetime.fromisoformat(value).replace(tzinfo=UTC)


def _seed_entry(
    session: Session,
    *,
    name: str,
    clinic_id: uuid.UUID,
    patient_id: uuid.UUID,
    author_id: uuid.UUID,
    section: str,
    origin: str,
    entry_type: str,
    title: str,
    contents: list[str],
    occurred_at: datetime,
    patient_facing: bool,
    source_job_id: uuid.UUID | None = None,
) -> tuple[Entry, EntryVersion]:
    entry_id = demo_id(f"entry-{name}")
    existing = session.get(Entry, entry_id)
    if existing is not None:
        if source_job_id is not None and existing.source_job_id is None:
            existing.source_job_id = source_job_id
            session.add(existing)
        assert existing.current_version_id is not None
        existing_version = session.get(EntryVersion, existing.current_version_id)
        assert existing_version is not None
        return existing, existing_version
    entry = Entry(
        id=entry_id,
        clinic_id=clinic_id,
        patient_id=patient_id,
        section=section,
        origin=origin,
        entry_type=entry_type,
        patient_facing=patient_facing,
        source_job_id=source_job_id,
        occurred_at=occurred_at,
        created_at=occurred_at,
    )
    session.add(entry)
    session.flush()
    current: EntryVersion | None = None
    for index, content in enumerate(contents, start=1):
        version_id = demo_id(f"entry-{name}-version-{index}")
        current = EntryVersion(
            id=version_id,
            clinic_id=clinic_id,
            entry_id=entry.id,
            version_no=index,
            title_ciphertext=field_codec.encrypt_text(
                clinic_id, "entry_version.title", version_id, title
            ),
            content_ciphertext=field_codec.encrypt_text(
                clinic_id, "entry_version.content", version_id, content
            ),
            content_sha256=hashlib.sha256(content.encode()).hexdigest(),
            patient_facing=patient_facing,
            author_id=author_id,
            created_at=occurred_at + timedelta(minutes=index - 1),
        )
        session.add(current)
    assert current is not None
    session.flush()
    entry.current_version_id = current.id
    session.add(entry)
    return entry, current


def _seed_highlight(
    session: Session,
    *,
    name: str,
    clinic_id: uuid.UUID,
    patient_id: uuid.UUID,
    entry: Entry,
    version: EntryVersion,
    content: str,
    quote: str,
    label: str,
    created_by_id: uuid.UUID,
    occurred_at: datetime,
    base_score: float,
    final_score: float,
    risk_reason: str,
    pinned: bool = False,
    critical: bool = False,
    unresolved: bool = False,
    clinician_confirmed: bool = True,
    support_state: str = "human_confirmed",
    risk_floor: str = "standard",
    risk_rule_ids: list[str] | None = None,
    abstained: bool = False,
    abstention_reason: str | None = None,
    feature_keys: list[str] | None = None,
) -> tuple[Highlight, ProvenancePointer]:
    """Create a deterministic, exact-source highlight and decision record."""

    highlight_id = demo_id(f"highlight-{name}")
    pointer_id = demo_id(f"provenance-{name}")
    start = content.index(quote)
    end = start + len(quote)
    highlight = session.get(Highlight, highlight_id)
    semantic_feature_keys = [
        *(feature_keys or []),
        *(["risk:critical"] if critical else []),
    ]
    if highlight is None:
        highlight = Highlight(
            id=highlight_id,
            clinic_id=clinic_id,
            patient_id=patient_id,
            entry_id=entry.id,
            source_entry_version_id=version.id,
            label_ciphertext=field_codec.encrypt_text(
                clinic_id, "highlight.label", highlight_id, label
            ),
            status="accepted",
            pinned=pinned,
            critical=critical,
            patient_facing=False,
            # Keep deterministic fixtures inside the same bounded semantic
            # taxonomy used by runtime learning. Free-form ``condition:*``
            # tokens are deliberately rejected by the learning service.
            feature_keys_json=semantic_feature_keys,
            base_score=base_score,
            final_score=final_score,
            risk_reason=risk_reason,
            unresolved=unresolved,
            clinician_confirmed=clinician_confirmed,
            created_by_id=created_by_id,
            created_at=occurred_at,
        )
        session.add(highlight)
        session.flush()
    elif semantic_feature_keys and highlight.feature_keys_json != semantic_feature_keys:
        # Repair only stable fixture rows in older development databases.
        # User-created highlights never share these deterministic ids.
        highlight.feature_keys_json = semantic_feature_keys
        session.add(highlight)
    pointer = session.get(ProvenancePointer, pointer_id)
    if pointer is None:
        pointer = ProvenancePointer(
            id=pointer_id,
            clinic_id=clinic_id,
            highlight_id=highlight.id,
            entry_version_id=version.id,
            start_offset=start,
            end_offset=end,
            exact_quote_ciphertext=field_codec.encrypt_text(
                clinic_id, "provenance.exact_quote", pointer_id, quote
            ),
            prefix_ciphertext=field_codec.encrypt_text(
                clinic_id,
                "provenance.prefix",
                pointer_id,
                content[max(0, start - 40) : start],
            ),
            suffix_ciphertext=field_codec.encrypt_text(
                clinic_id,
                "provenance.suffix",
                pointer_id,
                content[end : end + 40],
            ),
            quote_sha256=hashlib.sha256(quote.encode()).hexdigest(),
        )
        session.add(pointer)
        session.flush()
    assessment_id = demo_id(f"decision-assessment-{name}")
    if session.get(DecisionAssessment, assessment_id) is None:
        session.add(
            DecisionAssessment(
                id=assessment_id,
                clinic_id=clinic_id,
                highlight_id=highlight.id,
                output_type="extracted_fact",
                support_state=support_state,
                risk_tier=risk_floor,
                deterministic_floor=risk_floor,
                effective_risk=risk_floor,
                risk_rule_ids_json=risk_rule_ids or [],
                confidence_band="not_applicable",
                calibration_version="clinician-confirmed-v1",
                abstained=abstained,
                abstention_reason=abstention_reason,
                created_at=occurred_at,
            )
        )
    return highlight, pointer


def _seed_fact_assertion(
    session: Session,
    *,
    name: str,
    clinic_id: uuid.UUID,
    patient_id: uuid.UUID,
    entry: Entry,
    version: EntryVersion,
    pointer: ProvenancePointer,
    fact_type: str,
    subject: str,
    value: str,
    origin: str,
    effective_time: datetime,
    highlight_id: uuid.UUID | None = None,
) -> ClinicalFactAssertion:
    assertion_id = demo_id(f"clinical-fact-assertion-{name}")
    assertion = session.get(ClinicalFactAssertion, assertion_id)
    if assertion is not None:
        return assertion
    normalized_key = f"{fact_type}:{subject}:{value}:present:active".lower()
    assertion = ClinicalFactAssertion(
        id=assertion_id,
        clinic_id=clinic_id,
        patient_id=patient_id,
        entry_id=entry.id,
        source_entry_version_id=version.id,
        provenance_pointer_id=pointer.id,
        highlight_id=highlight_id,
        fact_type=fact_type,
        subject_ciphertext=field_codec.encrypt_text(
            clinic_id, "fact_assertion.subject", assertion_id, subject
        ),
        normalized_value_ciphertext=field_codec.encrypt_text(
            clinic_id, "fact_assertion.normalized_value", assertion_id, value
        ),
        normalized_key_hash=hashlib.sha256(normalized_key.encode()).hexdigest(),
        polarity="present",
        clinical_status="active",
        effective_time=effective_time,
        origin=origin,
        created_at=effective_time,
    )
    session.add(assertion)
    session.flush()
    return assertion


def _seed_longitudinal_complex_case(session: Session) -> None:
    """Build a source-linked, multi-role record spanning early adulthood to now."""

    clinic_id = demo_id("clinic-primary")
    patient_id = demo_id("patient-decay")
    staff_id = demo_id("user-staff")
    clinician_id = demo_id("user-clinician")
    worker_id = demo_id("user-worker")

    _seed_patient_identity(
        session,
        clinic_id=clinic_id,
        patient_id=patient_id,
        creator_membership_id=demo_id("membership-staff"),
        display_name="Jordan Wong",
        date_of_birth="1984-08-19",
        medical_record_number="MRN2004017",
        identity_number="SYNTHETIC017",
    )

    obesity_entry, obesity_version = _seed_entry(
        session,
        name="jordan-metabolic-risk-2004",
        clinic_id=clinic_id,
        patient_id=patient_id,
        author_id=staff_id,
        section="staff",
        origin="human",
        entry_type="manual_staff_note",
        title="Early metabolic risk review",
        contents=[
            "Obesity and a strong family history of type 2 diabetes were documented. "
            "The care team agreed on annual metabolic screening and supported weight-management follow-up."
        ],
        occurred_at=_fixture_time("2004-07-12T09:00:00"),
        patient_facing=True,
    )
    diabetes_entry, diabetes_version = _seed_entry(
        session,
        name="jordan-diabetes-diagnosis-2012",
        clinic_id=clinic_id,
        patient_id=patient_id,
        author_id=clinician_id,
        section="clinician",
        origin="human",
        entry_type="manual_clinician_note",
        title="Type 2 diabetes diagnosis",
        contents=[
            "Type 2 diabetes was confirmed after repeat testing. Started metformin. "
            "Metformin 500 mg oral twice daily was documented with glucose-monitoring education."
        ],
        occurred_at=_fixture_time("2012-03-22T11:20:00"),
        patient_facing=True,
    )
    pancreatitis_entry, pancreatitis_version = _seed_entry(
        session,
        name="jordan-pancreatitis-history-2018",
        clinic_id=clinic_id,
        patient_id=patient_id,
        author_id=clinician_id,
        section="clinician",
        origin="human",
        entry_type="manual_clinician_note",
        title="First acute pancreatitis admission",
        contents=[
            "Acute pancreatitis was diagnosed and treated by the inpatient team. "
            "The discharge plan documented recurrence warning signs and follow-up with gastroenterology."
        ],
        occurred_at=_fixture_time("2018-09-03T16:10:00"),
        patient_facing=True,
    )
    _seed_entry(
        session,
        name="jordan-weight-review-2021",
        clinic_id=clinic_id,
        patient_id=patient_id,
        author_id=staff_id,
        section="staff",
        origin="human",
        entry_type="manual_staff_note",
        title="Weight and diabetes follow-up",
        contents=[
            "Weight-management goals were reviewed alongside diabetes self-management. "
            "The patient preferred small diet changes and walking after meals."
        ],
        occurred_at=_fixture_time("2021-06-18T10:00:00"),
        patient_facing=True,
    )
    hydration_content = (
        "Diabetes sick-day plan reviewed. Maintain oral hydration with sugar-free "
        "fluids while unwell unless another treating team restricts oral intake. "
        "Seek urgent review for persistent vomiting or inability to keep fluids down."
    )
    hydration_entry, hydration_version = _seed_entry(
        session,
        name="jordan-diabetes-sick-day-plan-2025",
        clinic_id=clinic_id,
        patient_id=patient_id,
        author_id=clinician_id,
        section="clinician",
        origin="human",
        entry_type="manual_clinician_note",
        title="Diabetes sick-day plan",
        contents=[hydration_content],
        occurred_at=_fixture_time("2025-04-15T09:15:00"),
        patient_facing=True,
    )

    acute_versions = [
        "Admit for recurrent acute pancreatitis. Keep nil by mouth pending acute review and continue glucose monitoring.",
        "Admit for recurrent acute pancreatitis. Temporarily restrict oral intake while vomiting persists and continue bedside glucose monitoring.",
        "Current plan: temporarily no oral intake while vomiting persists; reassess oral intake after the acute-care review. "
        "Continue bedside glucose monitoring every four hours while oral intake is restricted. "
        "The earlier diabetes sick-day hydration plan is not to be followed concurrently until reviewed.",
    ]
    acute_entry, acute_version = _seed_entry(
        session,
        name="jordan-acute-plan-2026",
        clinic_id=clinic_id,
        patient_id=patient_id,
        author_id=clinician_id,
        section="clinician",
        origin="human",
        entry_type="manual_clinician_note",
        title="Current pancreatitis admission plan",
        contents=acute_versions,
        occurred_at=_fixture_time("2026-02-06T08:30:00"),
        patient_facing=False,
    )
    nurse_content = (
        "Patient requested water and referred to the earlier diabetes sick-day plan. "
        "The current acute-care instruction restricts oral intake. Continue bedside "
        "glucose monitoring every four hours while oral intake is restricted. The "
        "difference between the two plans was escalated to the acute-care clinician."
    )
    nurse_entry, nurse_version = _seed_entry(
        session,
        name="jordan-nursing-escalation-2026",
        clinic_id=clinic_id,
        patient_id=patient_id,
        author_id=staff_id,
        section="staff",
        origin="human",
        entry_type="manual_staff_note",
        title="Nursing escalation: oral-intake instructions",
        contents=[
            "Patient requested water and referred to an earlier diabetes plan. Clarification requested from the acute-care clinician.",
            nurse_content,
        ],
        occurred_at=_fixture_time("2026-02-06T09:10:00"),
        patient_facing=False,
    )

    ai_specs = (
        (
            "jordan-ai-doctor-2026",
            "ai_doctor_consult_summary",
            "doctor_consult",
            "AI-assisted multidisciplinary review",
            "AI-assisted review extracted that the current acute-care instruction is temporary no oral intake pending reassessment. "
            "This conflicts with the earlier diabetes sick-day hydration plan. Clinician confirmation is required before use.",
            acute_version,
        ),
        (
            "jordan-ai-nurse-2026",
            "ai_nurse_consult_summary",
            "care_note",
            "AI-assisted nursing handover",
            "AI-assisted nursing draft: vomiting continues, oral intake remains restricted, bedside glucose checks remain due, "
            "and the hydration discrepancy has been escalated to the acute-care clinician.",
            nurse_version,
        ),
        (
            "jordan-ai-patient-2026",
            "ai_patient_session_summary",
            "patient_insight",
            "AI-assisted patient account",
            "AI-assisted patient-session draft: the patient remembers being advised to drink sugar-free fluids when unwell "
            "and is unsure whether that advice still applies during this admission. Clinician review is required.",
            hydration_version,
        ),
    )
    ai_outputs: dict[str, tuple[Entry, EntryVersion, str]] = {}
    for name, entry_type, interaction_type, title, content, source_version in ai_specs:
        output_time = {
            "ai_doctor_consult_summary": _fixture_time("2026-02-06T09:20:00"),
            "ai_patient_session_summary": _fixture_time("2026-02-06T09:21:00"),
            "ai_nurse_consult_summary": _fixture_time("2026-02-06T09:22:00"),
        }[entry_type]
        job_id = demo_id(f"job-{name}")
        request_sha256 = hashlib.sha256(f"longitudinal:{name}".encode()).hexdigest()
        if session.get(Job, job_id) is None:
            session.add(
                Job(
                    id=job_id,
                    clinic_id=clinic_id,
                    patient_id=patient_id,
                    kind="ai_analyze",
                    state="completed",
                    idempotency_key=f"longitudinal:{name}:v1",
                    request_sha256=request_sha256,
                    payload_ciphertext=field_codec.encrypt_json(
                        clinic_id,
                        "job.payload",
                        job_id,
                        {"interaction_type": interaction_type},
                    ),
                    created_by_id=clinician_id,
                    created_at=_fixture_time("2026-02-06T09:18:00"),
                    updated_at=_fixture_time("2026-02-06T09:20:00"),
                )
            )
            session.flush()
        output_entry, output_version = _seed_entry(
            session,
            name=name,
            clinic_id=clinic_id,
            patient_id=patient_id,
            author_id=worker_id,
            section="system",
            origin="ai",
            entry_type=entry_type,
            title=title,
            contents=[content],
            occurred_at=output_time,
            patient_facing=False,
            source_job_id=job_id,
        )
        ai_outputs[entry_type] = (output_entry, output_version, content)
        redaction_id = demo_id(f"redaction-{name}")
        if session.get(RedactionRun, redaction_id) is None:
            session.add(
                RedactionRun(
                    id=redaction_id,
                    clinic_id=clinic_id,
                    source_entry_version_id=source_version.id,
                    status="completed",
                    input_sha256=source_version.content_sha256,
                    redacted_sha256=source_version.content_sha256,
                    entity_counts_json={},
                    map_ciphertext=field_codec.encrypt_json(
                        clinic_id, "redaction.map", redaction_id, {}
                    ),
                    residual_scan_passed=True,
                    created_at=_fixture_time("2026-02-06T09:19:00"),
                )
            )
            session.flush()
        run_id = demo_id(f"ai-run-{name}")
        if session.get(AIRun, run_id) is None:
            session.add(
                AIRun(
                    id=run_id,
                    clinic_id=clinic_id,
                    patient_id=patient_id,
                    job_id=job_id,
                    redaction_run_id=redaction_id,
                    source_entry_version_id=source_version.id,
                    executed_by_worker_membership_id=demo_id("membership-worker"),
                    interaction_type=interaction_type,
                    provider="deterministic_fixture",
                    model="deterministic-fixture-v1",
                    review_status="needs_review",
                    primary_output_ciphertext=field_codec.encrypt_json(
                        clinic_id,
                        "ai_run.primary_output",
                        run_id,
                        {"entry_type": entry_type},
                    ),
                    status="completed",
                    needs_review=True,
                    request_sha256=request_sha256,
                    output_entry_id=output_entry.id,
                    output_entry_version_id=output_version.id,
                    warnings_json=["CLINICAL_REVIEW_REQUIRED"],
                    created_at=_fixture_time("2026-02-06T09:20:00"),
                )
            )

    current_quote = (
        "Current plan: temporarily no oral intake while vomiting persists; "
        "reassess oral intake after the acute-care review."
    )
    current_highlight, current_pointer = _seed_highlight(
        session,
        name="jordan-current-acute-plan",
        clinic_id=clinic_id,
        patient_id=patient_id,
        entry=acute_entry,
        version=acute_version,
        content=acute_versions[-1],
        quote=current_quote,
        label="Acute pancreatitis: temporary oral-intake restriction pending reassessment",
        created_by_id=clinician_id,
        occurred_at=_fixture_time("2026-02-06T09:25:00"),
        base_score=0.98,
        final_score=1.0,
        risk_reason="clinician_accepted",
        pinned=True,
        risk_floor="high",
        risk_rule_ids=["ACTIVE_ACUTE_CARE_PLAN"],
        feature_keys=[
            "entity:diagnosis",
            "topic:follow_up",
            "entry_type:manual_clinician_note",
        ],
    )
    monitoring_quote = "Continue bedside glucose monitoring every four hours while oral intake is restricted."
    monitoring_highlight, monitoring_pointer = _seed_highlight(
        session,
        name="jordan-diabetes-monitoring",
        clinic_id=clinic_id,
        patient_id=patient_id,
        entry=nurse_entry,
        version=nurse_version,
        content=nurse_content,
        quote=monitoring_quote,
        label="Type 2 diabetes: four-hourly glucose checks remain due during intake restriction",
        created_by_id=clinician_id,
        occurred_at=_fixture_time("2026-02-06T09:26:00"),
        base_score=0.93,
        final_score=0.96,
        risk_reason="clinician_accepted",
        risk_floor="high",
        risk_rule_ids=["DIABETES_DURING_INTAKE_RESTRICTION"],
        feature_keys=[
            "entity:diagnosis",
            "entity:medication",
            "topic:follow_up",
            "entry_type:manual_staff_note",
        ],
    )
    ai_nurse_entry, ai_nurse_version, ai_nurse_content = ai_outputs[
        "ai_nurse_consult_summary"
    ]
    ai_quote = (
        "the hydration discrepancy has been escalated to the acute-care clinician"
    )
    ai_highlight, ai_pointer = _seed_highlight(
        session,
        name="jordan-ai-escalation",
        clinic_id=clinic_id,
        patient_id=patient_id,
        entry=ai_nurse_entry,
        version=ai_nurse_version,
        content=ai_nurse_content,
        quote=ai_quote,
        label="AI-scribed handover: hydration-plan discrepancy escalated for clinician review",
        created_by_id=clinician_id,
        occurred_at=_fixture_time("2026-02-06T09:27:00"),
        base_score=0.88,
        final_score=0.92,
        risk_reason="clinician_accepted",
        pinned=True,
        risk_floor="high",
        risk_rule_ids=["CLINICIAN_CONFIRMED_AI_EXTRACTION"],
        feature_keys=[
            "topic:follow_up",
            "entry_type:ai_nurse_consult_summary",
        ],
    )
    history_quote = "Acute pancreatitis was diagnosed"
    history_highlight, history_pointer = _seed_highlight(
        session,
        name="jordan-pancreatitis-history",
        clinic_id=clinic_id,
        patient_id=patient_id,
        entry=pancreatitis_entry,
        version=pancreatitis_version,
        content=(
            "Acute pancreatitis was diagnosed and treated by the inpatient team. "
            "The discharge plan documented recurrence warning signs and follow-up with gastroenterology."
        ),
        quote=history_quote,
        label="Recurrent pancreatitis history is relevant to the current admission",
        created_by_id=clinician_id,
        occurred_at=_fixture_time("2026-02-06T09:28:00"),
        base_score=0.72,
        final_score=0.82,
        risk_reason="clinician_accepted",
        risk_floor="elevated",
        risk_rule_ids=["RELEVANT_LONGITUDINAL_HISTORY"],
        feature_keys=[
            "entity:diagnosis",
            "entry_type:manual_clinician_note",
        ],
    )
    diabetes_quote = "Type 2 diabetes was confirmed"
    diabetes_highlight, diabetes_pointer = _seed_highlight(
        session,
        name="jordan-diabetes-history",
        clinic_id=clinic_id,
        patient_id=patient_id,
        entry=diabetes_entry,
        version=diabetes_version,
        content=(
            "Type 2 diabetes was confirmed after repeat testing. Started metformin. "
            "Metformin 500 mg oral twice daily was documented with glucose-monitoring education."
        ),
        quote=diabetes_quote,
        label="Type 2 diabetes has been documented since 2012",
        created_by_id=clinician_id,
        occurred_at=_fixture_time("2026-02-06T09:28:30"),
        base_score=0.62,
        final_score=0.74,
        risk_reason="clinician_accepted",
        risk_floor="elevated",
        risk_rule_ids=["LONGITUDINAL_DIABETES_HISTORY"],
        feature_keys=[
            "entity:diagnosis",
            "entity:medication",
            "entry_type:manual_clinician_note",
        ],
    )
    obesity_quote = (
        "Obesity and a strong family history of type 2 diabetes were documented"
    )
    obesity_highlight, obesity_pointer = _seed_highlight(
        session,
        name="jordan-metabolic-history",
        clinic_id=clinic_id,
        patient_id=patient_id,
        entry=obesity_entry,
        version=obesity_version,
        content=(
            "Obesity and a strong family history of type 2 diabetes were documented. "
            "The care team agreed on annual metabolic screening and supported weight-management follow-up."
        ),
        quote=obesity_quote,
        label="Longstanding obesity and metabolic risk remain relevant context",
        created_by_id=clinician_id,
        occurred_at=_fixture_time("2026-02-06T09:29:00"),
        base_score=0.58,
        final_score=0.7,
        risk_reason="recency",
        risk_floor="standard",
        risk_rule_ids=["LONGITUDINAL_CONTEXT"],
        feature_keys=[
            "entity:diagnosis",
            "entry_type:manual_staff_note",
        ],
    )
    hydration_quote = "Maintain oral hydration with sugar-free fluids while unwell"
    hydration_highlight, hydration_pointer = _seed_highlight(
        session,
        name="jordan-prior-hydration-plan",
        clinic_id=clinic_id,
        patient_id=patient_id,
        entry=hydration_entry,
        version=hydration_version,
        content=hydration_content,
        quote=hydration_quote,
        label="Earlier diabetes sick-day plan recommended oral hydration when not restricted",
        created_by_id=clinician_id,
        occurred_at=_fixture_time("2026-02-06T09:30:00"),
        base_score=0.52,
        final_score=0.66,
        risk_reason="recency",
        risk_floor="standard",
        risk_rule_ids=["HISTORICAL_PLAN"],
        feature_keys=[
            "topic:follow_up",
            "entry_type:manual_clinician_note",
        ],
    )

    facts = (
        (
            "obesity",
            obesity_entry,
            obesity_version,
            obesity_pointer,
            obesity_highlight,
            "condition",
            "obesity",
            "longstanding",
            "human",
            _fixture_time("2004-07-12T09:00:00"),
        ),
        (
            "type-2-diabetes",
            diabetes_entry,
            diabetes_version,
            diabetes_pointer,
            diabetes_highlight,
            "condition",
            "type 2 diabetes",
            "active",
            "human",
            _fixture_time("2012-03-22T11:20:00"),
        ),
        (
            "pancreatitis",
            pancreatitis_entry,
            pancreatitis_version,
            history_pointer,
            history_highlight,
            "condition",
            "acute pancreatitis",
            "recurrent",
            "human",
            _fixture_time("2018-09-03T16:10:00"),
        ),
        (
            "prior-hydration-plan",
            hydration_entry,
            hydration_version,
            hydration_pointer,
            hydration_highlight,
            "care_plan",
            "oral intake",
            "oral hydration advised when not restricted",
            "human",
            _fixture_time("2025-04-15T09:15:00"),
        ),
        (
            "current-intake-restriction",
            acute_entry,
            acute_version,
            current_pointer,
            current_highlight,
            "care_plan",
            "oral intake",
            "temporarily restricted pending reassessment",
            "human",
            _fixture_time("2026-02-06T08:30:00"),
        ),
        (
            "current-glucose-monitoring",
            nurse_entry,
            nurse_version,
            monitoring_pointer,
            monitoring_highlight,
            "care_plan",
            "glucose monitoring",
            "every four hours during oral-intake restriction",
            "human",
            _fixture_time("2026-02-06T09:10:00"),
        ),
        (
            "ai-hydration-escalation",
            ai_nurse_entry,
            ai_nurse_version,
            ai_pointer,
            ai_highlight,
            "care_coordination",
            "hydration-plan discrepancy",
            "escalated to acute-care clinician",
            "ai",
            _fixture_time("2026-02-06T09:20:00"),
        ),
    )
    assertions: dict[str, ClinicalFactAssertion] = {}
    for (
        name,
        entry,
        version,
        pointer,
        highlight,
        fact_type,
        subject,
        value,
        origin,
        effective_time,
    ) in facts:
        assertions[name] = _seed_fact_assertion(
            session,
            name=f"jordan-{name}",
            clinic_id=clinic_id,
            patient_id=patient_id,
            entry=entry,
            version=version,
            pointer=pointer,
            fact_type=fact_type,
            subject=subject,
            value=value,
            origin=origin,
            effective_time=effective_time,
            highlight_id=highlight.id,
        )
        assessment = session.exec(
            select(DecisionAssessment).where(
                DecisionAssessment.clinic_id == clinic_id,
                DecisionAssessment.highlight_id == highlight.id,
            )
        ).first()
        if assessment is not None and assessment.assertion_id is None:
            assessment.assertion_id = assertions[name].id
            session.add(assessment)

    ai_doctor_entry, ai_doctor_version, ai_doctor_content = ai_outputs[
        "ai_doctor_consult_summary"
    ]
    conflict_quote = "This conflicts with the earlier diabetes sick-day hydration plan"
    conflict_highlight, _ = _seed_highlight(
        session,
        name="jordan-oral-intake-conflict",
        clinic_id=clinic_id,
        patient_id=patient_id,
        entry=ai_doctor_entry,
        version=ai_doctor_version,
        content=ai_doctor_content,
        quote=conflict_quote,
        label="Conflicting oral-intake instructions require clinician resolution",
        created_by_id=clinician_id,
        occurred_at=_fixture_time("2026-02-06T09:31:00"),
        base_score=0.99,
        final_score=1.0,
        risk_reason="care_plan_conflict",
        unresolved=True,
        clinician_confirmed=False,
        support_state="supported",
        risk_floor="high",
        risk_rule_ids=["CARE_PLAN_CONFLICT"],
        abstained=True,
        abstention_reason="UNRESOLVED_HIGH_RISK_CONFLICT",
        feature_keys=[
            "topic:follow_up",
            "entry_type:ai_doctor_consult_summary",
        ],
    )
    conflict_id = demo_id("conflict-jordan-oral-intake-plan")
    if session.get(ConflictCase, conflict_id) is None:
        session.add(
            ConflictCase(
                id=conflict_id,
                clinic_id=clinic_id,
                patient_id=patient_id,
                left_entry_id=hydration_entry.id,
                right_entry_id=acute_entry.id,
                fact_type="care_plan",
                normalized_key="oral intake",
                left_version_id=hydration_version.id,
                right_version_id=acute_version.id,
                left_pointer_id=hydration_pointer.id,
                right_pointer_id=current_pointer.id,
                left_assertion_id=assertions["prior-hydration-plan"].id,
                right_assertion_id=assertions["current-intake-restriction"].id,
                severity="high",
                status="unresolved",
                created_at=_fixture_time("2026-02-06T09:31:00"),
            )
        )

    comment_id = demo_id("comment-jordan-hydration-conflict")
    if session.get(Comment, comment_id) is None:
        comment_quote = "hydration discrepancy"
        comment_start = ai_nurse_content.index(comment_quote)
        session.add(
            Comment(
                id=comment_id,
                clinic_id=clinic_id,
                entry_id=ai_nurse_entry.id,
                entry_version_id=ai_nurse_version.id,
                author_id=staff_id,
                body_ciphertext=field_codec.encrypt_text(
                    clinic_id,
                    "comment.body",
                    comment_id,
                    "@clinician Please reconcile the current oral-intake restriction with the earlier diabetes sick-day plan before handover.",
                ),
                start_offset=comment_start,
                end_offset=comment_start + len(comment_quote),
                exact_quote_ciphertext=field_codec.encrypt_text(
                    clinic_id,
                    "comment.exact_quote",
                    comment_id,
                    comment_quote,
                ),
                prefix_ciphertext=field_codec.encrypt_text(
                    clinic_id,
                    "comment.prefix",
                    comment_id,
                    ai_nurse_content[max(0, comment_start - 40) : comment_start],
                ),
                suffix_ciphertext=field_codec.encrypt_text(
                    clinic_id,
                    "comment.suffix",
                    comment_id,
                    ai_nurse_content[
                        comment_start + len(comment_quote) : comment_start
                        + len(comment_quote)
                        + 40
                    ],
                ),
                quote_sha256=hashlib.sha256(comment_quote.encode()).hexdigest(),
                assigned_membership_id=demo_id("membership-clinician"),
                review_required=True,
                created_at=_fixture_time("2026-02-06T09:35:00"),
            )
        )
        session.flush()
        session.add(
            CommentMention(
                id=demo_id("comment-mention-jordan-clinician"),
                clinic_id=clinic_id,
                comment_id=comment_id,
                mentioned_user_id=clinician_id,
                created_at=_fixture_time("2026-02-06T09:35:00"),
            )
        )
        session.add(
            CareTask(
                id=demo_id("task-jordan-hydration-review"),
                clinic_id=clinic_id,
                patient_id=patient_id,
                comment_id=comment_id,
                assignee_membership_id=demo_id("membership-clinician"),
                title_ciphertext=field_codec.encrypt_text(
                    clinic_id,
                    "care_task.title",
                    demo_id("task-jordan-hydration-review"),
                    "Resolve conflicting oral-intake instructions",
                ),
                status="open",
                created_at=_fixture_time("2026-02-06T09:35:00"),
            )
        )

    for action, resource_type, resource_id, actor_id, timestamp in (
        (
            "entry.versioned",
            "entry",
            acute_entry.id,
            clinician_id,
            _fixture_time("2026-02-06T08:34:00"),
        ),
        (
            "comment.mentioned",
            "comment",
            comment_id,
            staff_id,
            _fixture_time("2026-02-06T09:35:00"),
        ),
        (
            "conflict.detected",
            "conflict",
            conflict_id,
            clinician_id,
            _fixture_time("2026-02-06T09:31:00"),
        ),
    ):
        audit_id = demo_id(f"audit-jordan-{action}-{resource_id}")
        if session.get(AuditEvent, audit_id) is None:
            session.add(
                AuditEvent(
                    id=audit_id,
                    clinic_id=clinic_id,
                    actor_id=actor_id,
                    action=action,
                    resource_type=resource_type,
                    resource_id=resource_id,
                    metadata_json={"patient_id": str(patient_id)},
                    created_at=timestamp,
                )
            )

    session.flush()
    from app.api.deps import RequestContext
    from app.services.nightingale import rebuild_glance

    clinician = session.get(User, clinician_id)
    clinician_membership = session.get(
        ClinicMembership, demo_id("membership-clinician")
    )
    assert clinician is not None and clinician_membership is not None
    rebuild_glance(
        session,
        RequestContext(user=clinician, membership=clinician_membership),
        patient_id,
    )


def _seed_demo_domain(session: Session) -> None:
    """Add deterministic, synthetic Scenario A-E fixtures without real PHI."""

    clinic_id = demo_id("clinic-primary")
    patient_id = demo_id("patient-primary")
    decay_patient_id = demo_id("patient-decay")
    staff_id = demo_id("user-staff")
    clinician_id = demo_id("user-clinician")

    if session.get(Patient, decay_patient_id) is None:
        session.add(
            Patient(
                id=decay_patient_id,
                clinic_id=clinic_id,
                display_name_ciphertext=field_codec.encrypt_text(
                    clinic_id,
                    "patient.display_name",
                    decay_patient_id,
                    "Jordan Wong",
                ),
                external_ref_hash=hashlib.sha256(b"SYNTHETIC-DECAY-001").hexdigest(),
                created_at=_fixture_time("2023-01-10T09:00:00"),
            )
        )
        snapshot_id = demo_id("glance-decay")
        session.add(
            PatientGlanceSnapshot(
                id=snapshot_id,
                clinic_id=clinic_id,
                patient_id=decay_patient_id,
                payload_ciphertext=field_codec.encrypt_json(
                    clinic_id, "glance.payload", snapshot_id, {"cards": []}
                ),
                generated_at=_fixture_time("2023-01-10T09:00:00"),
            )
        )
        session.flush()

    _seed_entry(
        session,
        name="history-2025-04-15",
        clinic_id=clinic_id,
        patient_id=patient_id,
        author_id=staff_id,
        section="staff",
        origin="human",
        entry_type="manual_staff_note",
        title="Medication reconciliation",
        contents=[
            "Medication list reviewed during the home visit.",
            "Medication list reviewed; duplicate evening dose removed after review.",
        ],
        occurred_at=_fixture_time("2025-04-15T09:30:00"),
        patient_facing=True,
    )
    current_entry, current_version = _seed_entry(
        session,
        name="current-2026-02-06",
        clinic_id=clinic_id,
        patient_id=patient_id,
        author_id=clinician_id,
        section="clinician",
        origin="human",
        entry_type="manual_clinician_note",
        title="Current care review",
        contents=[
            "Medication reconciliation completed. Fall risk remains elevated. "
            "Follow-up blood pressure check due Friday. Daughter reports improved sleep."
        ],
        occurred_at=_fixture_time("2026-02-06T14:00:00"),
        patient_facing=True,
    )
    ai_outputs: dict[str, tuple[Entry, EntryVersion, str]] = {}
    for entry_type, interaction_type, title, content in (
        (
            "ai_doctor_consult_summary",
            "doctor_consult",
            "AI doctor consult summary",
            "Consult summary draft: medication reconciliation completed; "
            "clinician review is required.",
        ),
        (
            "ai_nurse_consult_summary",
            "care_note",
            "AI nurse consult summary",
            "Nursing care draft: continue fall-risk monitoring and complete the "
            "Friday blood pressure follow-up; clinician review is required.",
        ),
        (
            "ai_patient_session_summary",
            "patient_insight",
            "AI patient session summary",
            "Patient session draft: sleep has improved according to family; "
            "clinician review is required.",
        ),
    ):
        job_id = demo_id(f"job-{entry_type}")
        request_sha256 = hashlib.sha256(f"fixture:{entry_type}".encode()).hexdigest()
        if session.get(Job, job_id) is None:
            session.add(
                Job(
                    id=job_id,
                    clinic_id=clinic_id,
                    patient_id=patient_id,
                    kind="ai_analyze",
                    state="completed",
                    idempotency_key=f"fixture:{entry_type}:v1",
                    request_sha256=request_sha256,
                    payload_ciphertext=field_codec.encrypt_json(
                        clinic_id,
                        "job.payload",
                        job_id,
                        {"interaction_type": interaction_type, "synthetic": True},
                    ),
                    created_by_id=demo_id("user-clinician"),
                    created_at=_fixture_time("2026-02-06T14:04:00"),
                    updated_at=_fixture_time("2026-02-06T14:05:00"),
                )
            )
            session.flush()
        output_entry, output_version = _seed_entry(
            session,
            name=entry_type,
            clinic_id=clinic_id,
            patient_id=patient_id,
            author_id=demo_id("user-worker"),
            section="system",
            origin="ai",
            entry_type=entry_type,
            title=title,
            contents=[content],
            occurred_at=_fixture_time("2026-02-06T14:05:00"),
            patient_facing=False,
            source_job_id=job_id,
        )
        ai_outputs[entry_type] = (output_entry, output_version, content)
        redaction_id = demo_id(f"redaction-{entry_type}")
        if session.get(RedactionRun, redaction_id) is None:
            source_hash = current_version.content_sha256
            session.add(
                RedactionRun(
                    id=redaction_id,
                    clinic_id=clinic_id,
                    source_entry_version_id=current_version.id,
                    status="completed",
                    input_sha256=source_hash,
                    redacted_sha256=source_hash,
                    entity_counts_json={},
                    map_ciphertext=field_codec.encrypt_json(
                        clinic_id, "redaction.map", redaction_id, {}
                    ),
                    residual_scan_passed=True,
                    created_at=_fixture_time("2026-02-06T14:04:30"),
                )
            )
            session.flush()
        run_id = demo_id(f"ai-run-{entry_type}")
        if session.get(AIRun, run_id) is None:
            session.add(
                AIRun(
                    id=run_id,
                    clinic_id=clinic_id,
                    patient_id=patient_id,
                    job_id=job_id,
                    redaction_run_id=redaction_id,
                    source_entry_version_id=current_version.id,
                    executed_by_worker_membership_id=demo_id("membership-worker"),
                    interaction_type=interaction_type,
                    provider="deterministic_fixture",
                    model="deterministic-fixture-v1",
                    review_status="fixture",
                    primary_output_ciphertext=field_codec.encrypt_json(
                        clinic_id,
                        "ai_run.primary_output",
                        run_id,
                        {"synthetic": True, "entry_type": entry_type},
                    ),
                    status="completed",
                    needs_review=True,
                    request_sha256=request_sha256,
                    output_entry_id=output_entry.id,
                    output_entry_version_id=output_version.id,
                    warnings_json=["SYNTHETIC_FIXTURE"],
                    created_at=_fixture_time("2026-02-06T14:05:00"),
                )
            )
    _seed_entry(
        session,
        name="decay-candidate-2023",
        clinic_id=clinic_id,
        patient_id=decay_patient_id,
        author_id=staff_id,
        section="staff",
        origin="human",
        entry_type="manual_staff_note",
        title="Unprotected historical observation",
        contents=["Resolved skin irritation; no recurrence reported at follow-up."],
        occurred_at=_fixture_time("2023-01-10T10:00:00"),
        patient_facing=False,
    )
    session.flush()

    manual_source = (
        "Medication reconciliation completed. Fall risk remains elevated. "
        "Follow-up blood pressure check due Friday. Daughter reports improved sleep."
    )
    ai_doctor_entry, ai_doctor_version, ai_doctor_source = ai_outputs[
        "ai_doctor_consult_summary"
    ]
    highlight_specs = (
        (
            "fall-risk",
            "Fall risk remains elevated",
            "Fall risk remains elevated",
            True,
            True,
            "critical",
            current_entry,
            current_version,
            manual_source,
        ),
        (
            "bp-followup",
            "Follow-up blood pressure check due Friday",
            "Follow-up blood pressure check due Friday",
            False,
            False,
            "follow_up_due",
            current_entry,
            current_version,
            manual_source,
        ),
        (
            "medication",
            "Medication reconciliation completed",
            "Medication reconciliation completed",
            False,
            True,
            "clinician_accepted",
            current_entry,
            current_version,
            manual_source,
        ),
        (
            "ai-doctor-review",
            "AI doctor draft requires clinician review",
            "clinician review is required",
            False,
            True,
            "ai_scribed_review_required",
            ai_doctor_entry,
            ai_doctor_version,
            ai_doctor_source,
        ),
    )
    for index, (
        name,
        label,
        quote,
        critical,
        pinned,
        reason,
        source_entry,
        source_version,
        source_content,
    ) in enumerate(highlight_specs):
        highlight_id = demo_id(f"highlight-{name}")
        pointer_id = demo_id(f"provenance-{name}")
        start = source_content.index(quote)
        end = start + len(quote)
        prefix = source_content[max(0, start - 16) : start]
        suffix = source_content[end : end + 16]
        feature_keys = {
            "fall-risk": ["risk:critical", "topic:follow_up"],
            "bp-followup": ["topic:follow_up"],
            "medication": ["entity:medication"],
            "ai-doctor-review": ["entry_type:ai_doctor_consult_summary"],
        }[name]
        existing_highlight = session.get(Highlight, highlight_id)
        if existing_highlight is None:
            session.add(
                Highlight(
                    id=highlight_id,
                    clinic_id=clinic_id,
                    patient_id=patient_id,
                    entry_id=source_entry.id,
                    source_entry_version_id=source_version.id,
                    label_ciphertext=field_codec.encrypt_text(
                        clinic_id, "highlight.label", highlight_id, label
                    ),
                    status="accepted",
                    pinned=pinned,
                    critical=critical,
                    patient_facing=source_entry.origin == "human" and index < 3,
                    feature_keys_json=feature_keys,
                    base_score=0.7 - index * 0.05,
                    learned_score=0.05 if index == 2 else 0.0,
                    final_score=1.0 - index * 0.1,
                    risk_reason=reason,
                    clinician_confirmed=index in {2, 3},
                    created_by_id=clinician_id,
                    created_at=_fixture_time("2026-02-06T14:10:00")
                    + timedelta(minutes=index),
                )
            )
        elif existing_highlight.feature_keys_json != feature_keys:
            existing_highlight.feature_keys_json = feature_keys
            session.add(existing_highlight)
        if session.get(ProvenancePointer, pointer_id) is None:
            session.add(
                ProvenancePointer(
                    id=pointer_id,
                    clinic_id=clinic_id,
                    highlight_id=highlight_id,
                    entry_version_id=source_version.id,
                    start_offset=start,
                    end_offset=end,
                    exact_quote_ciphertext=field_codec.encrypt_text(
                        clinic_id, "provenance.exact_quote", pointer_id, quote
                    ),
                    prefix_ciphertext=field_codec.encrypt_text(
                        clinic_id, "provenance.prefix", pointer_id, prefix
                    ),
                    suffix_ciphertext=field_codec.encrypt_text(
                        clinic_id, "provenance.suffix", pointer_id, suffix
                    ),
                    quote_sha256=hashlib.sha256(quote.encode()).hexdigest(),
                )
            )
    comment_id = demo_id("comment-clinician-assignment")
    quote = "Fall risk remains elevated"
    start = manual_source.index(quote)
    end = start + len(quote)
    if session.get(Comment, comment_id) is None:
        session.add(
            Comment(
                id=comment_id,
                clinic_id=clinic_id,
                entry_id=current_entry.id,
                entry_version_id=current_version.id,
                author_id=staff_id,
                body_ciphertext=field_codec.encrypt_text(
                    clinic_id,
                    "comment.body",
                    comment_id,
                    "@clinician Please review this fall-risk item.",
                ),
                start_offset=start,
                end_offset=end,
                exact_quote_ciphertext=field_codec.encrypt_text(
                    clinic_id, "comment.exact_quote", comment_id, quote
                ),
                prefix_ciphertext=field_codec.encrypt_text(
                    clinic_id,
                    "comment.prefix",
                    comment_id,
                    manual_source[max(0, start - 16) : start],
                ),
                suffix_ciphertext=field_codec.encrypt_text(
                    clinic_id,
                    "comment.suffix",
                    comment_id,
                    manual_source[end : end + 16],
                ),
                quote_sha256=hashlib.sha256(quote.encode()).hexdigest(),
                assigned_membership_id=demo_id("membership-clinician"),
            )
        )
        session.flush()
        session.add(
            CommentMention(
                id=demo_id("comment-mention-clinician"),
                clinic_id=clinic_id,
                comment_id=comment_id,
                mentioned_user_id=clinician_id,
            )
        )
        task_id = demo_id("task-fall-risk-review")
        session.add(
            CareTask(
                id=task_id,
                clinic_id=clinic_id,
                patient_id=patient_id,
                comment_id=comment_id,
                assignee_membership_id=demo_id("membership-clinician"),
                title_ciphertext=field_codec.encrypt_text(
                    clinic_id,
                    "care_task.title",
                    task_id,
                    "Review fall-risk evidence",
                ),
            )
        )

    audit_id = demo_id("audit-demo-fixture")
    if session.get(AuditEvent, audit_id) is None:
        session.add(
            AuditEvent(
                id=audit_id,
                clinic_id=clinic_id,
                actor_id=clinician_id,
                action="fixture.loaded",
                resource_type="entry",
                resource_id=current_entry.id,
                metadata_json={"version_id": str(current_version.id)},
            )
        )
    stat_id = demo_id("importance-stat-medication")
    stat = session.get(ImportanceFeatureStat, stat_id)
    if stat is None:
        session.add(
            ImportanceFeatureStat(
                id=stat_id,
                clinic_id=clinic_id,
                feature_key="entity:medication",
                weight=0.05,
                positive_count=1,
                observation_count=1,
            )
        )
    elif stat.feature_key == "fixture:medication":
        stat.feature_key = "entity:medication"
        session.add(stat)
    feedback_id = demo_id("importance-feedback-medication")
    feedback = session.get(ImportanceFeedbackEvent, feedback_id)
    if feedback is None:
        session.add(
            ImportanceFeedbackEvent(
                id=feedback_id,
                clinic_id=clinic_id,
                highlight_id=demo_id("highlight-medication"),
                actor_membership_id=demo_id("membership-clinician"),
                signal="accept",
                feature_keys_json=["entity:medication"],
                applied_delta=0.05,
                idempotency_key="seed:entity:medication:accept:v1",
                request_sha256=hashlib.sha256(b"fixture-medication-accept").hexdigest(),
            )
        )
    elif feedback.feature_keys_json == ["fixture:medication"]:
        feedback.feature_keys_json = ["entity:medication"]
        feedback.idempotency_key = "seed:entity:medication:accept:v1"
        session.add(feedback)

    # Re-seeding must never replay the original card payload over user feedback
    # or highlight state. Build the projection from the current rows instead;
    # on first seed this produces the four deterministic cards, and on restart
    # it preserves accept/reject/pin/learning changes without count growth.
    from app.api.deps import RequestContext
    from app.services.nightingale import rebuild_glance

    clinician = session.get(User, clinician_id)
    clinician_membership = session.get(
        ClinicMembership, demo_id("membership-clinician")
    )
    assert clinician is not None and clinician_membership is not None
    rebuild_glance(
        session,
        RequestContext(user=clinician, membership=clinician_membership),
        patient_id,
    )
    _seed_longitudinal_complex_case(session)


def _seed_other_clinic_examples(session: Session) -> None:
    """Seed a second tenant with realistic, source-linked patient records."""

    clinic_id = demo_id("clinic-other")
    clinic = session.get(Clinic, clinic_id)
    if clinic is None:
        return
    clinic.name = "Harbour Family Clinic"
    session.add(clinic)

    staff_id = demo_id("user-other_staff")
    staff_membership_id = demo_id("membership-other_staff")
    staff = session.get(User, staff_id)
    staff_membership = session.get(ClinicMembership, staff_membership_id)
    if staff is None or staff_membership is None:
        return

    clinician_membership_id = demo_id("membership-other-clinician")
    clinician = session.exec(
        select(User).where(User.email == OTHER_CLINICIAN_EMAIL)
    ).first()
    if clinician is None:
        clinician = User(
            id=demo_id("user-other-clinician"),
            email=OTHER_CLINICIAN_EMAIL,
            full_name="Dr Maya Chen",
            hashed_password=get_password_hash("synthetic-demo-only"),
        )
        session.add(clinician)
        session.flush()
    clinician_membership = session.get(ClinicMembership, clinician_membership_id)
    if clinician_membership is None:
        clinician_membership = ClinicMembership(
            id=clinician_membership_id,
            clinic_id=clinic_id,
            user_id=clinician.id,
            role="clinician",
        )
        session.add(clinician_membership)
        session.flush()

    patient_specs = (
        ("patient-other", "Taylor Lee", "1991-09-03", "MRN2026002", "SYNTHETIC002"),
        (
            "patient-other-priya",
            "Priya Nair",
            "1978-11-24",
            "HFC2024018",
            "SYNTHETIC018",
        ),
        (
            "patient-other-daniel",
            "Daniel Koh",
            "1967-05-08",
            "HFC2023029",
            "SYNTHETIC029",
        ),
    )
    for patient_key, display_name, dob, mrn, identity_number in patient_specs:
        patient_id = demo_id(patient_key)
        if session.get(Patient, patient_id) is None:
            session.add(
                Patient(
                    id=patient_id,
                    clinic_id=clinic_id,
                    display_name_ciphertext=field_codec.encrypt_text(
                        clinic_id, "patient.display_name", patient_id, display_name
                    ),
                    external_ref_hash=hashlib.sha256(
                        f"OTHER:{mrn}".encode()
                    ).hexdigest(),
                    created_by_membership_id=staff_membership_id,
                )
            )
            session.flush()
        _seed_patient_identity(
            session,
            clinic_id=clinic_id,
            patient_id=patient_id,
            creator_membership_id=staff_membership_id,
            display_name=display_name,
            date_of_birth=dob,
            medical_record_number=mrn,
            identity_number=identity_number,
        )

    taylor_id = demo_id("patient-other")
    _seed_entry(
        session,
        name="other-taylor-asthma-review-2021",
        clinic_id=clinic_id,
        patient_id=taylor_id,
        author_id=clinician.id,
        section="clinician",
        origin="human",
        entry_type="manual_clinician_note",
        title="Asthma and allergy review",
        contents=[
            "Mild persistent asthma and seasonal allergic rhinitis were reviewed. "
            "The patient was asked to continue the preventer inhaler and return if night-time symptoms increased."
        ],
        occurred_at=_fixture_time("2021-05-12T10:20:00"),
        patient_facing=True,
    )
    taylor_content = (
        "Asthma remains well controlled with no night-time symptoms and no urgent visits in the past year. "
        "Continue the current preventer inhaler and review inhaler technique at the next visit."
    )
    taylor_entry, taylor_version = _seed_entry(
        session,
        name="other-taylor-asthma-review-2026",
        clinic_id=clinic_id,
        patient_id=taylor_id,
        author_id=staff_id,
        section="staff",
        origin="human",
        entry_type="manual_staff_note",
        title="Annual asthma follow-up",
        contents=[taylor_content],
        occurred_at=_fixture_time("2026-06-18T09:30:00"),
        patient_facing=True,
    )
    taylor_highlight, taylor_pointer = _seed_highlight(
        session,
        name="other-taylor-asthma-controlled",
        clinic_id=clinic_id,
        patient_id=taylor_id,
        entry=taylor_entry,
        version=taylor_version,
        content=taylor_content,
        quote="Asthma remains well controlled with no night-time symptoms",
        label="Asthma stable; continue current preventer plan",
        created_by_id=clinician.id,
        occurred_at=_fixture_time("2026-06-18T09:35:00"),
        base_score=0.66,
        final_score=0.72,
        risk_reason="clinician_confirmed_follow_up",
    )
    taylor_assertion = _seed_fact_assertion(
        session,
        name="other-taylor-asthma-controlled",
        clinic_id=clinic_id,
        patient_id=taylor_id,
        entry=taylor_entry,
        version=taylor_version,
        pointer=taylor_pointer,
        fact_type="condition",
        subject="asthma",
        value="well controlled",
        origin="human",
        effective_time=_fixture_time("2026-06-18T09:30:00"),
        highlight_id=taylor_highlight.id,
    )

    priya_id = demo_id("patient-other-priya")
    priya_report_content = (
        "The patient reports that lisinopril 10 mg is still being taken each morning. "
        "Home blood-pressure readings have usually been between 148 and 156 systolic this week."
    )
    priya_report_entry, priya_report_version = _seed_entry(
        session,
        name="other-priya-medication-report-2026",
        clinic_id=clinic_id,
        patient_id=priya_id,
        author_id=staff_id,
        section="staff",
        origin="human",
        entry_type="manual_staff_note",
        title="Blood-pressure and medication check",
        contents=[priya_report_content],
        occurred_at=_fixture_time("2026-07-02T08:40:00"),
        patient_facing=False,
    )
    priya_plan_content = (
        "Lisinopril was discontinued because of persistent cough; losartan 50 mg once daily is the current antihypertensive plan. "
        "Reconcile the medicines at the next contact and repeat home blood-pressure readings for seven days."
    )
    priya_plan_entry, priya_plan_version = _seed_entry(
        session,
        name="other-priya-medication-plan-2026",
        clinic_id=clinic_id,
        patient_id=priya_id,
        author_id=clinician.id,
        section="clinician",
        origin="human",
        entry_type="manual_clinician_note",
        title="Antihypertensive medication plan",
        contents=[priya_plan_content],
        occurred_at=_fixture_time("2026-07-02T09:05:00"),
        patient_facing=False,
    )
    report_highlight, report_pointer = _seed_highlight(
        session,
        name="other-priya-reported-lisinopril",
        clinic_id=clinic_id,
        patient_id=priya_id,
        entry=priya_report_entry,
        version=priya_report_version,
        content=priya_report_content,
        quote="lisinopril 10 mg is still being taken each morning",
        label="Medication list discrepancy needs clinician review",
        created_by_id=staff_id,
        occurred_at=_fixture_time("2026-07-02T09:10:00"),
        base_score=0.95,
        final_score=0.98,
        risk_reason="medication_status_conflict",
        unresolved=True,
        clinician_confirmed=False,
        support_state="supported",
        risk_floor="high",
        risk_rule_ids=["MEDICATION_STATUS_CONFLICT"],
        abstained=True,
        abstention_reason="UNRESOLVED_HIGH_RISK_CONFLICT",
    )
    plan_highlight, plan_pointer = _seed_highlight(
        session,
        name="other-priya-current-losartan",
        clinic_id=clinic_id,
        patient_id=priya_id,
        entry=priya_plan_entry,
        version=priya_plan_version,
        content=priya_plan_content,
        quote="losartan 50 mg once daily is the current antihypertensive plan",
        label="Confirm current antihypertensive medicines at next contact",
        created_by_id=clinician.id,
        occurred_at=_fixture_time("2026-07-02T09:11:00"),
        base_score=0.86,
        final_score=0.9,
        risk_reason="open_medication_reconciliation",
        pinned=True,
        risk_floor="elevated",
        risk_rule_ids=["OPEN_MEDICATION_RECONCILIATION"],
    )
    report_assertion = _seed_fact_assertion(
        session,
        name="other-priya-reported-lisinopril",
        clinic_id=clinic_id,
        patient_id=priya_id,
        entry=priya_report_entry,
        version=priya_report_version,
        pointer=report_pointer,
        fact_type="medication",
        subject="lisinopril",
        value="reported active 10 mg once daily",
        origin="human",
        effective_time=_fixture_time("2026-07-02T08:40:00"),
        highlight_id=report_highlight.id,
    )
    plan_assertion = _seed_fact_assertion(
        session,
        name="other-priya-current-losartan",
        clinic_id=clinic_id,
        patient_id=priya_id,
        entry=priya_plan_entry,
        version=priya_plan_version,
        pointer=plan_pointer,
        fact_type="medication",
        subject="losartan",
        value="active 50 mg once daily",
        origin="human",
        effective_time=_fixture_time("2026-07-02T09:05:00"),
        highlight_id=plan_highlight.id,
    )
    priya_conflict_id = demo_id("conflict-other-priya-medication-status")
    if session.get(ConflictCase, priya_conflict_id) is None:
        session.add(
            ConflictCase(
                id=priya_conflict_id,
                clinic_id=clinic_id,
                patient_id=priya_id,
                left_entry_id=priya_report_entry.id,
                right_entry_id=priya_plan_entry.id,
                fact_type="medication",
                normalized_key="antihypertensive medication status",
                left_version_id=priya_report_version.id,
                right_version_id=priya_plan_version.id,
                left_pointer_id=report_pointer.id,
                right_pointer_id=plan_pointer.id,
                left_assertion_id=report_assertion.id,
                right_assertion_id=plan_assertion.id,
                severity="high",
                status="unresolved",
                created_at=_fixture_time("2026-07-02T09:10:00"),
            )
        )

    daniel_id = demo_id("patient-other-daniel")
    _seed_entry(
        session,
        name="other-daniel-knee-replacement-2024",
        clinic_id=clinic_id,
        patient_id=daniel_id,
        author_id=clinician.id,
        section="clinician",
        origin="human",
        entry_type="manual_clinician_note",
        title="Right knee replacement follow-up",
        contents=[
            "The surgical wound was clean and dry at the two-week review. "
            "Continue physiotherapy and monitor for increasing redness, swelling, fever, or calf pain."
        ],
        occurred_at=_fixture_time("2024-10-24T14:10:00"),
        patient_facing=True,
    )
    daniel_content = (
        "Walking tolerance has improved to thirty minutes and the patient is using stairs with one handrail. "
        "Continue strengthening exercises twice weekly and review persistent night pain in six weeks."
    )
    daniel_entry, daniel_version = _seed_entry(
        session,
        name="other-daniel-rehabilitation-2026",
        clinic_id=clinic_id,
        patient_id=daniel_id,
        author_id=staff_id,
        section="staff",
        origin="human",
        entry_type="manual_staff_note",
        title="Rehabilitation progress",
        contents=[daniel_content],
        occurred_at=_fixture_time("2026-05-09T15:20:00"),
        patient_facing=True,
    )
    daniel_highlight, daniel_pointer = _seed_highlight(
        session,
        name="other-daniel-rehabilitation-progress",
        clinic_id=clinic_id,
        patient_id=daniel_id,
        entry=daniel_entry,
        version=daniel_version,
        content=daniel_content,
        quote="Walking tolerance has improved to thirty minutes",
        label="Knee rehabilitation progressing; review night pain in six weeks",
        created_by_id=clinician.id,
        occurred_at=_fixture_time("2026-05-09T15:25:00"),
        base_score=0.7,
        final_score=0.76,
        risk_reason="scheduled_follow_up",
    )
    daniel_assertion = _seed_fact_assertion(
        session,
        name="other-daniel-rehabilitation-progress",
        clinic_id=clinic_id,
        patient_id=daniel_id,
        entry=daniel_entry,
        version=daniel_version,
        pointer=daniel_pointer,
        fact_type="functional_status",
        subject="walking tolerance",
        value="improved to 30 minutes",
        origin="human",
        effective_time=_fixture_time("2026-05-09T15:20:00"),
        highlight_id=daniel_highlight.id,
    )

    for highlight, assertion in (
        (taylor_highlight, taylor_assertion),
        (report_highlight, report_assertion),
        (plan_highlight, plan_assertion),
        (daniel_highlight, daniel_assertion),
    ):
        assessment = session.exec(
            select(DecisionAssessment).where(
                DecisionAssessment.clinic_id == clinic_id,
                DecisionAssessment.highlight_id == highlight.id,
            )
        ).first()
        if assessment is not None and assessment.assertion_id is None:
            assessment.assertion_id = assertion.id
            session.add(assessment)

    session.flush()
    from app.api.deps import RequestContext
    from app.services.nightingale import rebuild_glance

    for patient_key, *_ in patient_specs:
        rebuild_glance(
            session,
            RequestContext(user=clinician, membership=clinician_membership),
            demo_id(patient_key),
        )


def _seed_patient_directories(
    session: Session, *, records_per_clinic: int = 300
) -> None:
    """Add a searchable, encrypted synthetic directory to each demo clinic.

    A small number of deliberate same-name records demonstrate why clinical
    identity must be confirmed with DOB and MRN rather than a display name.
    Detailed longitudinal examples remain separate from these directory-only
    records so the demo stays fast to browse.
    """

    first_names = (
        "Aisha",
        "Benjamin",
        "Chloe",
        "Daniel",
        "Elena",
        "Farah",
        "Grace",
        "Haris",
        "Iris",
        "Jia",
        "Kai",
        "Lina",
        "Marcus",
        "Nadia",
        "Owen",
        "Priya",
        "Ryan",
        "Sofia",
        "Thomas",
        "Wei Ming",
    )
    last_names = (
        "Abdullah",
        "Chen",
        "Goh",
        "Kaur",
        "Koh",
        "Lee",
        "Lim",
        "Nair",
        "Ng",
        "Rahman",
        "Singh",
        "Tan",
        "Teo",
        "Wong",
        "Yeo",
    )
    configs = (
        (
            "primary",
            demo_id("clinic-primary"),
            demo_id("membership-staff"),
            "NG",
        ),
        (
            "other",
            demo_id("clinic-other"),
            demo_id("membership-other_staff"),
            "HF",
        ),
    )
    for clinic_key, clinic_id, creator_id, mrn_prefix in configs:
        for index in range(1, records_per_clinic + 1):
            patient_id = demo_id(f"directory-{clinic_key}-{index:03d}")
            # The directory contains one deliberate same-name pair. All other
            # generated names are unique so identity warnings stay exceptional
            # rather than dominating the working list.
            display_name = (
                "Jamie Tan"
                if index in {80, 240}
                else (
                    f"{first_names[(index - 1) // len(last_names)]} "
                    f"{last_names[(index - 1) % len(last_names)]}"
                )
            )
            year = 1938 + (index * 7) % 68
            month = 1 + (index * 5) % 12
            day = 1 + (index * 11) % 27
            dob = f"{year:04d}-{month:02d}-{day:02d}"
            mrn = f"{mrn_prefix}{202600000 + index:09d}"
            identity = f"SYN-{clinic_key.upper()}-{index:06d}"
            patient = session.get(Patient, patient_id)
            if patient is None:
                patient = Patient(
                    id=patient_id,
                    clinic_id=clinic_id,
                    display_name_ciphertext=field_codec.encrypt_text(
                        clinic_id,
                        "patient.display_name",
                        patient_id,
                        display_name,
                    ),
                    external_ref_hash=field_codec.blind_index(
                        clinic_id,
                        "patient_identifier:medical_record_number",
                        mrn,
                    ),
                    created_by_membership_id=creator_id,
                )
                session.add(patient)
                session.flush()
            else:
                # Reconcile earlier synthetic directories that reused the same
                # small set of names. Patient IDs, MRNs and clinical history do
                # not change.
                patient.display_name_ciphertext = field_codec.encrypt_text(
                    clinic_id,
                    "patient.display_name",
                    patient_id,
                    display_name,
                )
                session.add(patient)
            _seed_patient_identity(
                session,
                clinic_id=clinic_id,
                patient_id=patient_id,
                creator_membership_id=creator_id,
                display_name=display_name,
                date_of_birth=dob,
                medical_record_number=mrn,
                identity_number=identity,
            )
            snapshot_id = demo_id(f"directory-glance-{clinic_key}-{index:03d}")
            if session.get(PatientGlanceSnapshot, snapshot_id) is None:
                session.add(
                    PatientGlanceSnapshot(
                        id=snapshot_id,
                        clinic_id=clinic_id,
                        patient_id=patient_id,
                        payload_ciphertext=field_codec.encrypt_json(
                            clinic_id,
                            "glance.payload",
                            snapshot_id,
                            {"cards": [], "review_cards": []},
                        ),
                    )
                )
        session.flush()


def _seed_today_visits(session: Session) -> None:
    """Seed a small, deterministic Singapore clinic schedule for today.

    Visit identities remain stable across restarts while their scheduled date
    follows the current Singapore calendar day. This keeps the local clinic
    workspace useful without turning patient creation time into a fake visit.
    """

    singapore = ZoneInfo("Asia/Singapore")
    today = datetime.now(singapore).date()

    def scheduled_at(hour: int, minute: int) -> datetime:
        local = datetime.combine(today, time(hour, minute), tzinfo=singapore)
        return local.astimezone(UTC)

    schedules = (
        (
            "primary",
            demo_id("clinic-primary"),
            (
                ("patient-decay", 8, 30, "in_progress", "acute_review"),
                ("patient-primary", 9, 15, "checked_in", "follow_up"),
                ("directory-primary-005", 10, 0, "scheduled", "clinic_visit"),
                (
                    "directory-primary-034",
                    11,
                    15,
                    "scheduled",
                    "medication_review",
                ),
                ("directory-primary-080", 13, 30, "scheduled", "follow_up"),
                ("directory-primary-121", 15, 0, "scheduled", "clinic_visit"),
            ),
        ),
        (
            "other",
            demo_id("clinic-other"),
            (
                ("patient-other-priya", 8, 45, "in_progress", "acute_review"),
                ("patient-other", 9, 30, "checked_in", "follow_up"),
                ("directory-other-008", 10, 30, "scheduled", "clinic_visit"),
                (
                    "directory-other-055",
                    11,
                    45,
                    "scheduled",
                    "medication_review",
                ),
                ("directory-other-080", 14, 0, "scheduled", "follow_up"),
                ("directory-other-150", 15, 30, "scheduled", "clinic_visit"),
            ),
        ),
    )
    for clinic_key, clinic_id, clinic_schedule in schedules:
        for patient_key, hour, minute, status, visit_type in clinic_schedule:
            patient_id = demo_id(patient_key)
            if session.get(Patient, patient_id) is None:
                continue
            visit_id = demo_id(f"visit-today-{clinic_key}-{patient_key}")
            visit = session.get(PatientVisit, visit_id)
            if visit is None:
                visit = PatientVisit(
                    id=visit_id,
                    clinic_id=clinic_id,
                    patient_id=patient_id,
                    scheduled_at=scheduled_at(hour, minute),
                    status=status,
                    visit_type=visit_type,
                )
            else:
                visit.scheduled_at = scheduled_at(hour, minute)
                visit.status = status
                visit.visit_type = visit_type
            session.add(visit)
    session.flush()


def seed_demo_data(session: Session, *, include_scenarios: bool = True) -> None:
    if session.get(Clinic, demo_id("clinic-primary")) is not None:
        _seed_platform_administrator(session)
        _seed_redaction_qualification(session, demo_id("clinic-primary"))
        _seed_redaction_qualification(session, demo_id("clinic-other"))
        _seed_patient_identity(
            session,
            clinic_id=demo_id("clinic-primary"),
            patient_id=demo_id("patient-primary"),
            creator_membership_id=demo_id("membership-staff"),
            display_name="Alex Tan",
            date_of_birth="1985-04-12",
            medical_record_number="MRN2026001",
            identity_number="SYNTHETIC001",
        )
        _seed_patient_identity(
            session,
            clinic_id=demo_id("clinic-other"),
            patient_id=demo_id("patient-other"),
            creator_membership_id=demo_id("membership-other_staff"),
            display_name="Taylor Lee",
            date_of_birth="1991-09-03",
            medical_record_number="MRN2026002",
            identity_number="SYNTHETIC002",
        )
        if include_scenarios:
            _seed_demo_domain(session)
            _seed_other_clinic_examples(session)
            _seed_patient_directories(session)
            _seed_today_visits(session)
        session.commit()
        return

    primary = Clinic(
        id=demo_id("clinic-primary"),
        code="NIGHTINGALE",
        slug="nightingale-demo",
        name="Nightingale Clinic",
    )
    other = Clinic(
        id=demo_id("clinic-other"),
        code="OTHERCLINIC",
        slug="other-demo",
        name="Harbour Family Clinic",
    )
    session.add(primary)
    session.add(other)
    session.flush()
    _seed_redaction_qualification(session, primary.id)
    _seed_redaction_qualification(session, other.id)

    password_hash = get_password_hash("synthetic-demo-only")
    personas = {
        name: User(
            id=demo_id(f"user-{name}"),
            email=email.strip().lower(),
            full_name=name.replace("_", " ").title(),
            hashed_password=password_hash,
        )
        for name, email in PERSONA_EMAILS.items()
    }
    session.add_all(list(personas.values()))
    session.flush()
    _seed_platform_administrator(session)

    role_by_persona = {
        "patient": "patient",
        "staff": "staff",
        "clinician": "clinician",
        "admin": "admin",
        "worker": "worker",
        "other_staff": "staff",
    }
    for persona, user in personas.items():
        clinic_id = other.id if persona == "other_staff" else primary.id
        session.add(
            ClinicMembership(
                id=demo_id(f"membership-{persona}"),
                clinic_id=clinic_id,
                user_id=user.id,
                role=role_by_persona[persona],
            )
        )
    session.flush()

    primary_patient_id = demo_id("patient-primary")
    other_patient_id = demo_id("patient-other")
    session.add(
        Patient(
            id=primary_patient_id,
            clinic_id=primary.id,
            display_name_ciphertext=field_codec.encrypt_text(
                primary.id, "patient.display_name", primary_patient_id, "Alex Tan"
            ),
            external_ref_hash=hashlib.sha256(b"SYNTHETIC-001").hexdigest(),
        )
    )
    session.add(
        Patient(
            id=other_patient_id,
            clinic_id=other.id,
            display_name_ciphertext=field_codec.encrypt_text(
                other.id, "patient.display_name", other_patient_id, "Taylor Lee"
            ),
            external_ref_hash=hashlib.sha256(b"SYNTHETIC-OTHER-001").hexdigest(),
        )
    )
    session.flush()
    _seed_patient_identity(
        session,
        clinic_id=primary.id,
        patient_id=primary_patient_id,
        creator_membership_id=demo_id("membership-staff"),
        display_name="Alex Tan",
        date_of_birth="1985-04-12",
        medical_record_number="MRN2026001",
        identity_number="SYNTHETIC001",
    )
    _seed_patient_identity(
        session,
        clinic_id=other.id,
        patient_id=other_patient_id,
        creator_membership_id=demo_id("membership-other_staff"),
        display_name="Taylor Lee",
        date_of_birth="1991-09-03",
        medical_record_number="MRN2026002",
        identity_number="SYNTHETIC002",
    )
    worker_job_id = demo_id("job-worker-demo")
    worker_attempt_id = demo_id("job-worker-demo-attempt")
    session.add(
        Job(
            id=worker_job_id,
            clinic_id=primary.id,
            patient_id=primary_patient_id,
            kind="synthetic_worker_fixture",
            state="running",
            attempt_count=1,
            locked_by=str(worker_attempt_id),
            locked_until=get_datetime_utc() + timedelta(hours=8),
            idempotency_key=hashlib.sha256(b"synthetic-worker-fixture-key").hexdigest(),
            request_sha256=hashlib.sha256(b"synthetic-worker-fixture").hexdigest(),
            payload_ciphertext=field_codec.encrypt_json(
                primary.id, "job.payload", worker_job_id, {"synthetic": True}
            ),
            created_by_id=personas["worker"].id,
        )
    )
    session.add(
        JobAttempt(
            id=worker_attempt_id,
            clinic_id=primary.id,
            job_id=worker_job_id,
            worker_membership_id=demo_id("membership-worker"),
            attempt_no=1,
        )
    )
    session.add(
        PatientUserLink(
            id=demo_id("patient-user-link-primary"),
            clinic_id=primary.id,
            patient_id=primary_patient_id,
            user_id=personas["patient"].id,
        )
    )

    for clinic_id, patient_id, suffix in (
        (primary.id, primary_patient_id, "primary"),
        (other.id, other_patient_id, "other"),
    ):
        snapshot_id = demo_id(f"glance-{suffix}")
        session.add(
            PatientGlanceSnapshot(
                id=snapshot_id,
                clinic_id=clinic_id,
                patient_id=patient_id,
                payload_ciphertext=field_codec.encrypt_json(
                    clinic_id, "glance.payload", snapshot_id, {"cards": []}
                ),
            )
        )
    session.flush()
    if include_scenarios:
        _seed_demo_domain(session)
        _seed_other_clinic_examples(session)
        _seed_patient_directories(session)
        _seed_today_visits(session)
    session.commit()


def membership_for_persona(session: Session, persona: str) -> ClinicMembership | None:
    email = PERSONA_EMAILS.get(persona)
    if email is None:
        return None
    user = session.exec(select(User).where(User.email == email)).first()
    if user is None:
        return None
    return session.exec(
        select(ClinicMembership).where(
            ClinicMembership.user_id == user.id,
            col(ClinicMembership.is_active).is_(True),
        )
    ).first()
