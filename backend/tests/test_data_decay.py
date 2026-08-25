import uuid
from datetime import timedelta

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.api.deps import RequestContext
from app.core.db import engine
from app.models import (
    ClinicMembership,
    DecayRun,
    EntryVersion,
    ProvenancePointer,
    User,
)
from app.seed import demo_id
from app.services.decay import archive_version, rehydrate_version
from app.services.nightingale import resolve_pointer


def test_zstd_aes_archive_rehydrate_preserves_hash_and_provenance_and_rejects_tamper(
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
        session.refresh(blob)
        blob.payload_ciphertext = (
            bytes([blob.payload_ciphertext[0] ^ 1]) + blob.payload_ciphertext[1:]
        )
        session.add(blob)
        session.commit()
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
