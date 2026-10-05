import asyncio
import json
import os
import tempfile
import weakref

_guild_locks = weakref.WeakKeyDictionary()


def guild_lock(gid: int) -> asyncio.Lock:
    per_loop = _guild_locks.setdefault(asyncio.get_running_loop(), {})
    return per_loop.setdefault(gid, asyncio.Lock())


def playlist_path(playlists_dir: str, gid: int) -> str:
    return os.path.join(playlists_dir, f"{gid}.json")


def load_playlists(playlists_dir: str, gid: int) -> dict:
    path = playlist_path(playlists_dir, gid)
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_playlists(playlists_dir: str, gid: int, data: dict) -> None:
    descriptor, temp_path = tempfile.mkstemp(dir=playlists_dir, prefix=f"{gid}.", suffix=".tmp")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, playlist_path(playlists_dir, gid))
    except BaseException:
        if os.path.exists(temp_path):
            os.remove(temp_path)
        raise
