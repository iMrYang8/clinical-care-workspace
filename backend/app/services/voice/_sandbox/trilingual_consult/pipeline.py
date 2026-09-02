"""Orchestrator: ordered agents + in-memory memory. No database."""

from __future__ import annotations

from trilingual_consult.agents.attribution import run_attribution
from trilingual_consult.agents.conflicts import run_conflicts
from trilingual_consult.agents.safety import run_safety
from trilingual_consult.agents.scribe import run_scribe
from trilingual_consult.agents.summaries import run_summaries
from trilingual_consult.agents.term_anchor import run_term_anchor
from trilingual_consult.agents.verifier import run_verifier
from trilingual_consult.lexicon import count_cmi_and_switches
from trilingual_consult.state import ConsultInput, ConsultState

_AGENTS = (
    ("scribe", run_scribe),
    ("attribution", run_attribution),
    ("safety", run_safety),
    ("term_anchor", run_term_anchor),
    ("conflicts", run_conflicts),
    ("summaries", run_summaries),
    ("verifier", run_verifier),
)


def run_consult_pipeline(consult: ConsultInput) -> ConsultState:
    state = ConsultState(
        consult_id=consult.consult_id,
        turns=list(consult.turns),
        enabled_languages=consult.enabled_languages,
    )
    state.trace("orchestrator:start")
    tagged = [turn.tagged_text or turn.text for turn in state.turns]
    state.cmi, state.switch_points = count_cmi_and_switches(tagged)
    for name, agent in _AGENTS:
        state.trace(f"orchestrator:run/{name}")
        state = agent(state)
    state.trace("orchestrator:done")
    return state
