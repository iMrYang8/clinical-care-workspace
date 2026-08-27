"""Normalization shared by operator-facing clinic code entry points."""

import re

CLINIC_CODE_PATTERN = re.compile(r"^[A-Z]{3,12}$")


def normalize_clinic_code(value: str | None) -> str | None:
    if value is None:
        return None
    # Lowercase entry is normalized for usability, but whitespace and every
    # non-ASCII letter remain validation errors rather than being silently
    # rewritten into a different operator identifier.
    normalized = value.upper()
    return normalized if CLINIC_CODE_PATTERN.fullmatch(normalized) else None
