from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

SpeakerRole = Literal["clinician", "patient", "family", "unknown"]
LanguageCode = Literal["en", "ms", "nan", "zh", "und"]


@dataclass(frozen=True)
class LanguageSpan:
    start: int
    end: int
    language: LanguageCode
    turn_index: int
    review_required: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ConsultTurn:
    speaker_id: str
    text: str
    start_ms: int = 0
    end_ms: int = 0
    source_language: str | None = None
    language_confidence: float | None = None
    speaker_role: SpeakerRole | None = None
    tagged_text: str | None = None
    overlap_group_id: str | None = None
    raw_text: str | None = None
    asr_hypothesis: str | None = None

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> ConsultTurn:
        role = payload.get("speaker_role")
        overlap = payload.get("overlap_group_id")
        hypothesis = payload.get("asr_hypothesis")
        raw = payload.get("raw_text")
        return cls(
            speaker_id=str(payload["speaker_id"]),
            text=str(payload["text"]),
            start_ms=int(payload.get("start_ms") or 0),
            end_ms=int(payload.get("end_ms") or 0),
            source_language=payload.get("source_language"),
            language_confidence=payload.get("language_confidence"),
            speaker_role=role,  # type: ignore[arg-type]
            tagged_text=payload.get("tagged_text"),
            overlap_group_id=str(overlap) if overlap else None,
            raw_text=str(raw) if raw else None,
            asr_hypothesis=str(hypothesis) if hypothesis else None,
        )


@dataclass
class ConsultInput:
    consult_id: str
    turns: list[ConsultTurn]
    enabled_languages: tuple[str, ...] = ("en", "ms", "zh", "nan")

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> ConsultInput:
        languages = payload.get("enabled_languages") or ["en", "ms", "zh", "nan"]
        return cls(
            consult_id=str(payload["id"]),
            turns=[ConsultTurn.from_dict(item) for item in payload["turns"]],
            enabled_languages=tuple(str(item) for item in languages),
        )


@dataclass(frozen=True)
class ProposedFact:
    fact_type: str
    key: str
    value: str
    polarity: str
    assertion_scope: str
    start: int
    end: int
    quote: str
    source_language: str
    speaker_id: str
    speaker_role: str
    review_required: bool
    turn_index: int
    penicillin_class: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ProposedAlert:
    concept_code: str
    polarity: str
    speaker_role: str
    source_language: str
    quote: str
    turn_index: int
    review_required: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ProposedConflict:
    fact_type: str
    key: str
    left_polarity: str
    right_polarity: str
    left_speaker_role: str
    right_speaker_role: str
    left_source_language: str
    right_source_language: str
    severity: str = "critical"
    auto_resolved: bool = False
    reason: str = "polarity"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ConsultState:
    consult_id: str
    turns: list[ConsultTurn]
    enabled_languages: tuple[str, ...]
    language_spans: list[LanguageSpan] = field(default_factory=list)
    speaker_roles: dict[str, str] = field(default_factory=dict)
    proposed_facts: list[ProposedFact] = field(default_factory=list)
    proposed_alerts: list[ProposedAlert] = field(default_factory=list)
    proposed_conflicts: list[ProposedConflict] = field(default_factory=list)
    summary_proposals: dict[str, str] = field(default_factory=dict)
    warning_codes: list[str] = field(default_factory=list)
    agent_trace: list[str] = field(default_factory=list)
    publish_blocked: bool = False
    cmi: float | None = None
    switch_points: int = 0
    synthetic: bool = True

    def add_warning(self, code: str) -> None:
        if code not in self.warning_codes:
            self.warning_codes.append(code)

    def trace(self, message: str) -> None:
        self.agent_trace.append(message)

    def to_dict(self) -> dict[str, Any]:
        return {
            "consult_id": self.consult_id,
            "synthetic": True,
            "not_clinical_validation": True,
            "speaker_roles": dict(sorted(self.speaker_roles.items())),
            "language_spans": [span.to_dict() for span in self.language_spans],
            "proposed_facts": [fact.to_dict() for fact in self.proposed_facts],
            "proposed_alerts": [alert.to_dict() for alert in self.proposed_alerts],
            "proposed_conflicts": [
                conflict.to_dict() for conflict in self.proposed_conflicts
            ],
            "summary_proposals": dict(sorted(self.summary_proposals.items())),
            "warning_codes": list(self.warning_codes),
            "agent_trace": list(self.agent_trace),
            "publish_blocked": self.publish_blocked,
            "cmi": self.cmi,
            "switch_points": self.switch_points,
            "turns": [
                {
                    "speaker_id": turn.speaker_id,
                    "text": turn.text,
                    "start_ms": turn.start_ms,
                    "end_ms": turn.end_ms,
                    "source_language": turn.source_language,
                    "speaker_role": self.speaker_roles.get(turn.speaker_id),
                    "overlap_group_id": turn.overlap_group_id,
                    "raw_text": turn.raw_text,
                }
                for turn in self.turns
            ],
        }
