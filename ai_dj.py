import asyncio
import json
import os
from dataclasses import dataclass

DEFAULT_MODEL = "gemini-3.5-flash-lite"
RADIO_TIMEOUT_S = 12.0
PLAYLIST_TIMEOUT_S = 15.0

_SONG_LIST_SCHEMA = {
    "type": "object",
    "properties": {
        "songs": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["songs"],
}

PLAYLIST_INTENT = "playlist"
UNSUPPORTED_INTENT = "unsupported"

_PLAYLIST_PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "intent": {"type": "string", "enum": [PLAYLIST_INTENT, UNSUPPORTED_INTENT]},
        "summary": {"type": "string"},
        "songs": {"type": "array", "items": {"type": "string"}},
        "max_duration_s": {"type": "integer"},
    },
    "required": ["intent", "summary", "songs", "max_duration_s"],
}

_SONG_RULES = (
    "Only real, official studio songs: no compilations, mixes, full albums, live sets or hour-long videos. "
    'Return each song as a YouTube search string in the form "Artist - Song title".'
)

_client = None


@dataclass
class PlaylistPlan:
    intent: str
    summary: str
    songs: list[str]
    max_duration_s: int

    @property
    def is_playlist(self) -> bool:
        return self.intent == PLAYLIST_INTENT and bool(self.songs)


def _get_client():
    global _client
    if _client is not None:
        return _client
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        return None
    try:
        from google import genai
    except ImportError as e:
        print(f"[ai_dj] google-genai no disponible: {e}")
        return None
    _client = genai.Client(api_key=api_key)
    return _client


async def _generate_json(prompt: str, schema: dict, timeout_s: float) -> dict | None:
    client = _get_client()
    if client is None:
        return None

    from google.genai import types

    try:
        response = await asyncio.wait_for(
            client.aio.models.generate_content(
                model=os.getenv("GEMINI_MODEL", "").strip() or DEFAULT_MODEL,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_json_schema=schema,
                    temperature=1.0,
                    automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
                ),
            ),
            timeout=timeout_s,
        )
        data = json.loads(response.text or "{}")
    except Exception as e:
        print(f"[ai_dj] Error llamando a Gemini: {e!r}")
        return None
    return data if isinstance(data, dict) else None


def _clean_songs(raw_songs, limit: int) -> list[str]:
    if not isinstance(raw_songs, list):
        return []
    return [song.strip() for song in raw_songs if isinstance(song, str) and song.strip()][:limit]


async def suggest_songs(seed: str, recent_titles: list[str], count: int) -> list[str]:
    recent = "\n".join(f"- {title}" for title in recent_titles) or "- (none yet)"
    prompt = (
        "You are the DJ of a Discord music radio.\n"
        f"Radio theme requested by the users: {seed}\n"
        f"Recently played (do not repeat these songs):\n{recent}\n\n"
        f"Suggest {count} different songs that fit the theme and flow naturally after the recent ones. "
        f"Mix artists when the theme allows it. {_SONG_RULES}"
    )
    data = await _generate_json(prompt, _SONG_LIST_SCHEMA, RADIO_TIMEOUT_S)
    return _clean_songs((data or {}).get("songs"), count)


async def plan_playlist(request: str, max_songs: int, default_max_duration_s: int) -> PlaylistPlan | None:
    prompt = (
        "You are the DJ of a Discord music bot. A user wrote this request, usually in Spanish:\n"
        f'"{request}"\n\n'
        f'First classify it. intent="{PLAYLIST_INTENT}" only when the user asks for music to be added '
        "(an artist, genre, mood, era or specific songs). Anything else, such as removing, skipping, "
        "reordering or asking about the current queue, chatting or unclear text, is "
        f'intent="{UNSUPPORTED_INTENT}" with an empty songs list.\n'
        "summary: one short sentence in neutral Latin American Spanish (use tú, never vos) "
        "stating what you understood, e.g. \"10 canciones de Bad Bunny, sin videos largos\". "
        'For unsupported requests, address the user directly, e.g. "Quieres sacar la canción 10 de la cola".\n\n'
        "For playlist requests, build the list of songs that best fulfills it. "
        f"Use the amount of songs the user asks for, never more than {max_songs}; "
        f"if no amount is given, use {min(10, max_songs)}. {_SONG_RULES}\n"
        "Also set max_duration_s: the longest acceptable video length in seconds. "
        "If the user complains about long videos or gives a limit, honor it; "
        f"otherwise use {default_max_duration_s}."
    )
    data = await _generate_json(prompt, _PLAYLIST_PLAN_SCHEMA, PLAYLIST_TIMEOUT_S)
    if data is None:
        return None
    intent = data.get("intent")
    if intent not in (PLAYLIST_INTENT, UNSUPPORTED_INTENT):
        intent = UNSUPPORTED_INTENT
    summary = data.get("summary") if isinstance(data.get("summary"), str) else ""
    max_duration_s = data.get("max_duration_s")
    if not isinstance(max_duration_s, int) or max_duration_s <= 0:
        max_duration_s = default_max_duration_s
    return PlaylistPlan(
        intent=intent,
        summary=summary.strip(),
        songs=_clean_songs(data.get("songs"), max_songs) if intent == PLAYLIST_INTENT else [],
        max_duration_s=max_duration_s,
    )
