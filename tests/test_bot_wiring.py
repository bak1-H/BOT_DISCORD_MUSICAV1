import asyncio
from types import SimpleNamespace

from music.service import MusicService
from tests.fakes import FakeContext, FakeMember, FakeVoiceChannel, settle


async def test_setup_hook_captures_the_running_loop_for_the_music_services(isolated_bot):
    isolated_bot.bot.playback_loop = None

    await isolated_bot.bot.setup_hook()

    assert isolated_bot.bot.playback_loop is asyncio.get_running_loop()


def test_default_help_command_is_disabled(isolated_bot):
    assert isolated_bot.bot.help_command is None
    assert "help" not in {command.name for command in isolated_bot.bot.commands}


async def test_music_service_is_one_per_guild_and_shares_the_registry_player(isolated_bot):
    first = isolated_bot.get_music_service(1)
    second = isolated_bot.get_music_service(2)

    assert isinstance(first, MusicService)
    assert isolated_bot.get_music_service(1) is first
    assert second is not first
    assert first.player is isolated_bot.players.get(1)
    assert second.player is isolated_bot.players.get(2)


async def test_music_service_uses_the_loop_injected_by_the_bot(isolated_bot):
    assert isolated_bot.get_music_service(1).loop is isolated_bot.bot.playback_loop


async def test_voice_state_update_starts_the_alone_watch_when_only_bots_remain(isolated_bot):
    ctx = FakeContext(guild_id=1, connected=True)
    ctx.voice_client.channel = FakeVoiceChannel(ctx=ctx, members=[FakeMember(is_bot=True)])

    await isolated_bot.on_voice_state_update(SimpleNamespace(guild=ctx.guild), None, None)

    task = isolated_bot.players.get(1).alone_task
    assert task is not None and not task.done()


async def test_voice_state_update_cancels_the_alone_watch_when_a_human_is_present(isolated_bot):
    ctx = FakeContext(guild_id=1, connected=True)
    ctx.voice_client.channel = FakeVoiceChannel(ctx=ctx, members=[FakeMember(is_bot=True)])
    await isolated_bot.on_voice_state_update(SimpleNamespace(guild=ctx.guild), None, None)
    task = isolated_bot.players.get(1).alone_task

    ctx.voice_client.channel.members.append(FakeMember(is_bot=False))
    await isolated_bot.on_voice_state_update(SimpleNamespace(guild=ctx.guild), None, None)
    await settle()

    assert isolated_bot.players.get(1).alone_task is None
    assert task.cancelled()
