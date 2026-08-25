import importlib
from pathlib import Path

from app.core.config import settings


def pyannote_runtime_status() -> tuple[bool, str]:
    """Report readiness without importing pyannote or downloading gated weights."""

    if not settings.PYANNOTE_ENABLED:
        return False, "PYANNOTE_DISABLED"
    if not settings.PYANNOTE_MODEL_DIR:
        return False, "PYANNOTE_LOCAL_MODEL_REQUIRED"
    model_dir = Path(settings.PYANNOTE_MODEL_DIR).expanduser()
    if not model_dir.is_dir():
        return False, "PYANNOTE_MODEL_NOT_CACHED"
    try:
        importlib.import_module("pyannote.audio")
    except ImportError:
        return False, "PYANNOTE_DEPENDENCY_UNAVAILABLE"
    # Model license acceptance and access are operator responsibilities. No
    # Hugging Face token is read or used at runtime by this readiness check.
    return True, "PYANNOTE_LOCAL_MODEL_READY"
