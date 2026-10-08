import sys
import types

import pytest


@pytest.fixture(autouse=True)
def clean_voice_cls(isolated_bot):
    yield
    isolated_bot.bot.voice_cls = None
    isolated_bot.music_services.clear()


class StubVoiceRecvClient:
    pass


@pytest.fixture
def fake_voice_recv(monkeypatch):
    module = types.ModuleType("discord.ext.voice_recv")
    module.VoiceRecvClient = StubVoiceRecvClient
    monkeypatch.setitem(sys.modules, "discord.ext.voice_recv", module)


@pytest.mark.parametrize("value", ["true", "TRUE", " 1 ", "yes", "on"])
def test_voice_activation_is_enabled_only_by_explicit_values(isolated_bot, value):
    assert isolated_bot.voice_activation_enabled({"VOICE_ACTIVATION_ENABLED": value}) is True


@pytest.mark.parametrize("env", [{}, {"VOICE_ACTIVATION_ENABLED": ""}, {"VOICE_ACTIVATION_ENABLED": "false"}, {"VOICE_ACTIVATION_ENABLED": "0"}])
def test_voice_activation_is_off_by_default(isolated_bot, env):
    assert isolated_bot.voice_activation_enabled(env) is False


def test_flag_off_installs_nothing_and_never_imports_voice_recv(isolated_bot, monkeypatch):
    monkeypatch.delitem(sys.modules, "discord.ext.voice_recv", raising=False)

    assert isolated_bot.install_voice({}) is None
    assert "discord.ext.voice_recv" not in sys.modules


def test_flag_on_returns_the_voice_recv_client_class(isolated_bot, fake_voice_recv):
    assert isolated_bot.install_voice({"VOICE_ACTIVATION_ENABLED": "true"}) is StubVoiceRecvClient


def test_flag_on_with_missing_dependency_leaves_the_bot_alive(isolated_bot, monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "discord.ext.voice_recv", None)

    assert isolated_bot.install_voice({"VOICE_ACTIVATION_ENABLED": "true"}) is None
    assert "[voz] deshabilitado" in capsys.readouterr().out


async def test_setup_hook_without_flag_builds_plain_gateways(isolated_bot, monkeypatch):
    monkeypatch.delenv("VOICE_ACTIVATION_ENABLED", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "")

    await isolated_bot.bot.setup_hook()

    assert isolated_bot.bot.voice_cls is None
    assert isolated_bot.get_music_service(1001).voice._voice_cls is None


async def test_setup_hook_with_flag_hands_the_class_to_new_gateways(isolated_bot, monkeypatch, fake_voice_recv):
    monkeypatch.setenv("VOICE_ACTIVATION_ENABLED", "true")
    monkeypatch.setenv("GEMINI_API_KEY", "")

    await isolated_bot.bot.setup_hook()

    assert isolated_bot.bot.voice_cls is StubVoiceRecvClient
    assert isolated_bot.get_music_service(1002).voice._voice_cls is StubVoiceRecvClient
