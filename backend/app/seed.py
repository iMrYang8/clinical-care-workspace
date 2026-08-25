"""Deterministic synthetic personas used by the local demo and tests."""

import hashlib
import uuid
from datetime import timedelta

from sqlmodel import Session, col, select

from app.core.field_crypto import field_codec
from app.core.security import get_password_hash
from app.models import (
    Clinic,
    ClinicMembership,
    Job,
    JobAttempt,
    Patient,
    PatientGlanceSnapshot,
    PatientUserLink,
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


def seed_demo_data(session: Session) -> None:
    if session.get(Clinic, demo_id("clinic-primary")) is not None:
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
