import json
import os


def playlist_path(playlists_dir: str, gid: int) -> str:
    return os.path.join(playlists_dir, f"{gid}.json")


def load_playlists(playlists_dir: str, gid: int) -> dict:
    path = playlist_path(playlists_dir, gid)
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_playlists(playlists_dir: str, gid: int, data: dict) -> None:
    with open(playlist_path(playlists_dir, gid), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
