import csv
import io
import json
import wave
from pathlib import Path

from sqlmodel import Session, select

from app.core.field_crypto import field_codec
from app.models import AudioAsset, DomainEvent, Entry, Patient, TranscriptSegment
from app.services.dataset_imports import (
    import_evaluation_pack,
    parse_textgrid,
    stable_id,
)


def _write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _write_wav(path: Path, samples: list[int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = io.BytesIO()
    with wave.open(payload, "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(16_000)
        for sample in samples:
            target.writeframesraw(int(sample).to_bytes(2, "little", signed=True))
    path.write_bytes(payload.getvalue())


def _textgrid(speaker: str, text: str, start: float, end: float) -> str:
    return f'''File type = "ooTextFile"
Object class = "TextGrid"

xmin = 0
xmax = {end}
tiers? <exists>
size = 1
item []:
    item [1]:
        class = "IntervalTier"
        name = "{speaker}"
        xmin = 0
        xmax = {end}
        intervals: size = 1
        intervals [1]:
            xmin = {start}
            xmax = {end}
            text = "{text}"
'''


def _evaluation_pack(tmp_path: Path) -> tuple[Path, Path]:
    raw = tmp_path / "raw"
    synthea = raw / "synthea/extracted"
    patient_id = "synthea-patient-1"
    encounter_id = "synthea-encounter-1"
    _write_csv(
        synthea / "patients.csv",
        ["Id", "BIRTHDATE", "GENDER", "RACE", "ETHNICITY"],
        [
            {
                "Id": patient_id,
                "BIRTHDATE": "1980-01-01",
                "GENDER": "F",
                "RACE": "asian",
                "ETHNICITY": "synthetic",
            }
        ],
    )
    _write_csv(
        synthea / "encounters.csv",
        [
            "Id",
            "START",
            "PATIENT",
            "DESCRIPTION",
            "ENCOUNTERCLASS",
            "REASONDESCRIPTION",
        ],
        [
            {
                "Id": encounter_id,
                "START": "2026-01-02T10:00:00Z",
                "PATIENT": patient_id,
                "DESCRIPTION": "Synthetic annual review",
                "ENCOUNTERCLASS": "wellness",
                "REASONDESCRIPTION": "Medication review",
            }
        ],
    )
    resource_fields = ["PATIENT", "ENCOUNTER", "DESCRIPTION", "VALUE", "UNITS"]
    for filename, description in (
        ("conditions.csv", "Synthetic hypertension"),
        ("medications.csv", "Synthetic medicine"),
        ("allergies.csv", "Synthetic allergy"),
        ("observations.csv", "Synthetic blood pressure"),
    ):
        _write_csv(
            synthea / filename,
            resource_fields,
            [
                {
                    "PATIENT": patient_id,
                    "ENCOUNTER": encounter_id,
                    "DESCRIPTION": description,
                    "VALUE": "120" if filename == "observations.csv" else "",
                    "UNITS": "mmHg" if filename == "observations.csv" else "",
                }
            ],
        )

    aci = raw / "aci_bench/extracted/aci-bench-corpus/challenge_data"
    _write_csv(
        aci / "train.csv",
        ["dataset", "encounter_id", "dialogue", "note"],
        [
            {
                "dataset": "aci",
                "encounter_id": "ACI001",
                "dialogue": "[doctor] How are you?\n[patient] Better.",
                "note": "Synthetic reference note.",
            }
        ],
    )
    _write_csv(
        aci / "train_metadata.csv",
        ["encounter_id", "cc"],
        [{"encounter_id": "ACI001", "cc": "follow-up"}],
    )

    primock = raw / "primock57/extracted/primock57-test"
    (primock / "notes").mkdir(parents=True)
    (primock / "transcripts").mkdir(parents=True)
    (primock / "notes/day1_consultation01.json").write_text(
        json.dumps(
            {
                "presenting_complaint": "synthetic cough",
                "note": "Synthetic clinician reference note.",
            }
        )
    )
    (primock / "transcripts/day1_consultation01_doctor.TextGrid").write_text(
        _textgrid("Doctor", "How are you?", 0.0, 1.0)
    )
    (primock / "transcripts/day1_consultation01_patient.TextGrid").write_text(
        _textgrid("Patient", "I am better.", 1.0, 2.0)
    )
    audio = raw / "primock57/audio"
    _write_wav(audio / "day1_consultation01_doctor.wav", [1, 2, 3, 4])
    _write_wav(audio / "day1_consultation01_patient.wav", [5, 6, 7, 8])

    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "datasets": [
                    {
                        "name": name,
                        "version": "test-v1",
                        "license": "test-license",
                        "source_url": f"https://example.test/{name}",
                        "archive": {"sha256": f"{index:064x}"},
                    }
                    for index, name in enumerate(
                        ("synthea", "aci_bench", "primock57"), start=1
                    )
                ],
            }
        )
    )
    return raw, manifest


def test_textgrid_parser_preserves_reference_timing(tmp_path: Path) -> None:
    path = tmp_path / "sample.TextGrid"
    path.write_text(_textgrid("Doctor", "Review <UNSURE>medicine</UNSURE>", 1.25, 2.5))
    segments = parse_textgrid(path, "Doctor")
    assert len(segments) == 1
    assert segments[0].speaker == "Doctor"
    assert segments[0].text == "Review medicine"
    assert segments[0].start_ms == 1250
    assert segments[0].end_ms == 2500


def test_evaluation_pack_import_is_encrypted_traceable_and_idempotent(
    owner_session: Session, client, auth_headers, tmp_path: Path
) -> None:
    raw, manifest = _evaluation_pack(tmp_path)
    first = import_evaluation_pack(
        owner_session,
        raw_root=raw,
        manifest_path=manifest,
        synthea_limit=1,
        aci_limit=1,
        primock_limit=1,
    )
    assert first["total"] == {
        "patients_created": 3,
        "entries_created": 6,
        "relations_created": 2,
        "highlights_created": 1,
        "sessions_created": 1,
        "transcript_segments_created": 2,
        "audio_assets_created": 1,
    }

    second = import_evaluation_pack(
        owner_session,
        raw_root=raw,
        manifest_path=manifest,
        synthea_limit=1,
        aci_limit=1,
        primock_limit=1,
    )
    assert all(value == 0 for value in second["total"].values())

    synthea_patient_id = stable_id("synthea", "patient", "synthea-patient-1")
    patient = owner_session.get(Patient, synthea_patient_id)
    assert patient is not None
    assert b"Synthea Patient" not in patient.display_name_ciphertext

    encounter_entry_id = stable_id("synthea", "entry", "encounter:synthea-encounter-1")
    encounter = owner_session.get(Entry, encounter_entry_id)
    assert encounter is not None and encounter.current_version_id is not None
    source_events = owner_session.exec(
        select(DomainEvent).where(
            DomainEvent.aggregate_id == encounter_entry_id,
            DomainEvent.event_type == "dataset.synthetic_imported",
        )
    ).all()
    assert len(source_events) == 1
    assert source_events[0].payload_json["synthetic"] is True
    assert "Synthetic annual review" not in json.dumps(source_events[0].payload_json)

    asset_id = stable_id("primock57", "audio-asset", "day1_consultation01")
    asset = owner_session.get(AudioAsset, asset_id)
    assert asset is not None
    payload = field_codec.decrypt(
        asset.clinic_id, "audio_asset.payload", asset.id, asset.payload_ciphertext
    )
    with wave.open(io.BytesIO(payload), "rb") as imported_audio:
        assert imported_audio.getnchannels() == 2
        assert imported_audio.getframerate() == 16_000

    segments = owner_session.exec(
        select(TranscriptSegment)
        .where(
            TranscriptSegment.session_id
            == stable_id("primock57", "voice-session", "day1_consultation01")
        )
        .order_by(TranscriptSegment.ordinal)
    ).all()
    assert [segment.speaker_id for segment in segments] == ["Doctor", "Patient"]

    headers = auth_headers("staff")
    patient_list = client.get("/api/v1/patients", headers=headers)
    assert patient_list.status_code == 200
    names = {row["display_name"] for row in patient_list.json()["data"]}
    assert {
        "Synthea Patient 001",
        "ACI-Bench Patient ACI001",
        "PriMock57 Patient 01",
    }.issubset(names)
    timeline = client.get(
        f"/api/v1/patients/{synthea_patient_id}/timeline", headers=headers
    )
    assert timeline.status_code == 200
    assert len(timeline.json()["data"]) == 2
    glance = client.get(
        f"/api/v1/patients/{synthea_patient_id}/glance", headers=headers
    )
    assert glance.status_code == 200
    assert len(glance.json()["cards"]) == 1
