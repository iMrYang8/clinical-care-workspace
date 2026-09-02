from __future__ import annotations

import uuid

import httpx
import pytest

from app.services.provider_resilience import (
    classify_provider_failure,
    retry_delay_seconds,
)

pytestmark = pytest.mark.unit


def _http_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://provider.invalid")
    response = httpx.Response(status, request=request)
    return httpx.HTTPStatusError("synthetic", request=request, response=response)


def test_provider_failure_taxonomy_distinguishes_retryable_outages() -> None:
    assert classify_provider_failure(TimeoutError()).code == "PROVIDER_TIMEOUT"
    outage = classify_provider_failure(_http_error(503))
    assert (outage.code, outage.failure_class, outage.retryable) == (
        "PROVIDER_HTTP_503",
        "transient",
        True,
    )
    invalid = classify_provider_failure(_http_error(400))
    assert invalid.retryable is False
    assert invalid.failure_class == "permanent"


def test_retry_schedule_uses_bounded_stable_jitter_across_one_hour() -> None:
    job_id = uuid.UUID("00000000-0000-0000-0000-000000000123")
    delays = [retry_delay_seconds(job_id, attempt) for attempt in range(1, 7)]

    assert 24 <= delays[0] <= 36
    assert 96 <= delays[1] <= 144
    assert 480 <= delays[2] <= 720
    assert 1_440 <= delays[3] <= 2_160
    assert 2_880 <= delays[4] <= 4_320
    assert 2_880 <= delays[5] <= 4_320
    assert delays == [retry_delay_seconds(job_id, i) for i in range(1, 7)]
