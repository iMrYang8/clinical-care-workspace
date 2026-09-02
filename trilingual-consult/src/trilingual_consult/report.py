"""Human-readable agent trace. Synthetic; not a clinical quality claim."""

from __future__ import annotations

from trilingual_consult.state import ConsultState


def render_markdown(state: ConsultState) -> str:
    lines = [
        f"# NightingaleSwitchCare `{state.consult_id}`",
        "",
        "> Synthetic gold. Not clinical validation. Not Nightingale runtime. Agents propose only.",
        "",
        f"- publish_blocked: `{state.publish_blocked}`",
        f"- CMI: `{state.cmi}` · switch points: `{state.switch_points}`",
        f"- warnings: {', '.join(state.warning_codes) if state.warning_codes else '(none)'}",
        "",
        "## Agent trace",
        "",
    ]
    for item in state.agent_trace:
        lines.append(f"- `{item}`")
    lines.extend(
        [
            "",
            "## Three-axis provenance (speaker_role × language × assertion)",
            "",
            "| speaker_role | language | assertion | quote | review_required |",
            "|---|---|---|---|---|",
        ]
    )
    for fact in state.proposed_facts:
        assertion = f"{fact.fact_type}:{fact.key}:{fact.polarity if fact.fact_type == 'allergy' else fact.value}"
        quote = fact.quote.replace("|", "\\|")
        lines.append(
            f"| {fact.speaker_role} | {fact.source_language} | `{assertion}` | {quote} | {fact.review_required} |"
        )
    if not state.proposed_facts:
        lines.append("| — | — | — | — | — |")
    lines.extend(["", "## Language spans", "", "| turn | start | end | language | review_required | slice |", "|---|---:|---:|---|---|---|"])
    for span in state.language_spans:
        turn = state.turns[span.turn_index]
        slice_text = turn.text[span.start : span.end].replace("|", "\\|")
        lines.append(
            f"| {span.turn_index} ({turn.speaker_id}) | {span.start} | {span.end} | "
            f"{span.language} | {span.review_required} | {slice_text} |"
        )
    lines.extend(["", "## Conflicts", ""])
    if not state.proposed_conflicts:
        lines.append("None. Publish is still a human gate.")
    else:
        lines.append("| key | reason | severity | auto_resolved | left | right |")
        lines.append("|---|---|---|---|---|---|")
        for conflict in state.proposed_conflicts:
            lines.append(
                f"| {conflict.key} | {conflict.reason} | {conflict.severity} | "
                f"{conflict.auto_resolved} | {conflict.left_speaker_role}/{conflict.left_polarity} | "
                f"{conflict.right_speaker_role}/{conflict.right_polarity} |"
            )
        lines.append("")
        lines.append("Nothing auto-resolved. `publish_blocked` stays true until a human decides.")
    lines.extend(["", "## Audience summaries (proposals)", ""])
    for key in ("clinician_en", "patient_zh", "family_ms"):
        body = state.summary_proposals.get(key, "")
        lines.append(f"### {key}")
        lines.append("")
        lines.append(body or "_(empty)_")
        lines.append("")
    return "\n".join(lines)
