import os

TRUE_VALUES = {"1", "true", "yes", "on"}


def enabled(env=None):
    source = os.environ if env is None else env
    return source.get("VOICE_DEBUG", "").strip().lower() in TRUE_VALUES


def emit(message):
    if not enabled():
        return
    try:
        print(f"[voz-debug] {message}")
    except Exception:
        pass
