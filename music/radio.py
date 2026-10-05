import random

import ai_dj
from music.ytdl import normalize_youtube_url

RADIO_MAX_DURATION_S = 600
RADIO_BATCH_SIZE = 5


def is_playable_candidate(entry: dict, max_duration_s: int, excluded_ids: set[str]) -> bool:
    video_id = entry.get("id")
    duration = entry.get("duration")
    return (
        bool(video_id)
        and video_id not in excluded_ids
        and entry.get("live_status") not in ("is_live", "is_upcoming")
        and (duration is None or duration <= max_duration_s)
    )


def enqueue_entry(player, entry: dict) -> str | None:
    url = normalize_youtube_url(entry.get("webpage_url") or entry.get("url"))
    if not url:
        return None
    title = entry.get("title", "Desconocido")
    player.enqueue(url, title)
    return title


def is_radio_candidate(player, entry: dict) -> bool:
    excluded_ids = player.radio.played | {player.last_video_id}
    return is_playable_candidate(entry, RADIO_MAX_DURATION_S, excluded_ids)


def enqueue_radio_pick(player, pick: dict) -> bool:
    title = enqueue_entry(player, pick)
    if title is None:
        return False
    radio = player.radio
    radio.played.add(pick["id"])
    radio.history.append(title)
    return True


async def search_entries(extract, query: str, search_count: int) -> list[dict]:
    info = await extract(query, is_search=True, search_count=search_count)
    entries = info.get("entries") if isinstance(info, dict) else None
    return [e for e in entries or [] if isinstance(e, dict)]


async def radio_next_from_ai(player, extract, query: str) -> bool:
    radio = player.radio
    pending = radio.suggestions
    if not pending:
        recent_titles = list(radio.history)
        pending.extend(await ai_dj.suggest_songs(query, recent_titles, RADIO_BATCH_SIZE))

    while pending:
        suggestion = pending.pop(0)
        try:
            entries = await search_entries(extract, suggestion, search_count=3)
        except Exception as e:
            print(f"Radio IA error buscando '{suggestion}': {e}")
            continue
        pick = next((e for e in entries if is_radio_candidate(player, e)), None)
        if pick and enqueue_radio_pick(player, pick):
            return True
    return False


async def radio_next_from_search(player, extract, query: str) -> bool:
    entries = await search_entries(extract, query, search_count=5)
    candidates = [e for e in entries if is_radio_candidate(player, e)]
    if not candidates:
        player.radio.played = set()
        candidates = [e for e in entries if is_radio_candidate(player, e)]
    if not candidates:
        return False
    return enqueue_radio_pick(player, random.choice(candidates))


async def radio_next(player, extract) -> bool:
    query = player.radio.query
    if not query:
        return False

    try:
        if await radio_next_from_ai(player, extract, query):
            return True
        return await radio_next_from_search(player, extract, query)
    except Exception as e:
        print(f"Radio error: {e}")
        return False
