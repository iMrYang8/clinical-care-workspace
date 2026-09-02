"""Deterministic provider failure taxonomy and persisted retry scheduling."""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from dataclasses import dataclass

import httpx

_BASE_BACKOFF_SECONDS = (30, 120, 600, 1_800, 3_600)


@dataclass(frozen=True)
class ProviderFailure:
    code: str
    failure_class: str
    retryable: bool
    status_code: int | None = None


class ProviderCircuitOpen(RuntimeError):
    """A persisted circuit prevented a premature provider request."""


def classify_provider_failure(exc: BaseException) -> ProviderFailure:
    if isinstance(exc, ProviderCircuitOpen):
        return ProviderFailure("PROVIDER_CIRCUIT_OPEN", "transient", True)
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError, httpx.TimeoutException)):
        return ProviderFailure("PROVIDER_TIMEOUT", "timeout", True)
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if status == 429:
            return ProviderFailure("PROVIDER_RATE_LIMITED", "transient", True, status)
        if status in {500, 502, 503, 504}:
            return ProviderFailure(f"PROVIDER_HTTP_{status}", "transient", True, status)
        return ProviderFailure(f"PROVIDER_HTTP_{status}", "permanent", False, status)
    if isinstance(exc, (httpx.NetworkError, ConnectionError)):
        return ProviderFailure("PROVIDER_NETWORK_ERROR", "transient", True)
    return ProviderFailure("PROVIDER_FAILURE", "unknown", False)


def retry_delay_seconds(job_id: uuid.UUID, attempt_count: int) -> int:
    """Return the plan's backoff with stable +/-20% per job/attempt jitter."""

    index = max(0, min(attempt_count - 1, len(_BASE_BACKOFF_SECONDS) - 1))
    base = _BASE_BACKOFF_SECONDS[index]
    digest = hashlib.sha256(f"{job_id}:{attempt_count}".encode()).digest()
    fraction = int.from_bytes(digest[:2], "big") / 65_535
    multiplier = 0.8 + (fraction * 0.4)
    return max(1, round(base * multiplier))
