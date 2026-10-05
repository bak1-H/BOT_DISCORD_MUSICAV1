import asyncio

import discord

from music.ports import ConnectResult

VOICE_CONNECT_TIMEOUT_S = 60


class DiscordVoiceGateway:
    def __init__(self, bot, guild_id: int) -> None:
        self._bot = bot
        self._guild_id = guild_id

    @property
    def client(self) -> discord.VoiceClient | None:
        guild = self._bot.get_guild(self._guild_id)
        return guild.voice_client if guild else None

    async def connect(self, channel: discord.VoiceChannel) -> ConnectResult:
        try:
            await channel.connect(timeout=VOICE_CONNECT_TIMEOUT_S)
        except asyncio.TimeoutError:
            return ConnectResult.TIMEOUT
        except (discord.Forbidden, discord.HTTPException, discord.ClientException) as e:
            print(f"Voice connect error: {e}")
            return ConnectResult.REFUSED
        return ConnectResult.CONNECTED


class ChannelNotifier:
    def __init__(self, player) -> None:
        self._player = player

    async def send(self, content: str | None = None, *, embed: discord.Embed | None = None) -> None:
        channel = self._player.text_channel
        if channel is None:
            return
        try:
            await channel.send(content, embed=embed)
        except Exception as e:
            print(f"Notifier send error: {e}")


def ffmpeg_audio_source(path: str) -> discord.FFmpegPCMAudio:
    return discord.FFmpegPCMAudio(path, options="-vn")
