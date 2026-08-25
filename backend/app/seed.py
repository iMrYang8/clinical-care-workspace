"""Deterministic synthetic personas used by the local demo and tests."""

import hashlib
import uuid
from datetime import UTC, datetime, timedelta

from sqlmodel import Session, col, select

from app.core.field_crypto import field_codec
from app.core.security import get_password_hash
from app.models import (
    AIRun,
    AuditEvent,
    CareTask,
    Clinic,
    ClinicMembership,
    Comment,
    CommentMention,
    Entry,
    EntryVersion,
    Highlight,
    ImportanceFeatureStat,
    ImportanceFeedbackEvent,
    Job,
    JobAttempt,
    Patient,
    PatientGlanceSnapshot,
    PatientUserLink,
    ProvenancePointer,
    RedactionRun,
    User,
    get_datetime_utc,
)

DEMO_NAMESPACE = uuid.UUID("8b54f8b7-7bca-4513-838c-15ac2b6758ad")


def demo_id(name: str) -> uuid.UUID:
    return uuid.uuid5(DEMO_NAMESPACE, name)


PERSONA_EMAILS = {
    "patient": "patient@nightingale.synthetic",
    "staff": "staff@nightingale.synthetic",
    "clinician": "clinician@nightingale.synthetic",
    "admin": "admin@nightingale.synthetic",
    "worker": "worker@nightingale.synthetic",
    "other_staff": "staff@other-clinic.synthetic",
}


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
                    "Jordan Archive Synthetic",
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
            "Medication list reviewed during the synthetic home visit.",
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
    for entry_type, interaction_type, title, content in (
        (
            "ai_doctor_consult_summary",
            "doctor_consult",
            "AI doctor consult summary",
            "Synthetic doctor-consult draft; clinician review is required.",
        ),
        (
            "ai_nurse_consult_summary",
            "care_note",
            "AI nurse consult summary",
            "Synthetic nurse-consult draft; clinician review is required.",
        ),
        (
            "ai_patient_session_summary",
            "patient_insight",
            "AI patient session summary",
            "Synthetic patient-session draft; clinician review is required.",
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
        contents=["Synthetic resolved observation eligible for cold storage."],
        occurred_at=_fixture_time("2023-01-10T10:00:00"),
        patient_facing=False,
    )
    session.flush()

    source = (
        "Medication reconciliation completed. Fall risk remains elevated. "
        "Follow-up blood pressure check due Friday. Daughter reports improved sleep."
    )
    highlight_specs = (
        ("fall-risk", "Fall risk remains elevated", True, True, "critical"),
        (
            "bp-followup",
            "Follow-up blood pressure check due Friday",
            False,
            True,
            "pinned",
        ),
        (
            "medication",
            "Medication reconciliation completed",
            False,
            True,
            "clinician_accepted",
        ),
        ("sleep", "Daughter reports improved sleep", False, False, "recency"),
    )
    for index, (name, quote, critical, pinned, reason) in enumerate(highlight_specs):
        highlight_id = demo_id(f"highlight-{name}")
        pointer_id = demo_id(f"provenance-{name}")
        start = source.index(quote)
        end = start + len(quote)
        prefix = source[max(0, start - 16) : start]
        suffix = source[end : end + 16]
        if session.get(Highlight, highlight_id) is None:
            session.add(
                Highlight(
                    id=highlight_id,
                    clinic_id=clinic_id,
                    patient_id=patient_id,
                    entry_id=current_entry.id,
                    source_entry_version_id=current_version.id,
                    label_ciphertext=field_codec.encrypt_text(
                        clinic_id, "highlight.label", highlight_id, quote
                    ),
                    status="accepted",
                    pinned=pinned,
                    critical=critical,
                    patient_facing=index < 3,
                    feature_keys_json=[f"fixture:{name}"],
                    base_score=0.7 - index * 0.05,
                    learned_score=0.05 if index == 2 else 0.0,
                    final_score=1.0 - index * 0.1,
                    risk_reason=reason,
                    clinician_confirmed=index == 2,
                    created_by_id=clinician_id,
                    created_at=_fixture_time("2026-02-06T14:10:00")
                    + timedelta(minutes=index),
                )
            )
        if session.get(ProvenancePointer, pointer_id) is None:
            session.add(
                ProvenancePointer(
                    id=pointer_id,
                    clinic_id=clinic_id,
                    highlight_id=highlight_id,
                    entry_version_id=current_version.id,
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
    start = source.index(quote)
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
                    "@clinician Please review this synthetic fall-risk item.",
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
                    source[max(0, start - 16) : start],
                ),
                suffix_ciphertext=field_codec.encrypt_text(
                    clinic_id,
                    "comment.suffix",
                    comment_id,
                    source[end : end + 16],
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
                    "Review synthetic fall-risk evidence",
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
    if session.get(ImportanceFeatureStat, stat_id) is None:
        session.add(
            ImportanceFeatureStat(
                id=stat_id,
                clinic_id=clinic_id,
                feature_key="fixture:medication",
                weight=0.05,
                positive_count=1,
                observation_count=1,
            )
        )
    feedback_id = demo_id("importance-feedback-medication")
    if session.get(ImportanceFeedbackEvent, feedback_id) is None:
        session.add(
            ImportanceFeedbackEvent(
                id=feedback_id,
                clinic_id=clinic_id,
                highlight_id=demo_id("highlight-medication"),
                actor_membership_id=demo_id("membership-clinician"),
                signal="accept",
                feature_keys_json=["fixture:medication"],
                applied_delta=0.05,
                idempotency_key="fixture:medication:accept:v1",
                request_sha256=hashlib.sha256(b"fixture-medication-accept").hexdigest(),
            )
        )

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


def seed_demo_data(session: Session, *, include_scenarios: bool = True) -> None:
    if session.get(Clinic, demo_id("clinic-primary")) is not None:
        if include_scenarios:
            _seed_demo_domain(session)
        session.commit()
        return

    primary = Clinic(
        id=demo_id("clinic-primary"),
        slug="nightingale-demo",
        name="Nightingale Demo Clinic",
    )
    other = Clinic(
        id=demo_id("clinic-other"), slug="other-demo", name="Other Synthetic Clinic"
    )
    session.add(primary)
    session.add(other)
    session.flush()

    password_hash = get_password_hash("synthetic-demo-only")
    personas = {
        name: User(
            id=demo_id(f"user-{name}"),
            email=email,
            full_name=name.replace("_", " ").title(),
            hashed_password=password_hash,
        )
        for name, email in PERSONA_EMAILS.items()
    }
    session.add_all(list(personas.values()))
    session.flush()

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
                primary.id, "patient.display_name", primary_patient_id, "Alex Synthetic"
            ),
            external_ref_hash=hashlib.sha256(b"SYNTHETIC-001").hexdigest(),
        )
    )
    session.add(
        Patient(
            id=other_patient_id,
            clinic_id=other.id,
            display_name_ciphertext=field_codec.encrypt_text(
                other.id, "patient.display_name", other_patient_id, "Taylor Synthetic"
            ),
            external_ref_hash=hashlib.sha256(b"SYNTHETIC-OTHER-001").hexdigest(),
        )
    )
    session.flush()
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
