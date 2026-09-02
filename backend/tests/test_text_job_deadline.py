from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any

import pytest

from app.models import Job, ProviderCircuitState
from app.services import ai_jobs
from app.services.provider_resilience import classify_provider_failure

pytestmark = pytest.mark.unit


def test_sequential_text_stages_share_one_whole_job_deadline() -> None:
    async def run() -> tuple[float, bool, float]:
        # Scale seconds down by 1,000 for the unit test: the first stage uses
        # 35/75 of the budget and the second would push the sequence past 75.
        deadline = ai_jobs._TextJobDeadline(0.075)  # noqa: SLF001
        started = time.monotonic()
        await deadline.wait_for(asyncio.sleep(0.035), stage_timeout_seconds=0.05)
        second_stage_completed = False

        async def second_stage() -> None:
            nonlocal second_stage_completed
            await asyncio.sleep(0.060)
            second_stage_completed = True

        with pytest.raises(TimeoutError):
            await deadline.wait_for(second_stage(), stage_timeout_seconds=0.07)
        return (
            time.monotonic() - started,
            second_stage_completed,
            deadline.remaining_seconds,
        )

    elapsed, second_stage_completed, remaining = asyncio.run(run())

    # A per-stage reset would let the second stage finish. The shared budget
    # cancels it, and the loose wall-clock assertion only catches deadlocks.
    assert second_stage_completed is False
    assert remaining == 0.0
    assert elapsed < 0.5


class _FailureSession:
    def add(self, _item: object) -> None:
        return None


def test_45_second_remote_hang_is_delayed_and_marked_timed_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def remote_hang() -> tuple[float, bool]:
        # Scale seconds down by 1,000. A 45-second no-result provider is cut off
        # by the independent 15-second first-result boundary, not the 75-second
        # whole-job boundary.
        deadline = ai_jobs._TextJobDeadline(0.075)  # noqa: SLF001
        started = time.monotonic()
        provider_returned = False

        async def provider_request() -> None:
            nonlocal provider_returned
            await asyncio.sleep(0.045)
            provider_returned = True

        with pytest.raises(TimeoutError) as caught:
            await deadline.wait_for(provider_request(), stage_timeout_seconds=0.015)
        failure = classify_provider_failure(caught.value)
        assert failure.code == "PROVIDER_TIMEOUT"
        return time.monotonic() - started, provider_returned

    elapsed, provider_returned = asyncio.run(remote_hang())
    clinic_id = uuid.uuid4()
    job = Job(
        clinic_id=clinic_id,
        patient_id=uuid.uuid4(),
        kind="ai_extract",
        state="needs_review",
        idempotency_key="fixture",
        request_sha256="a" * 64,
        payload_ciphertext=b"fixture",
        attempt_count=1,
        created_by_id=uuid.uuid4(),
    )
    circuit = ProviderCircuitState(
        clinic_id=clinic_id,
        provider="openai",
        capability="clinical_text",
    )

    def circuit_for_failure(
        _session: Any, _clinic_id: uuid.UUID, *, lock: bool = False
    ) -> ProviderCircuitState:
        del lock
        return circuit

    monkeypatch.setattr(ai_jobs, "_provider_circuit", circuit_for_failure)
    timeout_failure = classify_provider_failure(TimeoutError())

    retry_at = ai_jobs._record_provider_failure(  # noqa: SLF001
        _FailureSession(),  # type: ignore[arg-type]
        job,
        timeout_failure,
        retry_index=1,
    )

    assert provider_returned is False
    assert elapsed < 0.5
    assert job.state == "needs_review"
    assert job.error_code == "PROVIDER_TIMEOUT"
    assert job.error_class == "timeout"
    assert job.provider_outage is True
    assert job.delayed_at is not None
    assert job.timed_out_at is not None
    assert job.next_run_at == retry_at
    assert retry_at is not None
    assert circuit.state == "open"
    assert job.retry_history_json[-1]["error_class"] == "timeout"
