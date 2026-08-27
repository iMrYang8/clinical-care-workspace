from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass

from sqlmodel import Session, select

from app.api.deps import RequestContext
from app.core.field_crypto import field_codec
from app.models import (
    ClinicalFactAssertion,
    ConflictCase,
    Entry,
    EntryVersion,
    Highlight,
    ProvenancePointer,
)
from app.services.decisioning import create_assertion


@dataclass(frozen=True)
class NormalizedFact:
    fact_type: str
    key: str
    value: str
    start: int
    end: int
    quote: str


_ALLERGY_PATTERNS = (
    (
        re.compile(
            r"\bno\s+(?:known\s+)?allerg(?:y|ic)\s+to\s+([a-z][a-z0-9-]+)", re.I
        ),
        "absent",
    ),
    (re.compile(r"\b(?:allergic to|allergy to)\s+([a-z][a-z0-9-]+)", re.I), "present"),
    (re.compile(r"\b([a-z][a-z0-9-]+)\s+allergy\b", re.I), "present"),
)
_MEDICATION = re.compile(
    r"\b(started|start|taking|continue|continued|stopped|stop|discontinued)\s+"
    r"([a-z][a-z0-9-]{2,})\b",
    re.I,
)
_DOSE = re.compile(
    r"\b([a-z][a-z0-9-]{2,})\s+(\d+(?:\.\d+)?)\s*(mg|mcg|g|ml)"
    r"(?:\s+(po|oral|iv|intravenous|im|subcutaneous))?"
    r"(?:\s+(daily|once daily|twice daily|three times daily|bid|tid|qid|qd))?\b",
    re.I,
)

_ROUTES = {
    "po": "oral",
    "oral": "oral",
    "iv": "intravenous",
    "intravenous": "intravenous",
    "im": "intramuscular",
    "subcutaneous": "subcutaneous",
}
_FREQUENCIES = {
    "qd": "once_daily",
    "daily": "once_daily",
    "once daily": "once_daily",
    "bid": "twice_daily",
    "twice daily": "twice_daily",
    "tid": "three_times_daily",
    "three times daily": "three_times_daily",
    "qid": "four_times_daily",
}


def _dose_value(value: str, unit: str) -> str:
    numeric = float(value)
    lowered = unit.lower()
    if lowered == "g":
        numeric *= 1_000
        lowered = "mg"
    elif lowered == "mcg":
        numeric /= 1_000
        lowered = "mg"
    return f"{numeric:g}{lowered}"


def _dose_number(value: str) -> float | None:
    match = re.match(r"[0-9.]+", value)
    return float(match.group(0)) if match else None


def extract_normalized_facts(content: str) -> list[NormalizedFact]:
    facts: list[NormalizedFact] = []
    for pattern, value in _ALLERGY_PATTERNS:
        for match in pattern.finditer(content):
            if value == "present" and re.search(
                r"\bno\s+(?:known\s+)?$",
                content[max(0, match.start() - 12) : match.start()],
                re.I,
            ):
                continue
            facts.append(
                NormalizedFact(
                    fact_type="allergy",
                    key=match.group(1).lower(),
                    value=value,
                    start=match.start(),
                    end=match.end(),
                    quote=match.group(0),
                )
            )
    for match in _DOSE.finditer(content):
        drug = match.group(1).lower()
        quote = match.group(0)
        facts.append(
            NormalizedFact(
                fact_type="dose",
                key=drug,
                value=_dose_value(match.group(2), match.group(3)),
                start=match.start(),
                end=match.end(),
                quote=quote,
            )
        )
        if match.group(4):
            facts.append(
                NormalizedFact(
                    fact_type="route",
                    key=drug,
                    value=_ROUTES[match.group(4).lower()],
                    start=match.start(),
                    end=match.end(),
                    quote=quote,
                )
            )
        if match.group(5):
            facts.append(
                NormalizedFact(
                    fact_type="frequency",
                    key=drug,
                    value=_FREQUENCIES[match.group(5).lower()],
                    start=match.start(),
                    end=match.end(),
                    quote=quote,
                )
            )
    for match in _MEDICATION.finditer(content):
        action = match.group(1).lower()
        facts.append(
            NormalizedFact(
                fact_type="medication",
                key=match.group(2).lower(),
                value=(
                    "stopped"
                    if action in {"stopped", "stop", "discontinued"}
                    else "active"
                ),
                start=match.start(),
                end=match.end(),
                quote=match.group(0),
            )
        )
    # Exact duplicate regex matches are harmless; retain a single span/value.
    return list(
        {
            (fact.fact_type, fact.key, fact.value, fact.start, fact.end): fact
            for fact in facts
        }.values()
    )


def _pointer(
    session: Session,
    context: RequestContext,
    version: EntryVersion,
    content: str,
    fact: NormalizedFact,
) -> ProvenancePointer:
    pointer_id = uuid.uuid4()
    prefix = content[max(0, fact.start - 40) : fact.start]
    suffix = content[fact.end : fact.end + 40]
    pointer = ProvenancePointer(
        id=pointer_id,
        clinic_id=context.clinic_id,
        entry_version_id=version.id,
        start_offset=fact.start,
        end_offset=fact.end,
        exact_quote_ciphertext=field_codec.encrypt_text(
            context.clinic_id,
            "provenance.exact_quote",
            pointer_id,
            fact.quote,
        ),
        prefix_ciphertext=field_codec.encrypt_text(
            context.clinic_id, "provenance.prefix", pointer_id, prefix
        ),
        suffix_ciphertext=field_codec.encrypt_text(
            context.clinic_id, "provenance.suffix", pointer_id, suffix
        ),
        quote_sha256=hashlib.sha256(fact.quote.encode()).hexdigest(),
    )
    session.add(pointer)
    session.flush()
    return pointer


def detect_conflicts_for_version(
    session: Session,
    context: RequestContext,
    entry: Entry,
    version: EntryVersion,
    content: str,
) -> list[ConflictCase]:
    new_facts = extract_normalized_facts(content)
    if not new_facts:
        return []
    new_pointers: dict[tuple[str, str, str, int, int], ProvenancePointer] = {}
    for fact in new_facts:
        pointer = _pointer(session, context, version, content, fact)
        new_pointers[(fact.fact_type, fact.key, fact.value, fact.start, fact.end)] = (
            pointer
        )
        assertion = create_assertion(
            session,
            clinic_id=context.clinic_id,
            patient_id=entry.patient_id,
            entry_id=entry.id,
            source_entry_version_id=version.id,
            provenance_pointer=pointer,
            fact_type=fact.fact_type,
            subject=fact.key,
            normalized_value=fact.value,
            origin=entry.origin,
            polarity=(fact.value if fact.fact_type == "allergy" else "present"),
            clinical_status=(
                fact.value if fact.fact_type == "medication" else "active"
            ),
            medication=(
                fact.key
                if fact.fact_type in {"medication", "dose", "route", "frequency"}
                else None
            ),
            dose_value=_dose_number(fact.value) if fact.fact_type == "dose" else None,
            dose_unit=(
                re.sub(r"[0-9.]", "", fact.value) if fact.fact_type == "dose" else None
            ),
            route=(fact.value if fact.fact_type == "route" else None),
            frequency=(fact.value if fact.fact_type == "frequency" else None),
        )
        detect_conflicts_for_assertion(session, context, assertion)
    existing_entries = session.exec(
        select(Entry).where(
            Entry.clinic_id == context.clinic_id,
            Entry.patient_id == entry.patient_id,
            Entry.id != entry.id,
        )
    ).all()
    created: list[ConflictCase] = []
    from app.services.nightingale import decrypt_version

    for other in existing_entries:
        if other.current_version_id is None:
            continue
        other_version = session.get(EntryVersion, other.current_version_id)
        if other_version is None or other_version.clinic_id != context.clinic_id:
            continue
        _, other_content = decrypt_version(other_version)
        other_facts = extract_normalized_facts(other_content)
        for left in other_facts:
            for right in new_facts:
                if (
                    left.fact_type != right.fact_type
                    or left.key != right.key
                    or left.value == right.value
                ):
                    continue
                duplicate = session.exec(
                    select(ConflictCase).where(
                        ConflictCase.clinic_id == context.clinic_id,
                        ConflictCase.patient_id == entry.patient_id,
                        ConflictCase.left_version_id == other_version.id,
                        ConflictCase.right_version_id == version.id,
                        ConflictCase.fact_type == right.fact_type,
                        ConflictCase.normalized_key == right.key,
                    )
                ).first()
                if duplicate is not None:
                    continue
                left_pointer = _pointer(
                    session, context, other_version, other_content, left
                )
                right_pointer = new_pointers[
                    (right.fact_type, right.key, right.value, right.start, right.end)
                ]
                conflict = ConflictCase(
                    clinic_id=context.clinic_id,
                    patient_id=entry.patient_id,
                    left_entry_id=other.id,
                    right_entry_id=entry.id,
                    fact_type=right.fact_type,
                    normalized_key=right.key,
                    left_version_id=other_version.id,
                    right_version_id=version.id,
                    left_pointer_id=left_pointer.id,
                    right_pointer_id=right_pointer.id,
                    severity="critical" if right.fact_type == "allergy" else "high",
                    status="unresolved",
                )
                session.add(conflict)
                created.append(conflict)
    return created


def _assertion_text(assertion: ClinicalFactAssertion, field: str) -> str:
    payload = (
        assertion.subject_ciphertext
        if field == "subject"
        else assertion.normalized_value_ciphertext
    )
    return field_codec.decrypt_text(
        assertion.clinic_id,
        f"fact_assertion.{field}",
        assertion.id,
        payload,
    )


def detect_conflicts_for_assertion(
    session: Session,
    context: RequestContext,
    assertion: ClinicalFactAssertion,
) -> list[ConflictCase]:
    """Compare a newly persisted Human/AI/Voice assertion across all origins."""

    subject = _assertion_text(assertion, "subject").lower()
    value = _assertion_text(assertion, "normalized_value").lower()
    created: list[ConflictCase] = []
    candidates = session.exec(
        select(ClinicalFactAssertion).where(
            ClinicalFactAssertion.clinic_id == context.clinic_id,
            ClinicalFactAssertion.patient_id == assertion.patient_id,
            ClinicalFactAssertion.fact_type == assertion.fact_type,
            ClinicalFactAssertion.id != assertion.id,
        )
    ).all()
    for other in candidates:
        if _assertion_text(other, "subject").lower() != subject:
            continue
        if _assertion_text(other, "normalized_value").lower() == value:
            continue
        left, right = sorted((other, assertion), key=lambda item: str(item.id))
        possible_duplicates = session.exec(
            select(ConflictCase).where(
                ConflictCase.clinic_id == context.clinic_id,
                ConflictCase.patient_id == assertion.patient_id,
                ConflictCase.fact_type == assertion.fact_type,
                ConflictCase.normalized_key == subject,
                ConflictCase.status == "unresolved",
            )
        ).all()
        version_pair = {left.source_entry_version_id, right.source_entry_version_id}
        if any(
            {item.left_version_id, item.right_version_id} == version_pair
            for item in possible_duplicates
        ):
            continue
        conflict = ConflictCase(
            clinic_id=context.clinic_id,
            patient_id=assertion.patient_id,
            left_entry_id=left.entry_id,
            right_entry_id=right.entry_id,
            fact_type=assertion.fact_type,
            normalized_key=subject,
            left_version_id=left.source_entry_version_id,
            right_version_id=right.source_entry_version_id,
            left_pointer_id=left.provenance_pointer_id,
            right_pointer_id=right.provenance_pointer_id,
            left_assertion_id=left.id,
            right_assertion_id=right.id,
            severity="critical" if assertion.fact_type == "allergy" else "high",
            status="unresolved",
        )
        session.add(conflict)
        created.append(conflict)
        for candidate in (left, right):
            if candidate.highlight_id is None:
                continue
            highlight = session.get(Highlight, candidate.highlight_id)
            if highlight is None or highlight.clinic_id != context.clinic_id:
                continue
            highlight.unresolved = True
            if assertion.fact_type == "allergy":
                highlight.critical = True
                if "risk:critical" not in highlight.feature_keys_json:
                    highlight.feature_keys_json = [
                        *highlight.feature_keys_json,
                        "risk:critical",
                    ]
            session.add(highlight)
    return created
