import hashlib
import importlib
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from time import sleep

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.exc import DBAPIError
from sqlmodel import Session, select

from app.api.deps import RequestContext
from app.core.db import engine
from app.core.field_crypto import field_codec
from app.models import (
    ArchiveBlob,
    ClinicMembership,
    DecayRun,
    EntryVersion,
    ProvenancePointer,
    RetentionLock,
    User,
)
from app.seed import demo_id
from app.services.decay import (
    MAX_REHYDRATED_BYTES,
    archive_version,
    decode_archive,
    list_decay_candidates,
    rehydrate_version,
)
from app.services.nightingale import resolve_pointer


def test_zstd_aes_archive_rehydrate_preserves_hash_and_provenance_and_rejects_tamper(
    client: TestClient, auth_headers, owner_session: Session
) -> None:
    headers = auth_headers("clinician")
    patient_id = client.get("/api/v1/patients", headers=headers).json()["data"][0]["id"]
    entry = client.post(
        "/api/v1/entries",
        headers=headers,
        json={
            "patient_id": patient_id,
            "section": "clinician",
            "title": "Old synthetic evidence",
            "content": "prefix EVIDENCE suffix",
            "patient_facing": True,
        },
    )
    assert entry.status_code == 201, entry.text
    highlight = client.post(
        f"/api/v1/entries/{entry.json()['id']}/highlights",
        headers=headers,
        json={
            "entry_version_id": entry.json()["version_id"],
            "start_offset": 7,
            "end_offset": 15,
            "exact_quote": "EVIDENCE",
            "prefix": "prefix ",
            "suffix": " suffix",
            "label": "Historical evidence",
        },
    )
    assert highlight.status_code == 201, highlight.text

    with Session(engine) as session:
        user = session.get(User, demo_id("user-clinician"))
        membership = session.get(ClinicMembership, demo_id("membership-clinician"))
        assert user is not None and membership is not None
        context = RequestContext(user=user, membership=membership)
        version = session.get(EntryVersion, uuid.UUID(entry.json()["version_id"]))
        pointer = session.exec(
            select(ProvenancePointer).where(
                ProvenancePointer.highlight_id == uuid.UUID(highlight.json()["id"])
            )
        ).one()
        assert version is not None
        before = resolve_pointer(pointer, version)
        content_hash = version.content_sha256
        future = version.created_at + timedelta(days=731)

        blob = archive_version(session, context, version, now=future)
        session.commit()
        session.refresh(version)
        assert version.storage_tier == "cold"
        assert version.title_ciphertext is None
        assert version.content_ciphertext is None

        # The AEAD associated data binds the blob to the immutable version and
        # canonical plaintext hash, so swapping either metadata field fails.
        original_plaintext_hash = blob.plaintext_sha256
        blob.plaintext_sha256 = "0" * 64
        with session.no_autoflush:
            with pytest.raises(HTTPException) as aad_error:
                rehydrate_version(session, context, version)
        assert aad_error.value.detail["code"] == "ARCHIVE_INTEGRITY_ERROR"
        session.rollback()
        version = session.get(EntryVersion, uuid.UUID(entry.json()["version_id"]))
        blob = session.get(type(blob), blob.id)
        assert version is not None and blob is not None
        assert blob.plaintext_sha256 == original_plaintext_hash

        rehydrate_version(session, context, version)
        session.commit()
        session.refresh(version)
        after = resolve_pointer(pointer, version)
        assert version.storage_tier == "warm"
        assert version.content_sha256 == content_hash
        assert after["exact_quote"] == before["exact_quote"]
        assert after["quote_sha256"] == before["quote_sha256"]

        archive_version(session, context, version, now=future)
        session.commit()
        stored_blob = owner_session.get(ArchiveBlob, blob.id)
        assert stored_blob is not None
        stored_blob.payload_ciphertext = (
            bytes([stored_blob.payload_ciphertext[0] ^ 1])
            + stored_blob.payload_ciphertext[1:]
        )
        owner_session.add(stored_blob)
        owner_session.commit()
        session.expire_all()
        with pytest.raises(HTTPException) as error:
            rehydrate_version(session, context, version)
        assert error.value.detail["code"] == "ARCHIVE_INTEGRITY_ERROR"


def test_decay_api_is_dry_run_by_default_and_admin_cannot_write(
    client: TestClient, auth_headers
) -> None:
    clinician = auth_headers("clinician")
    preview = client.get("/api/v1/decay/preview", headers=clinician)
    assert preview.status_code == 200, preview.text
    assert preview.json()["policy_version"] == "nightingale-decay-v1"

    admin = auth_headers("admin")
    assert client.get("/api/v1/decay/preview", headers=admin).status_code == 200
    denied = client.post("/api/v1/decay/archive", headers=admin, json={})
    assert denied.status_code == 403

    dry_run = client.post("/api/v1/decay/archive", headers=clinician, json={})
    assert dry_run.status_code == 200, dry_run.text
    assert dry_run.json()["archived_count"] == 0
    with Session(engine) as session:
        run = session.exec(select(DecayRun)).one()
        assert run.dry_run is True


def test_decay_preview_reports_every_protection_reason(
    client: TestClient, auth_headers
) -> None:
    headers = auth_headers("clinician")
    patient_id = client.get("/api/v1/patients", headers=headers).json()["data"][0]["id"]
    entry = client.post(
        "/api/v1/entries",
        headers=headers,
        json={
            "patient_id": patient_id,
            "section": "clinician",
            "title": "Protected historical source",
            "content": "AB",
        },
    ).json()
    for start, end, quote, flags in (
        (0, 1, "A", {"critical": True}),
        (1, 2, "B", {"unresolved": True}),
    ):
        response = client.post(
            f"/api/v1/entries/{entry['id']}/highlights",
            headers=headers,
            json={
                "entry_version_id": entry["version_id"],
                "start_offset": start,
                "end_offset": end,
                "exact_quote": quote,
                "label": "Protection fixture",
                **flags,
            },
        )
        assert response.status_code == 201, response.text

    with Session(engine) as session:
        user = session.get(User, demo_id("user-clinician"))
        membership = session.get(ClinicMembership, demo_id("membership-clinician"))
        version = session.get(EntryVersion, uuid.UUID(entry["version_id"]))
        assert user is not None and membership is not None and version is not None
        candidates = list_decay_candidates(
            session,
            RequestContext(user=user, membership=membership),
            now=version.created_at + timedelta(days=731),
        )
        candidate = next(
            item for item in candidates if item.entry_version_id == version.id
        )
        assert {"critical", "unresolved"} <= set(candidate.protected_reasons)
        assert candidate.eligible_for_cold is False


@pytest.mark.unit
def test_archive_expansion_limit_returns_413() -> None:
    import zstandard

    clinic_id = uuid.uuid4()
    version_id = uuid.uuid4()
    blob_id = uuid.uuid4()
    plaintext = b"x" * (MAX_REHYDRATED_BYTES + 1)
    plaintext_hash = hashlib.sha256(plaintext).hexdigest()
    compressed = zstandard.ZstdCompressor(level=9).compress(plaintext)
    encrypted = field_codec.encrypt(
        clinic_id,
        f"archive.payload:{version_id}:{plaintext_hash}",
        blob_id,
        compressed,
    )
    blob = ArchiveBlob(
        id=blob_id,
        clinic_id=clinic_id,
        entry_version_id=version_id,
        payload_ciphertext=encrypted,
        plaintext_sha256=plaintext_hash,
        ciphertext_sha256=hashlib.sha256(encrypted).hexdigest(),
        original_size=len(plaintext),
        compressed_size=len(compressed),
    )
    with pytest.raises(HTTPException) as error:
        decode_archive(blob)
    assert error.value.status_code == 413


def test_concurrent_retention_lock_serializes_before_archive_recheck(
    client: TestClient, auth_headers, owner_session: Session
) -> None:
    headers = auth_headers("clinician")
    patient_id = client.get("/api/v1/patients", headers=headers).json()["data"][0]["id"]
    entry = client.post(
        "/api/v1/entries",
        headers=headers,
        json={
            "patient_id": patient_id,
            "section": "clinician",
            "title": "Concurrent retention fixture",
            "content": "retained history",
        },
    ).json()
    version_id = uuid.UUID(entry["version_id"])
    version = owner_session.get(EntryVersion, version_id)
    user = owner_session.get(User, demo_id("user-clinician"))
    assert version is not None and user is not None
    future = version.created_at + timedelta(days=731)
    owner_session.add(
        RetentionLock(
            clinic_id=version.clinic_id,
            entity_type="entry_version",
            entity_id=version.id,
            reason_code="SYNTHETIC_LEGAL_HOLD",
            created_by_id=user.id,
        )
    )
    owner_session.flush()  # trigger holds the shared advisory lock until commit

    def attempt_archive() -> dict:
        with Session(engine) as session:
            worker_user = session.get(User, demo_id("user-clinician"))
            membership = session.get(ClinicMembership, demo_id("membership-clinician"))
            candidate = session.get(EntryVersion, version_id)
            assert worker_user is not None and membership is not None
            assert candidate is not None
            try:
                archive_version(
                    session,
                    RequestContext(user=worker_user, membership=membership),
                    candidate,
                    now=future,
                )
            except HTTPException as exc:
                session.rollback()
                assert isinstance(exc.detail, dict)
                return exc.detail
            raise AssertionError("retention-protected version was archived")

    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(attempt_archive)
        sleep(0.15)
        assert pending.done() is False
        owner_session.commit()
        detail = pending.result(timeout=5)
    assert detail["code"] == "DECAY_NOT_ELIGIBLE"
    assert detail["protected_reasons"] == ["retention_lock"]


def test_ai_trust_downgrade_blocks_cold_data_then_clears_rehydrated_reference(
    client: TestClient, auth_headers, owner_session: Session
) -> None:
    hardening = importlib.import_module(
        "app.alembic.versions.b5e7a9c2d140_harden_worker_feedback_and_decay"
    )
    trust = importlib.import_module(
        "app.alembic.versions.e8b5c1d7a2f0_ai_trust_importance_decay"
    )
    headers = auth_headers("clinician")
    patient_id = client.get("/api/v1/patients", headers=headers).json()["data"][0]["id"]
    entry = client.post(
        "/api/v1/entries",
        headers=headers,
        json={
            "patient_id": patient_id,
            "section": "clinician",
            "title": "Downgrade fixture",
            "content": "durable history",
        },
    ).json()
    version_id = uuid.UUID(entry["version_id"])
    with Session(engine) as session:
        user = session.get(User, demo_id("user-clinician"))
        membership = session.get(ClinicMembership, demo_id("membership-clinician"))
        version = session.get(EntryVersion, version_id)
        assert user is not None and membership is not None and version is not None
        context = RequestContext(user=user, membership=membership)
        future = version.created_at + timedelta(days=731)
        archive_version(session, context, version, now=future)
        session.commit()

    migration_engine = owner_session.get_bind()
    with migration_engine.connect() as connection:
        transaction = connection.begin()
        migration_context = MigrationContext.configure(connection)
        with pytest.raises(DBAPIError, match="rehydrate every cold"):
            with Operations.context(migration_context):
                hardening.downgrade()
                trust.downgrade()
        transaction.rollback()

    with Session(engine) as session:
        user = session.get(User, demo_id("user-clinician"))
        membership = session.get(ClinicMembership, demo_id("membership-clinician"))
        version = session.get(EntryVersion, version_id)
        assert user is not None and membership is not None and version is not None
        rehydrate_version(
            session, RequestContext(user=user, membership=membership), version
        )
        session.commit()

    with migration_engine.connect() as connection:
        transaction = connection.begin()
        migration_context = MigrationContext.configure(connection)
        with Operations.context(migration_context):
            hardening.downgrade()
            trust.downgrade()
        row = connection.execute(
            select(
                EntryVersion.archive_blob_id,
                EntryVersion.title_ciphertext,
                EntryVersion.content_ciphertext,
            ).where(EntryVersion.id == version_id)
        ).one()
        assert row.archive_blob_id is None
        assert row.title_ciphertext is not None
        assert row.content_ciphertext is not None
        transaction.rollback()
