import asyncio
import os
from collections import deque
from dataclasses import dataclass, field

RADIO_HISTORY_SIZE = 10
LOOP_OFF = "off"


@dataclass(frozen=True)
class Track:
    entry_id: int
    url: str
    title: str


def new_radio_history() -> deque:
    return deque(maxlen=RADIO_HISTORY_SIZE)


@dataclass
class RadioState:
    query: str | None = None
    played: set = field(default_factory=set)
    history: deque = field(default_factory=new_radio_history)
    suggestions: list = field(default_factory=list)

    def reset_pool(self) -> None:
        self.played = set()
        self.history = new_radio_history()
        self.suggestions = []

    def clear(self) -> None:
        self.query = None
        self.reset_pool()


@dataclass
class GuildPlayer:
    guild_id: int
    queue: list = field(default_factory=list)
    current: dict | None = None
    loop_mode: str = LOOP_OFF
    radio: RadioState = field(default_factory=RadioState)
    last_video_id: str | None = None
    fail_count: int = 0
    audio_file: str | None = None
    text_channel: object | None = None
    alone_task: asyncio.Task | None = None
    voice_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    playback_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    next_entry_id: int = 1

    def make_track(self, url: str, title: str) -> Track:
        track = Track(self.next_entry_id, url, title)
        self.next_entry_id += 1
        return track

    def enqueue(self, url: str, title: str) -> Track:
        track = self.make_track(url, title)
        self.queue.append(track)
        return track

    def enqueue_front(self, url: str, title: str) -> Track:
        track = self.make_track(url, title)
        self.queue.insert(0, track)
        return track

    def cleanup_audio_file(self) -> None:
        path, self.audio_file = self.audio_file, None
        if path and os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass

    def reset_session(self) -> None:
        self.cleanup_audio_file()
        self.queue = []
        self.current = None
        self.loop_mode = LOOP_OFF
        self.radio.clear()


class PlayerRegistry:
    def __init__(self) -> None:
        self._players: dict[int, GuildPlayer] = {}

    def get(self, guild_id: int) -> GuildPlayer:
        player = self._players.get(guild_id)
        if player is None:
            player = GuildPlayer(guild_id)
            self._players[guild_id] = player
        return player

    def all(self) -> list:
        return list(self._players.values())
