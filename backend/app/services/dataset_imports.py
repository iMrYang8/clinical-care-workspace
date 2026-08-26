"""Idempotent importers for the pinned synthetic evaluation datasets."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import sys
import uuid
import wave
from array import array
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlmodel import Session, select

from app.api.deps import RequestContext
from app.core.field_crypto import field_codec
from app.models import (
    AudioAsset,
    ClinicMembership,
    DomainEvent,
    Entry,
    EntryRelation,
    EntryVersion,
    Highlight,
    Patient,
    PatientGlanceSnapshot,
    ProvenancePointer,
    TranscriptRevision,
    TranscriptSegment,
    User,
    VoiceSession,
)
from app.seed import demo_id
from app.services.nightingale import rebuild_glance

IMPORT_NAMESPACE = uuid.UUID("df49f5cc-59d4-492d-9374-7aa5fa67f77f")
IMPORT_TIME = datetime(2026, 8, 26, 0, 0, tzinfo=UTC)


@dataclass(frozen=True)
class DatasetMetadata:
    name: str
    version: str
    license: str
    source_url: str
    archive_sha256: str


@dataclass
class ImportCounts:
    patients_created: int = 0
    entries_created: int = 0
    relations_created: int = 0
    highlights_created: int = 0
    sessions_created: int = 0
    transcript_segments_created: int = 0
    audio_assets_created: int = 0

    def add(self, other: ImportCounts) -> None:
        for name, value in asdict(other).items():
            setattr(self, name, getattr(self, name) + value)

    def to_dict(self) -> dict[str, int]:
        return asdict(self)


@dataclass(frozen=True)
class ImportedEntry:
    entry: Entry
    version: EntryVersion
    created: bool


@dataclass(frozen=True)
class ReferenceSegment:
    speaker: str
    text: str
    start_ms: int
    end_ms: int
    overlap_group_id: str | None = None


def stable_id(dataset: str, kind: str, source_id: str) -> uuid.UUID:
    return uuid.uuid5(IMPORT_NAMESPACE, f"{dataset}:{kind}:{source_id}")


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _safe_datetime(value: str, fallback: datetime) -> datetime:
    normalized = value.strip()
    if not normalized:
        return fallback
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        return [
            {key: value or "" for key, value in row.items() if key is not None}
            for row in reader
        ]


def load_dataset_metadata(manifest_path: Path) -> dict[str, DatasetMetadata]:
    with manifest_path.open(encoding="utf-8") as handle:
        manifest: dict[str, Any] = json.load(handle)
    if manifest.get("schema_version") != 1:
        raise ValueError("Unsupported evaluation-pack manifest")
    result: dict[str, DatasetMetadata] = {}
    for raw in manifest["datasets"]:
        dataset = dict(raw)
        archive = dict(dataset["archive"])
        metadata = DatasetMetadata(
            name=str(dataset["name"]),
            version=str(dataset["version"]),
            license=str(dataset["license"]),
            source_url=str(dataset["source_url"]),
            archive_sha256=str(archive["sha256"]),
        )
        result[metadata.name] = metadata
    return result


def _source_payload(
    metadata: DatasetMetadata,
    *,
    source_record_id: str,
    source_sha256: str,
    transformation: str,
) -> dict[str, object]:
    return {
        "dataset_name": metadata.name,
        "dataset_version": metadata.version,
        "license": metadata.license,
        "source_url": metadata.source_url,
        "source_record_id": source_record_id,
        "source_sha256": source_sha256,
        "archive_sha256": metadata.archive_sha256,
        "transformation_version": transformation,
        "synthetic": True,
    }


def _source_event(
    session: Session,
    *,
    metadata: DatasetMetadata,
    clinic_id: uuid.UUID,
    actor_id: uuid.UUID,
    aggregate_type: str,
    aggregate_id: uuid.UUID,
    source_record_id: str,
    source_sha256: str,
    transformation: str,
) -> bool:
    event_id = stable_id(
        metadata.name,
        f"source-event-{aggregate_type}",
        f"{source_record_id}:{aggregate_id}",
    )
    existing = session.exec(
        select(DomainEvent).where(DomainEvent.id == event_id)
    ).first()
    if existing is not None:
        return False
    session.add(
        DomainEvent(
            id=event_id,
            clinic_id=clinic_id,
            event_type="dataset.synthetic_imported",
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            actor_id=actor_id,
            payload_json=_source_payload(
                metadata,
                source_record_id=source_record_id,
                source_sha256=source_sha256,
                transformation=transformation,
            ),
            created_at=IMPORT_TIME,
        )
    )
    return True


def _patient(
    session: Session,
    *,
    metadata: DatasetMetadata,
    clinic_id: uuid.UUID,
    actor_id: uuid.UUID,
    source_id: str,
    display_name: str,
) -> tuple[Patient, bool]:
    patient_id = stable_id(metadata.name, "patient", source_id)
    existing = session.get(Patient, patient_id)
    if existing is not None:
        return existing, False
    patient = Patient(
        id=patient_id,
        clinic_id=clinic_id,
        display_name_ciphertext=field_codec.encrypt_text(
            clinic_id, "patient.display_name", patient_id, display_name
        ),
        external_ref_hash=sha256_text(
            f"{metadata.name}:{metadata.version}:{source_id}"
        ),
        created_at=IMPORT_TIME,
    )
    session.add(patient)
    session.flush()
    snapshot_id = stable_id(metadata.name, "glance", source_id)
    session.add(
        PatientGlanceSnapshot(
            id=snapshot_id,
            clinic_id=clinic_id,
            patient_id=patient_id,
            payload_ciphertext=field_codec.encrypt_json(
                clinic_id,
                "glance.payload",
                snapshot_id,
                {"cards": [], "patient_cards": []},
            ),
            generated_at=IMPORT_TIME,
        )
    )
    _source_event(
        session,
        metadata=metadata,
        clinic_id=clinic_id,
        actor_id=actor_id,
        aggregate_type="patient",
        aggregate_id=patient_id,
        source_record_id=source_id,
        source_sha256=sha256_text(source_id),
        transformation="nightingale-dataset-import-v1",
    )
    return patient, True


def _entry(
    session: Session,
    *,
    metadata: DatasetMetadata,
    clinic_id: uuid.UUID,
    patient_id: uuid.UUID,
    author_id: uuid.UUID,
    source_id: str,
    section: str,
    origin: str,
    entry_type: str,
    title: str,
    content: str,
    occurred_at: datetime,
) -> ImportedEntry:
    entry_id = stable_id(metadata.name, "entry", source_id)
    version_id = stable_id(metadata.name, "entry-version", source_id)
    existing = session.get(Entry, entry_id)
    if existing is not None:
        version = session.get(EntryVersion, version_id)
        if version is None:
            raise RuntimeError(
                f"Imported entry {entry_id} is missing its stable version"
            )
        return ImportedEntry(existing, version, False)
    entry = Entry(
        id=entry_id,
        clinic_id=clinic_id,
        patient_id=patient_id,
        section=section,
        origin=origin,
        entry_type=entry_type,
        patient_facing=False,
        occurred_at=occurred_at,
        created_at=occurred_at,
    )
    session.add(entry)
    session.flush()
    version = EntryVersion(
        id=version_id,
        clinic_id=clinic_id,
        entry_id=entry_id,
        version_no=1,
        title_ciphertext=field_codec.encrypt_text(
            clinic_id, "entry_version.title", version_id, title
        ),
        content_ciphertext=field_codec.encrypt_text(
            clinic_id, "entry_version.content", version_id, content
        ),
        content_sha256=sha256_text(content),
        patient_facing=False,
        author_id=author_id,
        created_at=occurred_at,
    )
    session.add(version)
    session.flush()
    entry.current_version_id = version_id
    session.add(entry)
    if _source_event(
        session,
        metadata=metadata,
        clinic_id=clinic_id,
        actor_id=author_id,
        aggregate_type="entry",
        aggregate_id=entry_id,
        source_record_id=source_id,
        source_sha256=sha256_text(content),
        transformation="nightingale-dataset-import-v1",
    ):
        session.flush()
    return ImportedEntry(entry, version, True)


def _relation(
    session: Session,
    *,
    metadata: DatasetMetadata,
    clinic_id: uuid.UUID,
    source_entry_id: uuid.UUID,
    target_entry_id: uuid.UUID,
    created_by_id: uuid.UUID,
    source_id: str,
) -> bool:
    relation_id = stable_id(metadata.name, "entry-relation", source_id)
    if session.get(EntryRelation, relation_id) is not None:
        return False
    session.add(
        EntryRelation(
            id=relation_id,
            clinic_id=clinic_id,
            source_entry_id=source_entry_id,
            target_entry_id=target_entry_id,
            relation_type="derived_from",
            created_by_id=created_by_id,
            created_at=IMPORT_TIME,
        )
    )
    return True


def _highlight(
    session: Session,
    *,
    metadata: DatasetMetadata,
    clinic_id: uuid.UUID,
    patient_id: uuid.UUID,
    created_by_id: uuid.UUID,
    source_id: str,
    imported: ImportedEntry,
    exact_quote: str,
) -> bool:
    highlight_id = stable_id(metadata.name, "highlight", source_id)
    if session.get(Highlight, highlight_id) is not None:
        return False
    content = field_codec.decrypt_text(
        clinic_id,
        "entry_version.content",
        imported.version.id,
        imported.version.content_ciphertext or b"",
    )
    start = content.find(exact_quote)
    if start < 0 or not exact_quote:
        return False
    end = start + len(exact_quote)
    pointer_id = stable_id(metadata.name, "provenance", source_id)
    session.add(
        Highlight(
            id=highlight_id,
            clinic_id=clinic_id,
            patient_id=patient_id,
            entry_id=imported.entry.id,
            source_entry_version_id=imported.version.id,
            label_ciphertext=field_codec.encrypt_text(
                clinic_id, "highlight.label", highlight_id, exact_quote
            ),
            status="accepted",
            patient_facing=False,
            feature_keys_json=[f"dataset:{metadata.name}:recent_encounter"],
            base_score=0.45,
            final_score=0.45,
            risk_reason="synthetic_dataset_recent_encounter",
            created_by_id=created_by_id,
            created_at=imported.entry.occurred_at,
        )
    )
    session.flush()
    prefix = content[max(0, start - 32) : start]
    suffix = content[end : end + 32]
    session.add(
        ProvenancePointer(
            id=pointer_id,
            clinic_id=clinic_id,
            highlight_id=highlight_id,
            entry_version_id=imported.version.id,
            start_offset=start,
            end_offset=end,
            exact_quote_ciphertext=field_codec.encrypt_text(
                clinic_id, "provenance.exact_quote", pointer_id, exact_quote
            ),
            prefix_ciphertext=field_codec.encrypt_text(
                clinic_id, "provenance.prefix", pointer_id, prefix
            ),
            suffix_ciphertext=field_codec.encrypt_text(
                clinic_id, "provenance.suffix", pointer_id, suffix
            ),
            quote_sha256=sha256_text(exact_quote),
            created_at=imported.entry.occurred_at,
        )
    )
    return True


def _resource_lines(
    label: str, rows: list[dict[str, str]], *, limit: int = 6
) -> list[str]:
    values: list[str] = []
    for row in rows[:limit]:
        description = row.get("DESCRIPTION", "").strip()
        if not description:
            continue
        value = row.get("VALUE", "").strip()
        units = row.get("UNITS", "").strip()
        rendered = description
        if value:
            rendered += f": {value}{(' ' + units) if units else ''}"
        if rendered not in values:
            values.append(rendered)
    return [f"{label}: " + "; ".join(values)] if values else []


def import_synthea(
    session: Session,
    *,
    root: Path,
    metadata: DatasetMetadata,
    limit: int,
    encounters_per_patient: int = 5,
) -> ImportCounts:
    counts = ImportCounts()
    clinic_id = demo_id("clinic-primary")
    worker_id = demo_id("user-worker")
    clinician = session.get(User, demo_id("user-clinician"))
    membership = session.get(ClinicMembership, demo_id("membership-clinician"))
    if clinician is None or membership is None:
        raise RuntimeError("Primary demo clinic must be seeded before dataset import")

    patients = sorted(_csv_rows(root / "patients.csv"), key=lambda row: row["Id"])[
        :limit
    ]
    patient_ids = {row["Id"] for row in patients}
    encounters = [
        row
        for row in _csv_rows(root / "encounters.csv")
        if row["PATIENT"] in patient_ids
    ]
    resources: dict[str, dict[str, list[dict[str, str]]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for filename, label in (
        ("conditions.csv", "Conditions"),
        ("medications.csv", "Medications"),
        ("allergies.csv", "Allergies"),
        ("observations.csv", "Observations"),
    ):
        for row in _csv_rows(root / filename):
            if row.get("PATIENT") in patient_ids and row.get("ENCOUNTER"):
                resources[row["ENCOUNTER"]][label].append(row)

    by_patient: dict[str, list[dict[str, str]]] = defaultdict(list)
    for encounter in encounters:
        by_patient[encounter["PATIENT"]].append(encounter)

    for index, patient_row in enumerate(patients, start=1):
        source_patient_id = patient_row["Id"]
        patient, created = _patient(
            session,
            metadata=metadata,
            clinic_id=clinic_id,
            actor_id=worker_id,
            source_id=source_patient_id,
            display_name=f"Synthea Patient {index:03d}",
        )
        counts.patients_created += int(created)
        demographics = "\n".join(
            [
                "Synthetic Synthea demographics benchmark record.",
                f"Birth date: {patient_row.get('BIRTHDATE') or 'unknown'}",
                f"Gender: {patient_row.get('GENDER') or 'unknown'}",
                f"Race: {patient_row.get('RACE') or 'unknown'}",
                f"Ethnicity: {patient_row.get('ETHNICITY') or 'unknown'}",
                "This imported record is synthetic and is not a model output.",
            ]
        )
        demographic_entry = _entry(
            session,
            metadata=metadata,
            clinic_id=clinic_id,
            patient_id=patient.id,
            author_id=worker_id,
            source_id=f"patient:{source_patient_id}:demographics",
            section="system",
            origin="system",
            entry_type="system_record",
            title="Synthea demographics",
            content=demographics,
            occurred_at=_safe_datetime(patient_row.get("BIRTHDATE", ""), IMPORT_TIME),
        )
        counts.entries_created += int(demographic_entry.created)

        selected_encounters = sorted(
            by_patient[source_patient_id], key=lambda row: row["START"], reverse=True
        )[:encounters_per_patient]
        newest_entry: ImportedEntry | None = None
        newest_quote = ""
        for encounter in selected_encounters:
            description = encounter.get("DESCRIPTION", "").strip() or "Encounter"
            reason = encounter.get("REASONDESCRIPTION", "").strip()
            content_lines = [
                "Synthetic Synthea longitudinal encounter.",
                f"Encounter: {description}",
                f"Class: {encounter.get('ENCOUNTERCLASS') or 'unknown'}",
            ]
            if reason:
                content_lines.append(f"Reason: {reason}")
            for label in ("Conditions", "Medications", "Allergies", "Observations"):
                content_lines.extend(
                    _resource_lines(label, resources[encounter["Id"]][label])
                )
            content_lines.append(
                "Imported benchmark data; not a Nightingale clinical recommendation."
            )
            imported = _entry(
                session,
                metadata=metadata,
                clinic_id=clinic_id,
                patient_id=patient.id,
                author_id=worker_id,
                source_id=f"encounter:{encounter['Id']}",
                section="system",
                origin="system",
                entry_type="system_record",
                title=f"Synthea encounter · {description}",
                content="\n".join(content_lines),
                occurred_at=_safe_datetime(encounter["START"], IMPORT_TIME),
            )
            counts.entries_created += int(imported.created)
            if newest_entry is None:
                newest_entry = imported
                newest_quote = f"Encounter: {description}"
        if newest_entry is not None:
            counts.highlights_created += int(
                _highlight(
                    session,
                    metadata=metadata,
                    clinic_id=clinic_id,
                    patient_id=patient.id,
                    created_by_id=clinician.id,
                    source_id=f"patient:{source_patient_id}:recent",
                    imported=newest_entry,
                    exact_quote=newest_quote,
                )
            )
        session.flush()
        rebuild_glance(
            session,
            RequestContext(user=clinician, membership=membership),
            patient.id,
        )
    session.commit()
    return counts


def import_aci_bench(
    session: Session,
    *,
    root: Path,
    metadata: DatasetMetadata,
    limit: int,
) -> ImportCounts:
    counts = ImportCounts()
    clinic_id = demo_id("clinic-primary")
    worker_id = demo_id("user-worker")
    corpus = root / "aci-bench-corpus/challenge_data"
    records = sorted(
        _csv_rows(corpus / "train.csv"), key=lambda row: row["encounter_id"]
    )[:limit]
    metadata_rows = {
        row["encounter_id"]: row for row in _csv_rows(corpus / "train_metadata.csv")
    }
    for index, record in enumerate(records, start=1):
        encounter_id = record["encounter_id"]
        patient, created = _patient(
            session,
            metadata=metadata,
            clinic_id=clinic_id,
            actor_id=worker_id,
            source_id=encounter_id,
            display_name=f"ACI-Bench Patient {encounter_id}",
        )
        counts.patients_created += int(created)
        meta = metadata_rows.get(encounter_id, {})
        occurred_at = datetime(2026, 3, 1, tzinfo=UTC) + timedelta(days=index - 1)
        source = _entry(
            session,
            metadata=metadata,
            clinic_id=clinic_id,
            patient_id=patient.id,
            author_id=worker_id,
            source_id=f"{encounter_id}:dialogue",
            section="system",
            origin="system",
            entry_type="voice_transcript_source",
            title=f"ACI-Bench dialogue · {meta.get('cc') or encounter_id}",
            content=(
                record["dialogue"]
                + "\n\n[Imported synthetic benchmark dialogue; not a live transcript.]"
            ),
            occurred_at=occurred_at,
        )
        reference = _entry(
            session,
            metadata=metadata,
            clinic_id=clinic_id,
            patient_id=patient.id,
            author_id=worker_id,
            source_id=f"{encounter_id}:reference-note",
            section="system",
            origin="ai",
            entry_type="ai_doctor_consult_summary",
            title="ACI-Bench reference note · imported benchmark, not model output",
            content=record["note"],
            occurred_at=occurred_at + timedelta(minutes=1),
        )
        counts.entries_created += int(source.created) + int(reference.created)
        counts.relations_created += int(
            _relation(
                session,
                metadata=metadata,
                clinic_id=clinic_id,
                source_entry_id=reference.entry.id,
                target_entry_id=source.entry.id,
                created_by_id=worker_id,
                source_id=encounter_id,
            )
        )
    session.commit()
    return counts


_TEXTGRID_INTERVAL = re.compile(r"^\s*intervals \[\d+\]:\s*$")
_TEXTGRID_FIELD = re.compile(r"^\s*(xmin|xmax|text)\s*=\s*(.*)\s*$")
_TAG = re.compile(r"</?UNSURE>|<UNIN/>")


def parse_textgrid(path: Path, speaker: str) -> list[ReferenceSegment]:
    lines = path.read_text(encoding="utf-8").splitlines()
    segments: list[ReferenceSegment] = []
    current: dict[str, str] | None = None

    def append_current(values: dict[str, str] | None) -> None:
        if values is None or not {"xmin", "xmax", "text"}.issubset(values):
            return
        raw = values["text"].strip()
        if raw.startswith('"') and raw.endswith('"'):
            raw = raw[1:-1].replace('""', '"')
        text = " ".join(_TAG.sub("", raw).split())
        if not text:
            return
        segments.append(
            ReferenceSegment(
                speaker=speaker,
                text=text,
                start_ms=max(0, round(float(values["xmin"]) * 1000)),
                end_ms=max(0, round(float(values["xmax"]) * 1000)),
            )
        )

    for line in lines:
        if _TEXTGRID_INTERVAL.match(line):
            append_current(current)
            current = {}
            continue
        if current is None:
            continue
        field = _TEXTGRID_FIELD.match(line)
        if field:
            current[field.group(1)] = field.group(2)
    append_current(current)
    return segments


def merge_reference_segments(
    doctor: list[ReferenceSegment], patient: list[ReferenceSegment]
) -> list[ReferenceSegment]:
    ordered = sorted(
        [*doctor, *patient], key=lambda item: (item.start_ms, item.speaker)
    )
    result: list[ReferenceSegment] = []
    max_end = 0
    overlap_index = 0
    for segment in ordered:
        overlap: str | None = None
        if segment.start_ms < max_end:
            overlap_index += 1
            overlap = f"reference-overlap-{overlap_index}"
        result.append(
            ReferenceSegment(
                speaker=segment.speaker,
                text=segment.text,
                start_ms=segment.start_ms,
                end_ms=segment.end_ms,
                overlap_group_id=overlap,
            )
        )
        max_end = max(max_end, segment.end_ms)
    return result


def stereo_wav(doctor_path: Path, patient_path: Path) -> tuple[bytes, int]:
    def read_mono(path: Path) -> tuple[array[int], int]:
        with wave.open(str(path), "rb") as source:
            if (
                source.getnchannels() != 1
                or source.getsampwidth() != 2
                or source.getframerate() != 16_000
            ):
                raise ValueError(f"Unsupported PriMock57 WAV format: {path}")
            samples = array("h")
            samples.frombytes(source.readframes(source.getnframes()))
            if sys.byteorder != "little":
                samples.byteswap()
            return samples, source.getframerate()

    doctor, rate = read_mono(doctor_path)
    patient, patient_rate = read_mono(patient_path)
    if rate != patient_rate:
        raise ValueError("PriMock57 track sample rates do not match")
    frames = max(len(doctor), len(patient))
    if len(doctor) < frames:
        doctor.extend([0] * (frames - len(doctor)))
    if len(patient) < frames:
        patient.extend([0] * (frames - len(patient)))
    interleaved = array("h")
    for left, right in zip(doctor, patient, strict=True):
        interleaved.append(left)
        interleaved.append(right)
    if sys.byteorder != "little":
        interleaved.byteswap()
    output = io.BytesIO()
    with wave.open(output, "wb") as target:
        target.setnchannels(2)
        target.setsampwidth(2)
        target.setframerate(rate)
        target.writeframes(interleaved.tobytes())
    return output.getvalue(), round(frames / rate * 1000)


def _voice_session(
    session: Session,
    *,
    metadata: DatasetMetadata,
    clinic_id: uuid.UUID,
    patient_id: uuid.UUID,
    actor_id: uuid.UUID,
    source_id: str,
    segments: list[ReferenceSegment],
    doctor_audio: Path | None,
    patient_audio: Path | None,
) -> tuple[VoiceSession, int, bool, bool]:
    session_id = stable_id(metadata.name, "voice-session", source_id)
    revision_id = stable_id(metadata.name, "transcript-revision", source_id)
    existing = session.get(VoiceSession, session_id)
    if existing is not None:
        segment_count = len(
            session.exec(
                select(TranscriptSegment).where(
                    TranscriptSegment.revision_id == revision_id
                )
            ).all()
        )
        asset = session.exec(
            select(AudioAsset).where(AudioAsset.session_id == session_id)
        ).first()
        return existing, segment_count, False, asset is not None

    rendered = [f"[{segment.speaker}] {segment.text}" for segment in segments]
    transcript_text = "\n".join(rendered)
    voice_session = VoiceSession(
        id=session_id,
        clinic_id=clinic_id,
        patient_id=patient_id,
        capture_kind="clinical",
        state="ready",
        synthetic_fixture=True,
        fixture_id=f"primock57:{source_id}",
        created_by_id=actor_id,
        current_transcript_revision_id=None,
        warning_codes_json=["IMPORTED_REFERENCE_TRANSCRIPT"],
        created_at=IMPORT_TIME,
        updated_at=IMPORT_TIME,
    )
    session.add(voice_session)
    session.flush()
    revision = TranscriptRevision(
        id=revision_id,
        clinic_id=clinic_id,
        session_id=session_id,
        revision_no=1,
        text_ciphertext=field_codec.encrypt_text(
            clinic_id, "transcript_revision.text", revision_id, transcript_text
        ),
        text_sha256=sha256_text(transcript_text),
        provider="primock57-human-reference",
        model=metadata.version,
        detected_language="en-GB",
        status="ready",
        warning_codes_json=["IMPORTED_REFERENCE_TRANSCRIPT"],
        created_at=IMPORT_TIME,
    )
    session.add(revision)
    session.flush()
    cursor = 0
    for ordinal, (segment, rendered_text) in enumerate(
        zip(segments, rendered, strict=True)
    ):
        segment_id = stable_id(
            metadata.name, "transcript-segment", f"{source_id}:{ordinal}"
        )
        text_start = cursor
        text_end = cursor + len(rendered_text)
        session.add(
            TranscriptSegment(
                id=segment_id,
                clinic_id=clinic_id,
                session_id=session_id,
                revision_id=revision_id,
                ordinal=ordinal,
                text_ciphertext=field_codec.encrypt_text(
                    clinic_id, "transcript_segment.text", segment_id, rendered_text
                ),
                text_sha256=sha256_text(rendered_text),
                text_start=text_start,
                text_end=text_end,
                start_ms=segment.start_ms,
                end_ms=segment.end_ms,
                speaker_id=segment.speaker,
                detected_language="en-GB",
                confidence=1.0,
                confidence_source="human-aligned-reference",
                overlap_group_id=segment.overlap_group_id,
                provider="primock57-human-reference",
                model=metadata.version,
            )
        )
        cursor = text_end + 1
    voice_session.current_transcript_revision_id = revision_id
    session.add(voice_session)
    audio_created = False
    if doctor_audio is not None and patient_audio is not None:
        asset_id = stable_id(metadata.name, "audio-asset", source_id)
        payload, duration_ms = stereo_wav(doctor_audio, patient_audio)
        session.add(
            AudioAsset(
                id=asset_id,
                clinic_id=clinic_id,
                session_id=session_id,
                payload_ciphertext=field_codec.encrypt(
                    clinic_id, "audio_asset.payload", asset_id, payload
                ),
                plaintext_sha256=hashlib.sha256(payload).hexdigest(),
                duration_ms=duration_ms,
                media_type="audio/wav",
                sample_rate_hz=16_000,
                channels=2,
                preprocessing_json={
                    "dataset": metadata.name,
                    "version": metadata.version,
                    "channel_1": "doctor",
                    "channel_2": "patient",
                    "synthetic_mock_consultation": True,
                },
                created_at=IMPORT_TIME,
            )
        )
        audio_created = True
    return voice_session, len(segments), True, audio_created


def import_primock57(
    session: Session,
    *,
    root: Path,
    audio_root: Path,
    metadata: DatasetMetadata,
    limit: int,
) -> ImportCounts:
    counts = ImportCounts()
    clinic_id = demo_id("clinic-primary")
    worker_id = demo_id("user-worker")
    extracted_roots = sorted(path for path in root.iterdir() if path.is_dir())
    if len(extracted_roots) != 1:
        raise ValueError("PriMock57 archive must contain exactly one repository root")
    repository = extracted_roots[0]
    notes = sorted((repository / "notes").glob("day*_consultation*.json"))[:limit]
    for index, note_path in enumerate(notes, start=1):
        source_id = note_path.stem
        note_data: dict[str, Any] = json.loads(note_path.read_text(encoding="utf-8"))
        doctor_grid = repository / "transcripts" / f"{source_id}_doctor.TextGrid"
        patient_grid = repository / "transcripts" / f"{source_id}_patient.TextGrid"
        segments = merge_reference_segments(
            parse_textgrid(doctor_grid, "Doctor"),
            parse_textgrid(patient_grid, "Patient"),
        )
        patient, created = _patient(
            session,
            metadata=metadata,
            clinic_id=clinic_id,
            actor_id=worker_id,
            source_id=source_id,
            display_name=f"PriMock57 Patient {index:02d}",
        )
        counts.patients_created += int(created)
        transcript_text = "\n".join(
            f"[{segment.speaker}] {segment.text}" for segment in segments
        )
        occurred_at = datetime(2026, 4, 1, tzinfo=UTC) + timedelta(days=index - 1)
        transcript_entry = _entry(
            session,
            metadata=metadata,
            clinic_id=clinic_id,
            patient_id=patient.id,
            author_id=worker_id,
            source_id=f"{source_id}:transcript",
            section="system",
            origin="system",
            entry_type="voice_transcript_source",
            title=f"PriMock57 transcript · {note_data.get('presenting_complaint', source_id)}",
            content=transcript_text,
            occurred_at=occurred_at,
        )
        reference_note = _entry(
            session,
            metadata=metadata,
            clinic_id=clinic_id,
            patient_id=patient.id,
            author_id=worker_id,
            source_id=f"{source_id}:reference-note",
            section="system",
            origin="system",
            entry_type="voice_reviewed_result",
            title="PriMock57 clinician reference note · imported benchmark",
            content=str(note_data.get("note", "")),
            occurred_at=occurred_at + timedelta(minutes=1),
        )
        counts.entries_created += int(transcript_entry.created) + int(
            reference_note.created
        )
        counts.relations_created += int(
            _relation(
                session,
                metadata=metadata,
                clinic_id=clinic_id,
                source_entry_id=reference_note.entry.id,
                target_entry_id=transcript_entry.entry.id,
                created_by_id=worker_id,
                source_id=source_id,
            )
        )
        doctor_audio = audio_root / f"{source_id}_doctor.wav"
        patient_audio = audio_root / f"{source_id}_patient.wav"
        voice_session, segment_count, session_created, audio_created = _voice_session(
            session,
            metadata=metadata,
            clinic_id=clinic_id,
            patient_id=patient.id,
            actor_id=worker_id,
            source_id=source_id,
            segments=segments,
            doctor_audio=doctor_audio if doctor_audio.is_file() else None,
            patient_audio=patient_audio if patient_audio.is_file() else None,
        )
        counts.sessions_created += int(session_created)
        counts.transcript_segments_created += segment_count if session_created else 0
        counts.audio_assets_created += int(audio_created and session_created)
        _source_event(
            session,
            metadata=metadata,
            clinic_id=clinic_id,
            actor_id=worker_id,
            aggregate_type="voice_session",
            aggregate_id=voice_session.id,
            source_record_id=source_id,
            source_sha256=sha256_text(transcript_text + str(note_data.get("note", ""))),
            transformation="nightingale-primock57-reference-import-v1",
        )
    session.commit()
    return counts


def import_evaluation_pack(
    session: Session,
    *,
    raw_root: Path,
    manifest_path: Path,
    synthea_limit: int = 20,
    aci_limit: int = 10,
    primock_limit: int = 5,
) -> dict[str, object]:
    metadata = load_dataset_metadata(manifest_path)
    result: dict[str, object] = {}
    synthea = import_synthea(
        session,
        root=raw_root / "synthea/extracted",
        metadata=metadata["synthea"],
        limit=synthea_limit,
    )
    result["synthea"] = synthea.to_dict()
    aci = import_aci_bench(
        session,
        root=raw_root / "aci_bench/extracted",
        metadata=metadata["aci_bench"],
        limit=aci_limit,
    )
    result["aci_bench"] = aci.to_dict()
    primock = import_primock57(
        session,
        root=raw_root / "primock57/extracted",
        audio_root=raw_root / "primock57/audio",
        metadata=metadata["primock57"],
        limit=primock_limit,
    )
    result["primock57"] = primock.to_dict()
    total = ImportCounts()
    total.add(synthea)
    total.add(aci)
    total.add(primock)
    result["total"] = total.to_dict()
    return result
