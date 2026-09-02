"""Audience summaries are proposals. They must not drop family hearsay."""

from __future__ import annotations

from trilingual_consult.state import ConsultState, ProposedFact

_ZH_DISPLAY = {
    "penicillin": "盘尼西林",
    "amoxicillin": "阿莫西林",
    "metformin": "二甲双胍",
    "aspirin": "阿司匹林",
}


def _zh_name(key: str) -> str:
    return _ZH_DISPLAY.get(key, key)


def _allergy_line(facts: list[ProposedFact]) -> tuple[str, str, str]:
    patient = [
        fact
        for fact in facts
        if fact.fact_type == "allergy" and fact.speaker_role == "patient"
    ]
    family = [
        fact
        for fact in facts
        if fact.fact_type == "allergy" and fact.speaker_role == "family"
    ]
    clinician_bits: list[str] = []
    patient_zh_bits: list[str] = []
    family_ms_bits: list[str] = []
    for fact in patient:
        denied = fact.polarity == "absent"
        clinician_bits.append(
            f"Patient self-report ({fact.source_language}): "
            f"{'denies' if denied else 'reports'} {fact.key} allergy."
        )
        if denied:
            patient_zh_bits.append(f"您表示对{_zh_name(fact.key)}不过敏。")
        else:
            patient_zh_bits.append(f"您表示对{_zh_name(fact.key)}过敏。")
    for fact in family:
        clinician_bits.append(
            f"Family report ({fact.source_language}): {fact.key} allergy "
            f"({fact.polarity}) — hearsay, review required."
        )
        patient_zh_bits.append(
            f"家属用马来语提到{_zh_name(fact.key)}过敏，需医生核对，尚未作为您的确定过敏记录。"
        )
        family_ms_bits.append(
            f"Laporan anda (alahan {fact.key}) perlu disemak doktor. "
            "Ini bukan rekod sah sebelum doktor semak."
        )
    meds = [fact for fact in facts if fact.fact_type == "dose"]
    for fact in meds:
        clinician_bits.append(
            f"Clinician plan: {fact.key} {fact.value}."
        )
        family_ms_bits.append(f"Ubat: {fact.key} {fact.value}.")
        patient_zh_bits.append(f"医生计划：{_zh_name(fact.key)} {fact.value}。")
    return (
        " ".join(clinician_bits) or "No structured allergy or dose proposals.",
        "".join(patient_zh_bits) or "本次没有可展示的过敏或用药摘要。",
        " ".join(family_ms_bits) or "Tiada ringkasan ubat atau alahan.",
    )


def run_summaries(state: ConsultState) -> ConsultState:
    clinician_en, patient_zh, family_ms = _allergy_line(state.proposed_facts)
    state.summary_proposals = {
        "clinician_en": clinician_en,
        "patient_zh": patient_zh,
        "family_ms": family_ms,
    }
    state.trace("summaries:clinician_en/patient_zh/family_ms")
    return state
