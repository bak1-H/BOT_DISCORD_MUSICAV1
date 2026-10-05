import sys

import pytest
from discord.ext import commands

from agent.adapters import GeniusLyrics, GuildPlaylists
from agent.listener import AgentListener
from tests.discord_support import FakeIncomingMessage, FakeSender
from tests.agent_support import GID


class NullRunner:
    async def run(self, ctx, text):
        raise AssertionError("not expected in wiring tests")


@pytest.fixture
def agent_env(monkeypatch):
    import agent.model

    monkeypatch.setattr(agent.model, "build_runner", lambda env=None: NullRunner())
    yield {"GEMINI_API_KEY": "test-key"}


@pytest.fixture(autouse=True)
def clean_listeners(isolated_bot):
    yield
    for listener in list(isolated_bot.bot.extra_events.get("on_message", [])):
        isolated_bot.bot.remove_listener(listener, "on_message")
    isolated_bot.bot.agent_listener = None


@pytest.mark.parametrize("value", ["false", "FALSE", " 0 ", "no", "off"])
def test_kill_switch_values_disable_the_agent(isolated_bot, value):
    assert isolated_bot.agent_enabled({"AGENT_ENABLED": value}) is False


@pytest.mark.parametrize("env", [{}, {"AGENT_ENABLED": ""}, {"AGENT_ENABLED": "true"}, {"AGENT_ENABLED": "1"}])
def test_agent_is_enabled_by_default(isolated_bot, env):
    assert isolated_bot.agent_enabled(env) is True


def test_disabled_agent_registers_no_listener_and_never_imports_the_model(isolated_bot, monkeypatch):
    monkeypatch.setitem(sys.modules, "agent.model", None)

    assert isolated_bot.install_agent({"AGENT_ENABLED": "false", "GEMINI_API_KEY": "k"}) is None
    assert isolated_bot.bot.extra_events.get("on_message", []) == []


def test_missing_gemini_key_leaves_the_bot_without_agent_and_without_crashing(isolated_bot):
    assert isolated_bot.install_agent({"GEMINI_API_KEY": ""}) is None
    assert isolated_bot.bot.extra_events.get("on_message", []) == []


def test_import_failure_of_the_agent_package_leaves_the_bot_alive(isolated_bot, monkeypatch, agent_env):
    monkeypatch.setitem(sys.modules, "agent.listener", None)

    assert isolated_bot.install_agent(agent_env) is None
    assert isolated_bot.bot.extra_events.get("on_message", []) == []


def test_enabled_agent_registers_an_on_message_listener(isolated_bot, agent_env):
    listener = isolated_bot.install_agent(agent_env)

    assert isinstance(listener, AgentListener)
    assert listener.on_message in isolated_bot.bot.extra_events["on_message"]


def test_default_message_handler_still_processes_commands(isolated_bot, agent_env):
    isolated_bot.install_agent(agent_env)

    assert type(isolated_bot.bot).on_message is commands.Bot.on_message


async def test_setup_hook_installs_the_agent_once(isolated_bot, agent_env, monkeypatch):
    for key, value in agent_env.items():
        monkeypatch.setenv(key, value)

    await isolated_bot.bot.setup_hook()
    await isolated_bot.bot.setup_hook()

    assert len(isolated_bot.bot.extra_events["on_message"]) == 1
    assert isolated_bot.bot.agent_listener is not None


async def test_setup_hook_without_gemini_key_keeps_commands_working(isolated_bot, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "")

    await isolated_bot.bot.setup_hook()

    assert isolated_bot.bot.agent_listener is None
    assert isolated_bot.bot.playback_loop is not None
    assert "play" in {command.name for command in isolated_bot.bot.commands}


async def test_run_context_is_wired_with_lol_lyrics_playlists_and_guild_music(isolated_bot, agent_env):
    listener = isolated_bot.install_agent(agent_env)
    message = FakeIncomingMessage("makakiño hola", author=FakeSender(user_id=3))

    ctx = listener._context_factory(message)

    assert ctx.lol is isolated_bot.lol_service
    assert isinstance(ctx.lyrics, GeniusLyrics)
    assert isinstance(ctx.playlists, GuildPlaylists)
    assert ctx.playlists._playlists_dir == isolated_bot.PLAYLISTS_DIR
    assert ctx.music is isolated_bot.get_music_service(GID)
    assert ctx.voice_channel is message.author.voice.channel


def test_agent_startup_failure_log_prints_only_the_exception_type(isolated_bot, monkeypatch, agent_env, capsys):
    def explode(env):
        raise RuntimeError("secret-api-key-123")

    monkeypatch.setattr("agent.model.build_runner", explode)

    assert isolated_bot.install_agent(agent_env) is None
    printed = capsys.readouterr().out
    assert "RuntimeError" in printed
    assert "secret-api-key-123" not in printed
