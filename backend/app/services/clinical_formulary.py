"""Bounded, versioned formulary screening for deterministic clinical fixtures.

The formulary is deliberately small and fail closed.  It is not a prescribing
engine: it only decides whether a structured medication candidate is complete
and falls within the clinic-approved screening envelope before a clinician may
publish it.  Unknown concepts, aliases, units, routes, frequencies, and dose
ranges always require review.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal, cast

from sqlmodel import Session, col, select

from app.models import (
    ClinicFormularyConcept,
    ClinicFormularyConceptCreate,
    ClinicFormularyConceptPublic,
    ClinicFormularyReadinessPublic,
    ClinicFormularyVersion,
    ClinicFormularyVersionCreate,
    ClinicFormularyVersionPublic,
    get_datetime_utc,
)

FORMULARY_VERSION = "nightingale-clinic-formulary-v1"
ALLERGY_CONCEPT_MAP_VERSION = "nightingale-allergy-concepts-v1"
_SUPPORTED_ALIAS_LANGUAGES = {"en", "ms", "nan", "zh"}
_PERMITTED_CONFIGURATION_ROUTES = {
    "oral",
    "intravenous",
    "intramuscular",
    "subcutaneous",
}
_PERMITTED_CONFIGURATION_UNITS = {"mg", "ml"}
_VERSION_CODE = re.compile(r"^[a-z0-9][a-z0-9._-]{2,79}$")
_CONCEPT_CODE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:._-]{2,99}$")
_ALLERGY_CODE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:._-]{0,99}$")


class FormularyConfigurationError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class FormularyConcept:
    code: str
    display_name: str
    aliases: tuple[str, ...]
    dose_unit: str
    minimum_single_dose: float
    maximum_single_dose: float
    permitted_routes: frozenset[str]
    contraindicated_allergy_concepts: frozenset[str] = frozenset()


AllergyCategory = Literal["drug", "food", "environmental"]


@dataclass(frozen=True)
class AuditedAllergyConcept:
    """A bounded concept whose category was explicitly reviewed.

    Category assignment is exact-alias based.  Narrative text, partial matches,
    and concepts outside this table remain unavailable instead of being guessed.
    """

    canonical_name: str
    category: AllergyCategory
    aliases: tuple[str, ...]


@dataclass(frozen=True)
class MedicationScreeningResult:
    formulary_version: str
    qualification_source: Literal["deployment_fixture", "clinic_version"]
    state: Literal["qualified", "review_required"]
    concept_code: str | None
    canonical_name: str | None
    reason_codes: tuple[str, ...]

    @property
    def eligible(self) -> bool:
        return self.state == "qualified"


_CONCEPTS: tuple[FormularyConcept, ...] = (
    FormularyConcept(
        code="rxnorm:860975",
        display_name="metformin",
        aliases=(
            "metformin",
            "二甲双胍",
            "二甲雙胍",
            "er jia shuang gua",
        ),
        dose_unit="mg",
        minimum_single_dose=250,
        maximum_single_dose=1_000,
        permitted_routes=frozenset({"oral"}),
    ),
    FormularyConcept(
        code="rxnorm:723",
        display_name="amoxicillin",
        aliases=(
            "amoxicillin",
            "amoksisilin",
            "a mo xi lin",
            "阿莫西林",
        ),
        dose_unit="mg",
        minimum_single_dose=125,
        maximum_single_dose=1_000,
        permitted_routes=frozenset({"oral"}),
        contraindicated_allergy_concepts=frozenset(
            {"penicillin", "allergy:penicillin"}
        ),
    ),
    FormularyConcept(
        code="rxnorm:1191",
        display_name="aspirin",
        aliases=("aspirin", "asipirin", "a si pi lin", "阿司匹林"),
        dose_unit="mg",
        minimum_single_dose=50,
        maximum_single_dose=1_000,
        permitted_routes=frozenset({"oral"}),
        contraindicated_allergy_concepts=frozenset({"aspirin", "allergy:aspirin"}),
    ),
)

_AUDITED_ALLERGY_CONCEPTS: tuple[AuditedAllergyConcept, ...] = (
    AuditedAllergyConcept(
        canonical_name="penicillin",
        category="drug",
        aliases=("penicillin", "penisilin", "青霉素", "青黴素"),
    ),
    AuditedAllergyConcept(
        canonical_name="clindamycin",
        category="drug",
        aliases=("clindamycin", "klindamisin"),
    ),
    AuditedAllergyConcept(
        canonical_name="amoxicillin",
        category="drug",
        aliases=("amoxicillin", "amoksisilin"),
    ),
    AuditedAllergyConcept(
        canonical_name="aspirin",
        category="drug",
        aliases=("aspirin",),
    ),
    AuditedAllergyConcept(
        canonical_name="peanut",
        category="food",
        aliases=("peanut", "groundnut", "kacang", "花生"),
    ),
    AuditedAllergyConcept(
        canonical_name="latex",
        category="environmental",
        aliases=("latex", "lateks", "乳胶", "乳膠"),
    ),
)

_FORMULARY_TEMPLATES: dict[str, tuple[ClinicFormularyConceptCreate, ...]] = {
    FORMULARY_VERSION: (
        ClinicFormularyConceptCreate(
            concept_code="rxnorm:860975",
            canonical_name="metformin",
            multilingual_aliases={
                "en": ["metformin"],
                "ms": ["metformin"],
                "nan": ["metformin"],
                "zh": ["二甲双胍", "二甲雙胍"],
            },
            dose_unit="mg",
            minimum_single_dose=250,
            maximum_single_dose=1_000,
            permitted_routes=["oral"],
        ),
        ClinicFormularyConceptCreate(
            concept_code="rxnorm:723",
            canonical_name="amoxicillin",
            multilingual_aliases={
                "en": ["amoxicillin"],
                "ms": ["amoksisilin"],
                "nan": ["amoxicillin"],
                "zh": ["阿莫西林"],
            },
            dose_unit="mg",
            minimum_single_dose=125,
            maximum_single_dose=1_000,
            permitted_routes=["oral"],
            contraindicated_allergy_concepts=[
                "penicillin",
                "allergy:penicillin",
            ],
        ),
        ClinicFormularyConceptCreate(
            concept_code="rxnorm:1191",
            canonical_name="aspirin",
            multilingual_aliases={
                "en": ["aspirin"],
                "ms": ["aspirin"],
                "nan": ["asipirin"],
                "zh": ["阿司匹林"],
            },
            dose_unit="mg",
            minimum_single_dose=50,
            maximum_single_dose=1_000,
            permitted_routes=["oral"],
            contraindicated_allergy_concepts=["aspirin", "allergy:aspirin"],
        ),
    )
}


def _fold(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value.strip().casefold())
    accentless = "".join(char for char in decomposed if not unicodedata.combining(char))
    return " ".join(re.sub(r"[^\w\u3400-\u9fff]+", " ", accentless).split())


_ALLERGY_ALIASES = {
    _fold(alias): concept
    for concept in _AUDITED_ALLERGY_CONCEPTS
    for alias in (concept.canonical_name, *concept.aliases)
}


def canonicalize_allergy_concept(value: str) -> AuditedAllergyConcept | None:
    """Resolve only an exact alias from the audited, versioned concept map."""

    candidate = value.strip()
    if candidate.casefold().startswith("allergy:"):
        candidate = candidate.split(":", 1)[1]
    return _ALLERGY_ALIASES.get(_fold(candidate))


def allergy_category_for_assertion(
    subject: str, assertion_scope: str
) -> AllergyCategory | None:
    """Return an assertion category without inferring from narrative wording."""

    if assertion_scope == "drug_allergies":
        return "drug"
    if assertion_scope != "specific_substance":
        return None
    concept = canonicalize_allergy_concept(subject)
    return concept.category if concept is not None else None


def _require_exact_text(value: str, *, code: str) -> str:
    if not value or value != value.strip() or "\x00" in value:
        raise FormularyConfigurationError(code)
    return value


def _validated_configuration_payload(
    concepts: Iterable[ClinicFormularyConceptCreate],
) -> list[dict[str, object]]:
    concept_list = list(concepts)
    if not concept_list:
        raise FormularyConfigurationError("FORMULARY_CONCEPTS_REQUIRED")
    codes: set[str] = set()
    alias_owners: dict[str, str] = {}
    payload: list[dict[str, object]] = []
    for item in concept_list:
        code = _require_exact_text(
            item.concept_code, code="FORMULARY_CONCEPT_CODE_INVALID"
        )
        if not _CONCEPT_CODE.fullmatch(code) or code in codes:
            raise FormularyConfigurationError(
                "FORMULARY_CONCEPT_CODE_DUPLICATE"
                if code in codes
                else "FORMULARY_CONCEPT_CODE_INVALID"
            )
        codes.add(code)
        canonical_name = _require_exact_text(
            item.canonical_name, code="FORMULARY_CANONICAL_NAME_INVALID"
        )
        if set(item.multilingual_aliases) != _SUPPORTED_ALIAS_LANGUAGES:
            raise FormularyConfigurationError(
                "FORMULARY_MULTILINGUAL_ALIASES_INCOMPLETE"
            )
        aliases: dict[str, list[str]] = {}
        for language in sorted(_SUPPORTED_ALIAS_LANGUAGES):
            raw_aliases = item.multilingual_aliases[language]
            if not raw_aliases:
                raise FormularyConfigurationError(
                    "FORMULARY_MULTILINGUAL_ALIASES_INCOMPLETE"
                )
            validated_aliases: list[str] = []
            for raw_alias in raw_aliases:
                alias = _require_exact_text(raw_alias, code="FORMULARY_ALIAS_INVALID")
                folded = _fold(alias)
                if not folded:
                    raise FormularyConfigurationError("FORMULARY_ALIAS_INVALID")
                previous_owner = alias_owners.get(folded)
                if previous_owner is not None and previous_owner != code:
                    raise FormularyConfigurationError("FORMULARY_ALIAS_AMBIGUOUS")
                if alias in validated_aliases:
                    raise FormularyConfigurationError("FORMULARY_ALIAS_DUPLICATE")
                alias_owners[folded] = code
                validated_aliases.append(alias)
            aliases[language] = sorted(validated_aliases)
        canonical_folded = _fold(canonical_name)
        previous_owner = alias_owners.get(canonical_folded)
        if previous_owner is not None and previous_owner != code:
            raise FormularyConfigurationError("FORMULARY_ALIAS_AMBIGUOUS")
        alias_owners[canonical_folded] = code

        if item.dose_unit not in _PERMITTED_CONFIGURATION_UNITS:
            raise FormularyConfigurationError("FORMULARY_DOSE_UNIT_UNKNOWN")
        if (
            isinstance(item.minimum_single_dose, bool)
            or isinstance(item.maximum_single_dose, bool)
            or not math.isfinite(item.minimum_single_dose)
            or not math.isfinite(item.maximum_single_dose)
            or item.minimum_single_dose <= 0
            or item.maximum_single_dose < item.minimum_single_dose
        ):
            raise FormularyConfigurationError("FORMULARY_DOSE_RANGE_INVALID")
        routes = list(item.permitted_routes)
        if (
            not routes
            or len(routes) != len(set(routes))
            or any(route not in _PERMITTED_CONFIGURATION_ROUTES for route in routes)
        ):
            raise FormularyConfigurationError("FORMULARY_ROUTE_UNKNOWN")
        contraindications: list[str] = []
        for raw_concept in item.contraindicated_allergy_concepts:
            allergy_concept = _require_exact_text(
                raw_concept,
                code="FORMULARY_ALLERGY_CONCEPT_INVALID",
            )
            if not _ALLERGY_CODE.fullmatch(allergy_concept):
                raise FormularyConfigurationError("FORMULARY_ALLERGY_CONCEPT_INVALID")
            if allergy_concept in contraindications:
                raise FormularyConfigurationError("FORMULARY_ALLERGY_CONCEPT_DUPLICATE")
            contraindications.append(allergy_concept)
        payload.append(
            {
                "concept_code": code,
                "canonical_name": canonical_name,
                "multilingual_aliases": aliases,
                "dose_unit": item.dose_unit,
                "minimum_single_dose": float(item.minimum_single_dose),
                "maximum_single_dose": float(item.maximum_single_dose),
                "permitted_routes": sorted(routes),
                "contraindicated_allergy_concepts": sorted(contraindications),
            }
        )
    return sorted(payload, key=lambda item: str(item["concept_code"]))


def clinic_formulary_content_sha256(
    concepts: Iterable[ClinicFormularyConceptCreate],
) -> str:
    payload = _validated_configuration_payload(concepts)
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def validate_formulary_template(template: str) -> tuple[str, int]:
    concepts = _FORMULARY_TEMPLATES.get(template)
    if concepts is None:
        raise FormularyConfigurationError("FORMULARY_TEMPLATE_UNKNOWN")
    return clinic_formulary_content_sha256(concepts), len(concepts)


_ALIASES = {
    _fold(alias): concept
    for concept in _CONCEPTS
    for alias in (concept.display_name, *concept.aliases)
}


def formulary_concepts(
    *, version: str = FORMULARY_VERSION
) -> tuple[FormularyConcept, ...]:
    if version != FORMULARY_VERSION:
        return ()
    return _CONCEPTS


def formulary_content_sha256(concepts: Iterable[FormularyConcept]) -> str:
    payload = [
        {
            "code": concept.code,
            "display_name": concept.display_name,
            "aliases": sorted(concept.aliases),
            "dose_unit": concept.dose_unit,
            "minimum_single_dose": concept.minimum_single_dose,
            "maximum_single_dose": concept.maximum_single_dose,
            "permitted_routes": sorted(concept.permitted_routes),
            "contraindicated_allergy_concepts": sorted(
                concept.contraindicated_allergy_concepts
            ),
        }
        for concept in sorted(concepts, key=lambda item: item.code)
    ]
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def canonicalize_medication(
    value: str, *, version: str = FORMULARY_VERSION
) -> FormularyConcept | None:
    if version != FORMULARY_VERSION:
        return None
    return _ALIASES.get(_fold(value))


def _canonicalize_from(
    value: str, concepts: Iterable[FormularyConcept]
) -> FormularyConcept | None:
    normalized = _fold(value)
    return next(
        (
            concept
            for concept in concepts
            if normalized
            in {_fold(alias) for alias in (concept.display_name, *concept.aliases)}
        ),
        None,
    )


def _normalized_unit(value: str | None) -> tuple[str | None, float]:
    if value is None:
        return None, 1.0
    normalized = _fold(value)
    aliases = {
        "mg": ("mg", 1.0),
        "毫克": ("mg", 1.0),
        "mcg": ("mg", 0.001),
        "μg": ("mg", 0.001),
        "g": ("mg", 1_000.0),
        "克": ("mg", 1_000.0),
        "ml": ("ml", 1.0),
        "毫升": ("ml", 1.0),
    }
    return aliases.get(normalized, (normalized or None, 1.0))


def _normalized_route(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = _fold(value)
    return {
        "po": "oral",
        "oral": "oral",
        "by mouth": "oral",
        "secara oral": "oral",
        "melalui mulut": "oral",
        "khau hok": "oral",
        "chhui": "oral",
        "口服": "oral",
        "iv": "intravenous",
        "intravenous": "intravenous",
        "intravena": "intravenous",
        "静脉": "intravenous",
        "靜脈": "intravenous",
        "im": "intramuscular",
        "intramuscular": "intramuscular",
        "肌肉注射": "intramuscular",
        "subcutaneous": "subcutaneous",
        "sc": "subcutaneous",
        "皮下注射": "subcutaneous",
    }.get(normalized, normalized or None)


def _normalized_frequency(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = _fold(value).replace("_", " ")
    return {
        "qd": "once_daily",
        "daily": "once_daily",
        "once daily": "once_daily",
        "once_daily": "once_daily",
        "sekali sehari": "once_daily",
        "chit jit chit pai": "once_daily",
        "每日一次": "once_daily",
        "每天一次": "once_daily",
        "bid": "twice_daily",
        "twice daily": "twice_daily",
        "twice_daily": "twice_daily",
        "dua kali sehari": "twice_daily",
        "chit jit nng pai": "twice_daily",
        "每日两次": "twice_daily",
        "每日兩次": "twice_daily",
        "每天两次": "twice_daily",
        "每天兩次": "twice_daily",
        "tid": "three_times_daily",
        "three times daily": "three_times_daily",
        "three_times_daily": "three_times_daily",
        "tiga kali sehari": "three_times_daily",
        "每日三次": "three_times_daily",
        "每天三次": "three_times_daily",
        "qid": "four_times_daily",
        "four times daily": "four_times_daily",
        "four_times_daily": "four_times_daily",
        "empat kali sehari": "four_times_daily",
        "每日四次": "four_times_daily",
        "每天四次": "four_times_daily",
    }.get(normalized, normalized.replace(" ", "_") or None)


def _screen_medication_regimen(
    *,
    medication: str | None,
    dose_value: float | None,
    dose_unit: str | None,
    route: str | None,
    frequency: str | None,
    active_allergy_concepts: Iterable[str] = (),
    version: str,
    concepts: tuple[FormularyConcept, ...],
    qualification_source: Literal["deployment_fixture", "clinic_version"],
    version_available: bool = True,
) -> MedicationScreeningResult:
    reasons: list[str] = []
    if not version_available:
        reasons.append("FORMULARY_VERSION_UNAVAILABLE")
    concept = _canonicalize_from(medication, concepts) if medication else None
    if medication is None or not medication.strip():
        reasons.append("MEDICATION_MISSING")
    elif concept is None:
        reasons.append("MEDICATION_CONCEPT_UNKNOWN")

    if dose_value is None or not isinstance(dose_value, (int, float)):
        reasons.append("DOSE_MISSING")
    elif dose_value <= 0:
        reasons.append("DOSE_INVALID")
    normalized_unit, factor = _normalized_unit(dose_unit)
    if normalized_unit is None:
        reasons.append("DOSE_UNIT_MISSING")
    normalized_route = _normalized_route(route)
    if normalized_route is None:
        reasons.append("ROUTE_MISSING")
    normalized_frequency = _normalized_frequency(frequency)
    if normalized_frequency is None:
        reasons.append("FREQUENCY_MISSING")

    if concept is not None:
        if normalized_unit is not None and normalized_unit != concept.dose_unit:
            reasons.append("DOSE_UNIT_NOT_PERMITTED")
        if dose_value is not None and dose_value > 0 and normalized_unit is not None:
            normalized_dose = float(dose_value) * factor
            if normalized_unit == concept.dose_unit and not (
                concept.minimum_single_dose
                <= normalized_dose
                <= concept.maximum_single_dose
            ):
                reasons.append("DOSE_OUT_OF_SCREENING_RANGE")
        if (
            normalized_route is not None
            and normalized_route not in concept.permitted_routes
        ):
            reasons.append("ROUTE_NOT_PERMITTED")
        active = {_fold(value) for value in active_allergy_concepts}
        contraindications = {
            _fold(value) for value in concept.contraindicated_allergy_concepts
        }
        if active & contraindications:
            reasons.append("ACTIVE_ALLERGY_CONTRAINDICATION")

    unique_reasons = tuple(dict.fromkeys(reasons))
    return MedicationScreeningResult(
        formulary_version=version,
        qualification_source=qualification_source,
        state="review_required" if unique_reasons else "qualified",
        concept_code=concept.code if concept is not None else None,
        canonical_name=concept.display_name if concept is not None else None,
        reason_codes=unique_reasons,
    )


def screen_medication_regimen(
    *,
    medication: str | None,
    dose_value: float | None,
    dose_unit: str | None,
    route: str | None,
    frequency: str | None,
    active_allergy_concepts: Iterable[str] = (),
    version: str = FORMULARY_VERSION,
) -> MedicationScreeningResult:
    """Screen against the frozen deployment fixture and label it explicitly."""

    concepts = formulary_concepts(version=version)
    return _screen_medication_regimen(
        medication=medication,
        dose_value=dose_value,
        dose_unit=dose_unit,
        route=route,
        frequency=frequency,
        active_allergy_concepts=active_allergy_concepts,
        version=version,
        concepts=concepts,
        qualification_source="deployment_fixture",
        version_available=bool(concepts),
    )


def _database_concept(row: ClinicFormularyConcept) -> FormularyConcept | None:
    aliases = tuple(
        alias
        for language_aliases in row.multilingual_aliases_json.values()
        if isinstance(language_aliases, list)
        for alias in language_aliases
        if isinstance(alias, str) and alias.strip()
    )
    routes = frozenset(
        route
        for route in row.permitted_routes_json
        if isinstance(route, str) and route.strip()
    )
    contraindications = frozenset(
        concept
        for concept in row.contraindicated_allergy_concepts_json
        if isinstance(concept, str) and concept.strip()
    )
    if (
        not row.canonical_name.strip()
        or not row.dose_unit.strip()
        or not routes
        or row.minimum_single_dose <= 0
        or row.maximum_single_dose < row.minimum_single_dose
    ):
        return None
    return FormularyConcept(
        code=row.concept_code,
        display_name=row.canonical_name,
        aliases=aliases,
        dose_unit=row.dose_unit,
        minimum_single_dose=row.minimum_single_dose,
        maximum_single_dose=row.maximum_single_dose,
        permitted_routes=routes,
        contraindicated_allergy_concepts=contraindications,
    )


def _database_concept_create(
    row: ClinicFormularyConcept,
) -> ClinicFormularyConceptCreate:
    if not row.active:
        raise FormularyConfigurationError("FORMULARY_INACTIVE_CONCEPT_PRESENT")
    return ClinicFormularyConceptCreate(
        concept_code=row.concept_code,
        canonical_name=row.canonical_name,
        multilingual_aliases=row.multilingual_aliases_json,
        dose_unit=row.dose_unit,
        minimum_single_dose=row.minimum_single_dose,
        maximum_single_dose=row.maximum_single_dose,
        permitted_routes=row.permitted_routes_json,
        contraindicated_allergy_concepts=(row.contraindicated_allergy_concepts_json),
    )


def _database_content_sha256(rows: Iterable[ClinicFormularyConcept]) -> str:
    return clinic_formulary_content_sha256(
        _database_concept_create(row) for row in rows
    )


def _version_rows(
    session: Session,
    version: ClinicFormularyVersion,
) -> list[ClinicFormularyConcept]:
    return list(
        session.exec(
            select(ClinicFormularyConcept)
            .where(
                ClinicFormularyConcept.clinic_id == version.clinic_id,
                ClinicFormularyConcept.formulary_version_id == version.id,
            )
            .order_by(col(ClinicFormularyConcept.concept_code))
        ).all()
    )


def _version_digest(
    rows: list[ClinicFormularyConcept],
) -> tuple[str | None, str | None]:
    try:
        return _database_content_sha256(rows), None
    except (FormularyConfigurationError, TypeError, ValueError):
        return None, "CLINIC_FORMULARY_CONTENT_INVALID"


def screen_clinic_medication_regimen(
    session: Session,
    *,
    clinic_id: uuid.UUID,
    medication: str | None,
    dose_value: float | None,
    dose_unit: str | None,
    route: str | None,
    frequency: str | None,
    active_allergy_concepts: Iterable[str] = (),
) -> MedicationScreeningResult:
    """Prefer one active clinic version; use the labelled fixture if absent.

    Multiple active versions or malformed rows fail closed.  The fallback is
    deliberately reported as ``deployment_fixture`` so callers and assessment
    evidence do not describe it as audited clinic data.
    """

    versions = session.exec(
        select(ClinicFormularyVersion)
        .where(
            ClinicFormularyVersion.clinic_id == clinic_id,
            ClinicFormularyVersion.status == "active",
        )
        .order_by(col(ClinicFormularyVersion.effective_at).desc())
    ).all()
    if not versions:
        return screen_medication_regimen(
            medication=medication,
            dose_value=dose_value,
            dose_unit=dose_unit,
            route=route,
            frequency=frequency,
            active_allergy_concepts=active_allergy_concepts,
        )
    if len(versions) != 1:
        return MedicationScreeningResult(
            formulary_version="ambiguous",
            qualification_source="clinic_version",
            state="review_required",
            concept_code=None,
            canonical_name=None,
            reason_codes=("CLINIC_FORMULARY_VERSION_AMBIGUOUS",),
        )
    version = versions[0]
    rows = session.exec(
        select(ClinicFormularyConcept).where(
            ClinicFormularyConcept.clinic_id == clinic_id,
            ClinicFormularyConcept.formulary_version_id == version.id,
            col(ClinicFormularyConcept.active).is_(True),
        )
    ).all()
    concepts = tuple(
        concept for row in rows if (concept := _database_concept(row)) is not None
    )
    computed_digest, digest_error = _version_digest(list(rows))
    if (
        not concepts
        or len(concepts) != len(rows)
        or digest_error is not None
        or computed_digest != version.content_sha256
        or version.content_locked_at is None
        or version.qualified_at is None
    ):
        return MedicationScreeningResult(
            formulary_version=version.version_code,
            qualification_source="clinic_version",
            state="review_required",
            concept_code=None,
            canonical_name=None,
            reason_codes=("CLINIC_FORMULARY_CONTENT_INVALID",),
        )
    return _screen_medication_regimen(
        medication=medication,
        dose_value=dose_value,
        dose_unit=dose_unit,
        route=route,
        frequency=frequency,
        active_allergy_concepts=active_allergy_concepts,
        version=version.version_code,
        concepts=concepts,
        qualification_source="clinic_version",
    )


def _concept_public(row: ClinicFormularyConcept) -> ClinicFormularyConceptPublic:
    return ClinicFormularyConceptPublic(
        id=row.id,
        concept_code=row.concept_code,
        canonical_name=row.canonical_name,
        multilingual_aliases=row.multilingual_aliases_json,
        dose_unit=row.dose_unit,
        minimum_single_dose=row.minimum_single_dose,
        maximum_single_dose=row.maximum_single_dose,
        permitted_routes=row.permitted_routes_json,
        contraindicated_allergy_concepts=(row.contraindicated_allergy_concepts_json),
    )


def formulary_version_public(
    session: Session,
    version: ClinicFormularyVersion,
    *,
    include_concepts: bool = True,
) -> ClinicFormularyVersionPublic:
    rows = _version_rows(session, version)
    computed_digest, digest_error = _version_digest(rows)
    digest_matches = computed_digest == version.content_sha256 and digest_error is None
    if not digest_matches or version.content_locked_at is None:
        qualification_state = "invalid"
    elif version.status == "active":
        qualification_state = "active"
    elif version.status == "retired":
        qualification_state = "retired"
    elif version.qualified_at is not None:
        qualification_state = "qualified"
    else:
        qualification_state = "unqualified"
    return ClinicFormularyVersionPublic(
        id=version.id,
        version_code=version.version_code,
        status=cast(Literal["draft", "active", "retired"], version.status),
        content_sha256=version.content_sha256,
        computed_content_sha256=computed_digest,
        digest_matches=digest_matches,
        qualification_state=qualification_state,
        qualification_source=cast(
            Literal["clinic_admin", "platform_template"] | None,
            version.qualification_source,
        ),
        content_locked_at=version.content_locked_at,
        qualified_at=version.qualified_at,
        effective_at=version.effective_at,
        retired_at=version.retired_at,
        concept_count=len(rows),
        concepts=[_concept_public(row) for row in rows] if include_concepts else [],
    )


def _validated_version_code(version_code: str) -> str:
    code = _require_exact_text(version_code, code="FORMULARY_VERSION_CODE_INVALID")
    if not _VERSION_CODE.fullmatch(code):
        raise FormularyConfigurationError("FORMULARY_VERSION_CODE_INVALID")
    return code


def _insert_formulary_draft(
    session: Session,
    *,
    clinic_id: uuid.UUID,
    version_code: str,
    concepts: Iterable[ClinicFormularyConceptCreate],
    created_by_membership_id: uuid.UUID | None,
) -> ClinicFormularyVersion:
    code = _validated_version_code(version_code)
    concept_list = list(concepts)
    payload = _validated_configuration_payload(concept_list)
    content_sha256 = hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    now = get_datetime_utc()
    version = ClinicFormularyVersion(
        clinic_id=clinic_id,
        version_code=code,
        status="draft",
        content_sha256=content_sha256,
        effective_at=now,
        created_by_membership_id=created_by_membership_id,
    )
    session.add(version)
    session.flush()
    for item in concept_list:
        session.add(
            ClinicFormularyConcept(
                clinic_id=clinic_id,
                formulary_version_id=version.id,
                concept_code=item.concept_code,
                canonical_name=item.canonical_name,
                multilingual_aliases_json=item.multilingual_aliases,
                dose_unit=item.dose_unit,
                minimum_single_dose=item.minimum_single_dose,
                maximum_single_dose=item.maximum_single_dose,
                permitted_routes_json=item.permitted_routes,
                contraindicated_allergy_concepts_json=(
                    item.contraindicated_allergy_concepts
                ),
                active=True,
            )
        )
    session.flush()
    version.content_locked_at = now
    session.add(version)
    session.flush()
    return version


def create_clinic_formulary_draft(
    session: Session,
    *,
    clinic_id: uuid.UUID,
    membership_id: uuid.UUID,
    body: ClinicFormularyVersionCreate,
) -> ClinicFormularyVersion:
    return _insert_formulary_draft(
        session,
        clinic_id=clinic_id,
        version_code=body.version_code,
        concepts=body.concepts,
        created_by_membership_id=membership_id,
    )


def _locked_version(
    session: Session,
    *,
    clinic_id: uuid.UUID,
    version_id: uuid.UUID,
) -> ClinicFormularyVersion | None:
    return session.exec(
        select(ClinicFormularyVersion)
        .where(
            ClinicFormularyVersion.clinic_id == clinic_id,
            ClinicFormularyVersion.id == version_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    ).first()


def qualify_clinic_formulary_version(
    session: Session,
    *,
    clinic_id: uuid.UUID,
    membership_id: uuid.UUID,
    version_id: uuid.UUID,
    expected_content_sha256: str,
) -> ClinicFormularyVersion | None:
    version = _locked_version(session, clinic_id=clinic_id, version_id=version_id)
    if version is None:
        return None
    if version.status != "draft" or version.content_locked_at is None:
        raise FormularyConfigurationError("FORMULARY_VERSION_NOT_QUALIFIABLE")
    if not re.fullmatch(r"[0-9a-f]{64}", expected_content_sha256):
        raise FormularyConfigurationError("FORMULARY_EXPECTED_DIGEST_INVALID")
    computed_digest, digest_error = _version_digest(_version_rows(session, version))
    if (
        digest_error is not None
        or computed_digest is None
        or computed_digest != version.content_sha256
    ):
        raise FormularyConfigurationError("FORMULARY_CONTENT_DIGEST_INVALID")
    if expected_content_sha256 != computed_digest:
        raise FormularyConfigurationError("FORMULARY_EXPECTED_DIGEST_MISMATCH")
    if version.qualified_at is None:
        version.qualified_at = get_datetime_utc()
        version.qualified_by_membership_id = membership_id
        version.qualification_source = "clinic_admin"
        session.add(version)
        session.flush()
    elif (
        version.qualified_by_membership_id != membership_id
        or version.qualification_source != "clinic_admin"
    ):
        raise FormularyConfigurationError("FORMULARY_ALREADY_QUALIFIED")
    return version


def activate_clinic_formulary_version(
    session: Session,
    *,
    clinic_id: uuid.UUID,
    membership_id: uuid.UUID,
    version_id: uuid.UUID,
    expected_content_sha256: str,
) -> tuple[ClinicFormularyVersion | None, uuid.UUID | None]:
    versions = list(
        session.exec(
            select(ClinicFormularyVersion)
            .where(ClinicFormularyVersion.clinic_id == clinic_id)
            .order_by(col(ClinicFormularyVersion.created_at))
            .with_for_update()
            .execution_options(populate_existing=True)
        ).all()
    )
    target = next((item for item in versions if item.id == version_id), None)
    if target is None:
        return None, None
    if target.status == "retired":
        raise FormularyConfigurationError("FORMULARY_RETIRED_VERSION_IMMUTABLE")
    computed_digest, digest_error = _version_digest(_version_rows(session, target))
    if (
        not re.fullmatch(r"[0-9a-f]{64}", expected_content_sha256)
        or digest_error is not None
        or computed_digest is None
        or computed_digest != target.content_sha256
        or expected_content_sha256 != computed_digest
        or target.qualified_at is None
    ):
        raise FormularyConfigurationError("FORMULARY_ACTIVATION_NOT_QUALIFIED")
    if target.status == "active":
        return target, None
    previous = next((item for item in versions if item.status == "active"), None)
    now = get_datetime_utc()
    if previous is not None:
        previous.status = "retired"
        previous.retired_at = now
        session.add(previous)
        # Release the partial unique active-version slot before activating the
        # replacement. SQLAlchemy update ordering is not a clinical invariant.
        session.flush()
    target.status = "active"
    target.effective_at = now
    target.activated_by_membership_id = membership_id
    session.add(target)
    session.flush()
    return target, previous.id if previous is not None else None


def clinic_formulary_readiness(
    session: Session,
    clinic_id: uuid.UUID,
) -> ClinicFormularyReadinessPublic:
    active_versions = list(
        session.exec(
            select(ClinicFormularyVersion).where(
                ClinicFormularyVersion.clinic_id == clinic_id,
                ClinicFormularyVersion.status == "active",
            )
        ).all()
    )
    if not active_versions:
        return ClinicFormularyReadinessPublic(
            ready=False,
            reason_code="clinic_formulary_active_version_missing",
        )
    if len(active_versions) != 1:
        return ClinicFormularyReadinessPublic(
            ready=False,
            reason_code="clinic_formulary_active_version_ambiguous",
        )
    version = active_versions[0]
    computed_digest, digest_error = _version_digest(_version_rows(session, version))
    if (
        digest_error is not None
        or computed_digest != version.content_sha256
        or version.content_locked_at is None
        or version.qualified_at is None
    ):
        return ClinicFormularyReadinessPublic(
            ready=False,
            reason_code="clinic_formulary_active_version_invalid",
            active_version_id=version.id,
            version_code=version.version_code,
            content_sha256=version.content_sha256,
            qualification_source=cast(
                Literal["clinic_admin", "platform_template"] | None,
                version.qualification_source,
            ),
        )
    return ClinicFormularyReadinessPublic(
        ready=True,
        active_version_id=version.id,
        version_code=version.version_code,
        content_sha256=version.content_sha256,
        qualification_source=cast(
            Literal["clinic_admin", "platform_template"] | None,
            version.qualification_source,
        ),
    )


def seed_clinic_formulary_template(
    session: Session,
    *,
    clinic_id: uuid.UUID,
    template: str,
) -> ClinicFormularyVersion:
    concepts = _FORMULARY_TEMPLATES.get(template)
    if concepts is None:
        raise FormularyConfigurationError("FORMULARY_TEMPLATE_UNKNOWN")
    existing = session.exec(
        select(ClinicFormularyVersion).where(
            ClinicFormularyVersion.clinic_id == clinic_id,
            ClinicFormularyVersion.version_code == template,
        )
    ).first()
    if existing is not None:
        readiness = clinic_formulary_readiness(session, clinic_id)
        if existing.status == "active" and readiness.ready:
            return existing
        raise FormularyConfigurationError("FORMULARY_TEMPLATE_SEED_CONFLICT")
    version = _insert_formulary_draft(
        session,
        clinic_id=clinic_id,
        version_code=template,
        concepts=concepts,
        created_by_membership_id=None,
    )
    now = get_datetime_utc()
    version.qualified_at = now
    version.qualification_source = "platform_template"
    version.status = "active"
    version.effective_at = now
    session.add(version)
    session.flush()
    return version
