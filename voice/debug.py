import os
from datetime import datetime, timezone
from pathlib import Path

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


def save_audio(wav_bytes, user_id, env=None, now=None):
    source = os.environ if env is None else env
    directory = (source.get("VOICE_DEBUG_DIR") or "").strip()
    if not directory or not enabled(source):
        return
    try:
        moment = now or datetime.now(timezone.utc)
        target = Path(directory)
        target.mkdir(parents=True, exist_ok=True)
        path = target / f"stt-{moment.strftime('%Y%m%d-%H%M%S-%f')}-{int(user_id)}.wav"
        path.write_bytes(wav_bytes)
        emit(f"stt audio saved {path.name}")
    except Exception as error:
        emit(f"stt audio save failed {type(error).__name__}")
