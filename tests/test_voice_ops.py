from types import SimpleNamespace

import pytest

from tests.fakes import FakeContext, FakeVoiceClient

OWNER_ID = 42


class RecordingSwitch:
    def __init__(self, enabled=True, listening=False):
        self.enabled = enabled
        self.listening = listening
        self.calls = []

    def listening_in(self, guild_id):
        return self.listening

    def enable(self, clients=()):
        self.calls.append(("on", list(clients)))
        self.enabled = True

    def disable(self):
        self.calls.append(("off", None))
        self.enabled = False


@pytest.fixture
def voice_bot(isolated_bot, monkeypatch):
    monkeypatch.setattr(isolated_bot.bot, "owner_id", OWNER_ID)
    yield isolated_bot
    isolated_bot.bot.voice_activation = None


def invoke_as(ctx, user_id):
    ctx.author = SimpleNamespace(id=user_id, voice=ctx.author.voice)
    return ctx


async def test_a_non_owner_is_refused_and_nothing_changes(voice_bot):
    switch = voice_bot.bot.voice_activation = RecordingSwitch()
    ctx = invoke_as(FakeContext(), 7)

    await voice_bot.voz.callback(ctx, "off")

    assert switch.calls == []
    assert switch.enabled is True
    assert ctx.notifier.has_text_containing("Solo el dueño")


async def test_an_owner_check_that_raises_answers_once_and_changes_nothing(voice_bot, monkeypatch):
    switch = voice_bot.bot.voice_activation = RecordingSwitch()
    ctx = invoke_as(FakeContext(), OWNER_ID)

    async def failing_is_owner(user):
        raise RuntimeError("no network")

    monkeypatch.setattr(voice_bot.bot, "is_owner", failing_is_owner)

    await voice_bot.voz.callback(ctx, "off")

    assert switch.calls == []
    assert switch.enabled is True
    assert ctx.notifier.has_text_containing("No pude verificar")


async def test_the_owner_turns_the_listening_off_and_on(voice_bot):
    switch = voice_bot.bot.voice_activation = RecordingSwitch()
    ctx = invoke_as(FakeContext(), OWNER_ID)

    await voice_bot.voz.callback(ctx, "off")
    assert switch.calls == [("off", None)]
    assert ctx.notifier.has_text_containing("desactivada")

    await voice_bot.voz.callback(ctx, "ON")
    assert switch.calls[-1][0] == "on"
    assert ctx.notifier.has_text_containing("**activada**")


async def test_status_reports_the_switch_and_the_listening_of_the_invoker_guild(voice_bot):
    voice_bot.bot.voice_activation = RecordingSwitch(enabled=True, listening=True)
    ctx = invoke_as(FakeContext(), OWNER_ID)

    await voice_bot.voz.callback(ctx, "estado")

    assert ctx.notifier.has_text_containing("**activada**")
    assert ctx.notifier.has_text_containing("**escuchando**")


async def test_an_unknown_action_shows_the_usage_and_changes_nothing(voice_bot):
    switch = voice_bot.bot.voice_activation = RecordingSwitch()
    ctx = invoke_as(FakeContext(), OWNER_ID)

    await voice_bot.voz.callback(ctx, "quizas")

    assert switch.calls == []
    assert ctx.notifier.has_text_containing("Uso:")


async def test_without_the_activation_the_owner_is_told_it_is_unavailable(voice_bot):
    ctx = invoke_as(FakeContext(), OWNER_ID)

    await voice_bot.voz.callback(ctx, "on")

    assert ctx.notifier.has_text_containing("no está habilitada")


async def test_salir_disconnects_and_replies(isolated_bot, ctx):
    client = ctx.voice_client
    isolated_bot.players.get(1).enqueue("u", "t")

    await isolated_bot.salir.callback(ctx)

    assert client.disconnect_calls == 1
    assert isolated_bot.players.get(1).queue == []
    assert ctx.notifier.has_text_containing("Me fui")


async def test_salir_outside_voice_says_so(isolated_bot, disconnected_ctx):
    await isolated_bot.salir.callback(disconnected_ctx)

    assert disconnected_ctx.notifier.has_text_containing("No estoy en un canal")


def test_salir_has_the_leave_alias(isolated_bot):
    assert "leave" in isolated_bot.salir.aliases


async def test_services_keep_alive_only_with_an_enabled_activation(isolated_bot):
    bot = isolated_bot.bot
    service = isolated_bot.get_music_service(5)

    assert service._keep_alive() is False

    bot.voice_activation = RecordingSwitch(enabled=True)
    try:
        assert service._keep_alive() is True
        bot.voice_activation.enabled = False
        assert service._keep_alive() is False
    finally:
        bot.voice_activation = None


@pytest.mark.parametrize(
    "env,expected",
    [({}, 900), ({"VOICE_IDLE_MINUTES": "5"}, 300), ({"VOICE_IDLE_MINUTES": "0"}, 900), ({"VOICE_IDLE_MINUTES": "x"}, 900)],
)
def test_idle_minutes_come_from_the_environment_with_a_safe_default(isolated_bot, env, expected):
    assert isolated_bot.voice_idle_seconds(env) == expected
