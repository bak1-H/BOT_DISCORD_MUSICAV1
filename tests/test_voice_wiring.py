import asyncio
import importlib.util
import sys
import types
from types import SimpleNamespace

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


@pytest.fixture(autouse=True)
def clean_voice_activation(isolated_bot):
    yield
    isolated_bot.bot.voice_activation = None


class RecordingActivation:
    def __init__(self):
        self.clients = []
        self.updates = []

    def ensure_listening(self, client):
        self.clients.append(client)

    def voice_state_changed(self, member, before, after):
        self.updates.append(member)


async def test_flag_off_never_builds_the_activation_and_gateways_have_no_callback(isolated_bot, monkeypatch):
    monkeypatch.delenv("VOICE_ACTIVATION_ENABLED", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "")
    monkeypatch.setattr(isolated_bot, "install_voice_activation", lambda *args, **kwargs: pytest.fail("built"))

    await isolated_bot.bot.setup_hook()

    assert isolated_bot.bot.voice_activation is None
    assert isolated_bot.get_music_service(1003).voice._on_connected is None


async def test_flag_on_hands_the_activation_hook_to_gateways_and_delegates(isolated_bot, monkeypatch, fake_voice_recv):
    monkeypatch.setenv("VOICE_ACTIVATION_ENABLED", "true")
    monkeypatch.setenv("GEMINI_API_KEY", "")
    activation = RecordingActivation()
    monkeypatch.setattr(isolated_bot, "install_voice_activation", lambda *args, **kwargs: activation)

    await isolated_bot.bot.setup_hook()
    gateway = isolated_bot.get_music_service(1004).voice
    gateway._on_connected("client")

    assert isolated_bot.bot.voice_activation is activation
    assert activation.clients == ["client"]


async def test_connect_hook_is_harmless_without_an_activation(isolated_bot):
    isolated_bot.bot.on_voice_connected("client")


async def test_voice_state_updates_reach_the_activation_and_never_break_the_handler(isolated_bot):
    class Exploding(RecordingActivation):
        def voice_state_changed(self, member, before, after):
            raise RuntimeError("boom")

    member = SimpleNamespace(guild=SimpleNamespace(id=1), id=5)
    isolated_bot.bot.voice_activation = RecordingActivation()
    await isolated_bot.on_voice_state_update(member, None, None)
    assert isolated_bot.bot.voice_activation.updates == [member]

    isolated_bot.bot.voice_activation = Exploding()
    await isolated_bot.on_voice_state_update(member, None, None)


def test_activation_without_the_agent_degrades_with_one_line(isolated_bot, capsys):
    assert isolated_bot.install_voice_activation(None, None, {}) is None
    assert capsys.readouterr().out.count("[voz]") == 1


def test_activation_without_the_model_dir_degrades_with_one_line(isolated_bot, capsys):
    assert isolated_bot.install_voice_activation(object(), None, {"GEMINI_API_KEY": "k"}) is None
    assert capsys.readouterr().out.count("[voz]") == 1


def test_activation_without_the_api_key_degrades_with_one_line(isolated_bot, tmp_path, capsys):
    env = {"VOICE_VOSK_MODEL_DIR": str(tmp_path)}

    assert isolated_bot.install_voice_activation(object(), None, env) is None
    assert capsys.readouterr().out.count("[voz]") == 1


def pretend_vosk(monkeypatch, installed):
    original = importlib.util.find_spec

    def find_spec(name, *args, **kwargs):
        if name == "vosk":
            return object() if installed else None
        return original(name, *args, **kwargs)

    monkeypatch.setattr(importlib.util, "find_spec", find_spec)


def test_activation_without_vosk_degrades_with_one_line_before_listening(isolated_bot, tmp_path, monkeypatch, capsys):
    pretend_vosk(monkeypatch, installed=False)
    env = {"VOICE_VOSK_MODEL_DIR": str(tmp_path), "GEMINI_API_KEY": "k"}

    assert isolated_bot.install_voice_activation(object(), None, env) is None

    output = capsys.readouterr().out
    assert output.count("[voz]") == 1
    assert "escucha activa" not in output
    assert "vosk" in output


async def test_activation_is_built_when_everything_is_configured(isolated_bot, tmp_path, monkeypatch, capsys):
    from voice.activation import VoiceActivation

    pretend_vosk(monkeypatch, installed=True)
    env = {"VOICE_VOSK_MODEL_DIR": str(tmp_path), "GEMINI_API_KEY": "k"}

    activation = isolated_bot.install_voice_activation(object(), asyncio.get_running_loop(), env)

    assert isinstance(activation, VoiceActivation)
    assert "[voz] escucha activa" in capsys.readouterr().out
