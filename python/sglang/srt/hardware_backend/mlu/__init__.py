"""MLU hardware backend registration."""

_INITIALIZED = False


def init_mlu_backend() -> None:
    """Initialize MLU backend hooks that are still kept out of core modules."""
    global _INITIALIZED
    if _INITIALIZED:
        return

    _INITIALIZED = True
