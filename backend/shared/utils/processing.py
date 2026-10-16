"""Processing switch shared by HTTP, ingestion and standalone worker entrypoints."""
import os


def background_processing_enabled() -> bool:
    raw = os.getenv('ENABLE_BACKGROUND_PROCESSING', 'true').strip().lower()
    if raw in {'true', '1', 'yes', 'on'}:
        return True
    if raw in {'false', '0', 'no', 'off'}:
        return False
    raise RuntimeError('ENABLE_BACKGROUND_PROCESSING must be a boolean value')


def require_background_processing():
    if not background_processing_enabled():
        raise RuntimeError('Background processing is disabled; no work was started')
