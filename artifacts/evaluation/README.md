# Trust evaluation artifacts

Only aggregate, non-PHI reports belong in this directory. Raw model responses and
audio caches are ignored by Git under `artifacts/evaluation/cache/`.

- `redaction-v2.json`: completed local Gold Span redaction evaluation.
- `voice-calibration.json`: generated only after the real PriMock57 OpenAI run.
- `fact-calibration.json`: generated only after the real ACI-Bench OpenAI run.

If the real reports are absent, expired, undersized, or do not match the exact
provider/model/task/request-parameter/dataset hashes, runtime confidence remains
`Unavailable` and the output is routed to clinical review.
