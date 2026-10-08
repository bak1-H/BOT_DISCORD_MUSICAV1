from enum import Enum
from typing import Protocol

import discord


class ConnectResult(Enum):
    CONNECTED = "connected"
    ALREADY_CONNECTED = "already_connected"
    TIMEOUT = "timeout"
    REFUSED = "refused"


class VoiceGateway(Protocol):
    @property
    def client(self) -> discord.VoiceClient | None: ...

    async def connect(self, channel: discord.VoiceChannel) -> ConnectResult: ...


class Notifier(Protocol):
    async def send(self, content: str | None = None, *, embed: discord.Embed | None = None) -> None: ...


class Extractor(Protocol):
    async def search(self, query: str, count: int) -> list[dict]: ...

    async def download(self, guild_id: int, url: str) -> tuple[dict, str, str]: ...

    def discard_download(self, guild_id: int, url: str) -> None: ...
