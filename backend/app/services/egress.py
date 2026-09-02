"""Typed, fail-closed boundary for remote text-model egress.

Remote providers accept only :class:`QualifiedRedactedText` constructed from a
completed redaction report. Keeping this check next to the actual provider call
makes the redaction-before-model ordering executable rather than documentary.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Protocol, cast

from app.services.providers.base import ClinicalNoteDraft, ExtractionContext


class RedactionProof(Protocol):
    @property
    def redacted_text(self) -> str: ...

    @property
    def redacted_sha256(self) -> str: ...

    @property
    def residual_scan_passed(self) -> bool: ...

    @property
    def remote_egress_allowed(self) -> bool: ...

    @property
    def status(self) -> str: ...

    @property
    def error_code(self) -> str | None: ...


_QUALIFICATION_MARKER = object()


@dataclass(frozen=True, init=False)
class QualifiedRedactedText:
    """Opaque payload minted only after a complete redaction proof passes."""

    text: str
    sha256: str
    redaction_status: str
    _qualification_marker: object = field(repr=False, compare=False)

    @classmethod
    def from_report(cls, report: RedactionProof) -> QualifiedRedactedText:
        digest = hashlib.sha256(report.redacted_text.encode()).hexdigest()
        if (
            not report.remote_egress_allowed
            or not report.residual_scan_passed
            or report.error_code is not None
            or digest != report.redacted_sha256
        ):
            raise ValueError("REMOTE_TEXT_EGRESS_NOT_QUALIFIED")
        payload = object.__new__(cls)
        object.__setattr__(payload, "text", report.redacted_text)
        object.__setattr__(payload, "sha256", digest)
        object.__setattr__(payload, "redaction_status", report.status)
        object.__setattr__(payload, "_qualification_marker", _QUALIFICATION_MARKER)
        return payload

    def assert_qualified(self) -> None:
        """Revalidate provenance and integrity at the provider boundary."""

        digest = hashlib.sha256(self.text.encode()).hexdigest()
        if (
            getattr(self, "_qualification_marker", None) is not _QUALIFICATION_MARKER
            or digest != self.sha256
        ):
            raise ValueError("REMOTE_TEXT_EGRESS_NOT_QUALIFIED")


class RemoteClinicalNoteProvider(Protocol):
    """Remote providers can only receive a gateway-qualified payload."""

    async def extract(
        self, payload: QualifiedRedactedText, context: ExtractionContext
    ) -> ClinicalNoteDraft: ...


class RemoteClinicalReviewProvider(RemoteClinicalNoteProvider, Protocol):
    review_model: str | None

    async def review(
        self,
        payload: QualifiedRedactedText,
        context: ExtractionContext,
        primary: ClinicalNoteDraft,
    ) -> ClinicalNoteDraft: ...


class TextModelEgressGateway:
    """The single callable boundary between qualified text and a remote model."""

    def __init__(self, provider: RemoteClinicalNoteProvider) -> None:
        self.provider = provider

    async def extract(
        self, report: RedactionProof, context: ExtractionContext
    ) -> ClinicalNoteDraft:
        payload = QualifiedRedactedText.from_report(report)
        payload.assert_qualified()
        return await self.provider.extract(payload, context)

    async def review(
        self,
        report: RedactionProof,
        context: ExtractionContext,
        primary: ClinicalNoteDraft,
    ) -> ClinicalNoteDraft:
        payload = QualifiedRedactedText.from_report(report)
        payload.assert_qualified()
        provider = cast(RemoteClinicalReviewProvider, self.provider)
        return await provider.review(payload, context, primary)
