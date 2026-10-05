import asyncio
from types import SimpleNamespace

import discord
import pytest

from music.discord_adapters import ChannelNotifier, DiscordVoiceGateway, ffmpeg_audio_source
from music.player import GuildPlayer
from music.ports import ConnectResult
from tests.fakes import FakeAudioSource, FakeVoiceClient, RecordingNotifier


class GuildBook:
    def __init__(self):
        self.guilds = {}
        self.lookups = 0

    def get_guild(self, guild_id):
        self.lookups += 1
        return self.guilds.get(guild_id)


class ExplodingChannel:
    async def send(self, content=None, **kwargs):
        raise discord.HTTPException(SimpleNamespace(status=500, reason="boom"), "boom")


class RefusingChannel:
    def __init__(self, error):
        self.error = error

    async def connect(self, timeout=60):
        raise self.error


class AcceptingChannel:
    def __init__(self):
        self.timeouts = []

    async def connect(self, timeout=60):
        self.timeouts.append(timeout)


def test_gateway_client_is_derived_from_the_guild_on_every_access():
    book = GuildBook()
    first, second = FakeVoiceClient(), FakeVoiceClient()
    guild = SimpleNamespace(voice_client=first)
    book.guilds[1] = guild
    gateway = DiscordVoiceGateway(book, 1)

    assert gateway.client is first
    guild.voice_client = second
    assert gateway.client is second
    guild.voice_client = None
    assert gateway.client is None
    assert book.lookups == 3


def test_gateway_client_is_none_for_an_unknown_guild():
    assert DiscordVoiceGateway(GuildBook(), 99).client is None


async def test_gateway_connect_uses_a_sixty_second_timeout():
    channel = AcceptingChannel()

    result = await DiscordVoiceGateway(GuildBook(), 1).connect(channel)

    assert result is ConnectResult.CONNECTED
    assert channel.timeouts == [60]


@pytest.mark.parametrize(
    "error, expected",
    [
        (asyncio.TimeoutError(), ConnectResult.TIMEOUT),
        (discord.ClientException("busy"), ConnectResult.REFUSED),
        (discord.HTTPException(SimpleNamespace(status=500, reason="x"), "x"), ConnectResult.REFUSED),
    ],
)
async def test_gateway_connect_maps_failures_to_results(error, expected):
    result = await DiscordVoiceGateway(GuildBook(), 1).connect(RefusingChannel(error))

    assert result is expected


async def test_notifier_sends_to_the_channel_the_player_has_at_that_moment():
    player = GuildPlayer(1)
    first, second = RecordingNotifier(), RecordingNotifier()
    notifier = ChannelNotifier(player)

    player.text_channel = first
    await notifier.send("uno")
    player.text_channel = second
    await notifier.send("dos")

    assert first.texts == ["uno"]
    assert second.texts == ["dos"]


async def test_notifier_forwards_embeds():
    player = GuildPlayer(1)
    player.text_channel = RecordingNotifier()
    embed = discord.Embed(title="Hola")

    await ChannelNotifier(player).send(embed=embed)

    assert player.text_channel.embed_titles == ["Hola"]


async def test_notifier_swallows_send_errors():
    player = GuildPlayer(1)
    player.text_channel = ExplodingChannel()

    await ChannelNotifier(player).send("hola")


async def test_notifier_without_a_text_channel_does_nothing():
    await ChannelNotifier(GuildPlayer(1)).send("hola")


def test_ffmpeg_audio_source_builds_the_source_without_video(monkeypatch):
    monkeypatch.setattr(discord, "FFmpegPCMAudio", FakeAudioSource)

    source = ffmpeg_audio_source("song.webm")

    assert source.path == "song.webm"
    assert source.options == {"options": "-vn"}
