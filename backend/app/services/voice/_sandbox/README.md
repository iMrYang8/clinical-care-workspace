# Vendored NightingaleSwitchCare snapshot

Copy of the sibling `trilingual-consult/` package. Agents still do not import
`app`. Nightingale's worker may import them through
`app.services.voice.multi_agent` when `VOICE_MULTI_AGENT_PIPELINE=true`.

Refresh with `scripts/sync-trilingual-sandbox.sh`. `tests/test_trilingual_snapshot.py`
fails if this tree drifts from the sibling.

This is synthetic consult understanding, not ASR and not a clinical-quality claim.
