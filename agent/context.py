from dataclasses import dataclass, field
from typing import Protocol

from lol.riot import LolPlayer
from lol.service import Comparison
from music.service import MusicService

MAX_TOOL_CALLS_PER_TURN = 6
MAX_DESTRUCTIVE_PER_TURN = 3
MAX_SONGS_PER_TURN = 20


@dataclass(frozen=True)
class LyricsResult:
    title: str
    artist: str
    text: str


class LyricsPort(Protocol):
    async def search(self, title: str) -> LyricsResult | None: ...


class PlaylistsPort(Protocol):
    def load(self) -> dict: ...

    def save(self, data: dict) -> None: ...


class LolPort(Protocol):
    async def summoner(self, riot_id: str) -> LolPlayer: ...

    async def compare(self, riot_id_a: str, riot_id_b: str) -> Comparison: ...


@dataclass(frozen=True)
class PendingAction:
    kind: str
    prompt: str
    payload: dict


@dataclass
class TurnLedger:
    tool_calls: int = 0
    destructive: int = 0
    songs_queued: int = 0
    songs_saved: int = 0
    limit_hit: bool = False
    executed: list = field(default_factory=list)
    pending: list = field(default_factory=list)


@dataclass
class RunContext:
    guild_id: int
    channel_id: int
    author_id: int
    music: MusicService
    voice_channel: object | None = None
    text_channel: object | None = None
    playlists: PlaylistsPort | None = None
    lyrics: LyricsPort | None = None
    lol: LolPort | None = None
    ledger: TurnLedger = field(default_factory=TurnLedger)
