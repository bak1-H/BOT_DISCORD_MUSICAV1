import asyncio
from types import SimpleNamespace

import discord
import pytest

from tests.discord_support import FakeTextChannel
from tests.voice_support import FakeDetector, FakeTranscriber
from voice.activation import PRIVACY_NOTICE, VoiceActivation
from voice.pipeline import ActivationSink

GUILD_ID = 1
VOICE_CHANNEL_ID = 70
BOT_ID = 999


class FakeListenClient:
    def __init__(self, voice_channel_id=VOICE_CHANNEL_ID, connected=True):
        self.guild = SimpleNamespace(id=GUILD_ID)
        self.channel = SimpleNamespace(id=voice_channel_id)
        self.connected = connected
        self.listening = False
        self.sinks = []
        self.listen_calls = 0
        self.report_not_listening = False
        self.stop_listening_calls = 0

    def stop_listening(self):
        self.stop_listening_calls += 1
        self.listening = False

    def is_connected(self):
        return self.connected

    def is_listening(self):
        return False if self.report_not_listening else self.listening

    def listen(self, sink, *, after=None):
        self.listen_calls += 1
        if self.listening:
            raise discord.ClientException("Already receiving audio.")
        self.listening = True
        self.sinks.append(sink)


class Rig:
    def __init__(self, text_channel="default"):
        self.channel = FakeTextChannel(channel_id=50) if text_channel == "default" else text_channel
        self.player = SimpleNamespace(text_channel=self.channel)
        self.activation = VoiceActivation(
            FakeDetector(),
            SimpleNamespace(),
            FakeTranscriber(),
            lambda guild_id: self.player,
            asyncio.get_running_loop(),
            500,
            notice_poll_s=0.01,
        )
        self.client = FakeListenClient()

    async def settle(self):
        while self.activation._notice_tasks:
            await asyncio.gather(*self.activation._notice_tasks)

    @property
    def notices(self):
        return [sent.content for sent in self.channel.sent]


@pytest.fixture
async def rig():
    instance = Rig()
    yield instance
    instance.activation.forget(GUILD_ID)


async def test_ensure_listening_attaches_an_activation_sink(rig):
    assert rig.activation.ensure_listening(rig.client) is True

    assert rig.client.listen_calls == 1
    assert isinstance(rig.client.sinks[0], ActivationSink)


async def test_ensure_listening_is_idempotent_while_the_client_is_listening(rig):
    rig.activation.ensure_listening(rig.client)

    assert rig.activation.ensure_listening(rig.client) is False
    assert rig.client.listen_calls == 1


async def test_already_receiving_race_is_swallowed_and_keeps_the_running_pipeline(rig, capsys):
    rig.activation.ensure_listening(rig.client)
    running = rig.activation._pipelines[GUILD_ID]
    rig.client.report_not_listening = True

    assert rig.activation.ensure_listening(rig.client) is False

    assert rig.client.listen_calls == 2
    assert rig.activation._pipelines[GUILD_ID] is running
    assert not running.stopped
    assert "[voz] activación: listen ClientException" in capsys.readouterr().out


async def test_a_disconnected_client_is_not_asked_to_listen(rig):
    rig.client.connected = False

    assert rig.activation.ensure_listening(rig.client) is False
    assert rig.client.listen_calls == 0


async def test_a_client_without_reception_is_left_alone(rig):
    assert rig.activation.ensure_listening(SimpleNamespace(is_connected=lambda: True)) is False


async def test_a_client_that_fails_to_report_its_state_is_logged_once(rig, capsys):
    class Broken:
        def listen(self, sink, after=None):
            raise AssertionError

        def is_connected(self):
            raise RuntimeError

    assert rig.activation.ensure_listening(Broken()) is False
    assert rig.activation.ensure_listening(Broken()) is False

    assert capsys.readouterr().out.count("state RuntimeError") == 1


async def test_listening_is_rearmed_after_the_router_dies_and_the_old_worker_stops(rig):
    rig.activation.ensure_listening(rig.client)
    first = rig.activation._pipelines[GUILD_ID]
    rig.client.listening = False

    assert rig.activation.ensure_listening(rig.client) is True

    assert rig.client.listen_calls == 2
    assert first.stopped
    assert rig.activation._pipelines[GUILD_ID] is not first


async def test_the_privacy_notice_is_sent_once_and_not_on_reconnects(rig):
    rig.activation.ensure_listening(rig.client)
    await rig.settle()
    rig.client.listening = False
    rig.activation.ensure_listening(rig.client)
    await rig.settle()

    assert rig.notices == [PRIVACY_NOTICE]


async def test_the_privacy_notice_is_sent_again_for_a_different_voice_channel(rig):
    rig.activation.ensure_listening(rig.client)
    await rig.settle()
    other = FakeListenClient(voice_channel_id=71)

    rig.activation.ensure_listening(other)
    await rig.settle()

    assert rig.notices == [PRIVACY_NOTICE, PRIVACY_NOTICE]


async def test_the_privacy_notice_waits_for_the_text_channel_to_exist():
    rig = Rig(text_channel=None)
    try:
        rig.activation.ensure_listening(rig.client)
        await asyncio.sleep(0.03)
        rig.player.text_channel = rig.channel = FakeTextChannel(channel_id=51)
        await rig.settle()

        assert rig.notices == [PRIVACY_NOTICE]
    finally:
        rig.activation.forget(GUILD_ID)


class OrderedTranscriber(FakeTranscriber):
    def __init__(self, channel):
        super().__init__("")
        self.channel = channel
        self.notices_seen = []

    async def transcribe(self, wav_bytes):
        self.notices_seen.append(len(self.channel.sent))
        return await super().transcribe(wav_bytes)


def session_listener():
    async def handle_request(message, request):
        return None

    return SimpleNamespace(is_limited=lambda user_id: False, is_busy=lambda guild_id: False, handle_request=handle_request)


async def test_a_late_text_channel_gets_the_notice_exactly_once_and_before_the_first_transcription():
    rig = Rig(text_channel=None)
    transcriber = OrderedTranscriber(None)
    rig.activation._transcriber = transcriber
    rig.activation._listener = session_listener()
    try:
        rig.activation.ensure_listening(rig.client)
        rig.player.text_channel = rig.channel = transcriber.channel = FakeTextChannel(channel_id=51)
        session = rig.activation._session_for(SimpleNamespace(id=GUILD_ID))

        await session._transcribe_and_handle(SimpleNamespace(id=7, display_name="Maxi"), b"\x00\x01")
        await rig.settle()

        assert transcriber.notices_seen == [1]
        assert rig.notices == [PRIVACY_NOTICE]
    finally:
        rig.activation.forget(GUILD_ID)


async def test_without_a_text_channel_for_the_notice_the_transcriber_is_never_called():
    rig = Rig()
    transcriber = rig.activation._transcriber
    rig.activation._listener = session_listener()
    session = rig.activation._session_for(SimpleNamespace(id=GUILD_ID))
    rig.player.text_channel = None

    await session._transcribe_and_handle(SimpleNamespace(id=7, display_name="Maxi"), b"\x00\x01")

    assert transcriber.calls == []
    assert rig.notices == []


async def test_a_notice_that_fails_to_send_blocks_transcription_and_is_retried(capsys):
    rig = Rig()
    transcriber = rig.activation._transcriber
    rig.activation._listener = session_listener()
    session = rig.activation._session_for(SimpleNamespace(id=GUILD_ID))
    rig.channel.send_error = RuntimeError("down")

    await session._transcribe_and_handle(SimpleNamespace(id=7, display_name="Maxi"), b"\x00\x01")
    assert transcriber.calls == []

    rig.channel.send_error = None
    await session._transcribe_and_handle(SimpleNamespace(id=7, display_name="Maxi"), b"\x00\x01")

    assert len(transcriber.calls) == 1
    assert rig.notices == [PRIVACY_NOTICE]


def member(user_id, guild):
    return SimpleNamespace(id=user_id, guild=guild)


def make_guild(client):
    return SimpleNamespace(id=GUILD_ID, me=SimpleNamespace(id=BOT_ID), voice_client=client)


def state(channel_id):
    return SimpleNamespace(channel=SimpleNamespace(id=channel_id) if channel_id else None)


async def test_the_bot_leaving_voice_stops_the_worker_and_drops_the_session(rig):
    rig.activation.ensure_listening(rig.client)
    pipeline = rig.activation._pipelines[GUILD_ID]
    guild = make_guild(None)

    rig.activation.voice_state_changed(member(BOT_ID, guild), state(70), state(None))

    assert pipeline.stopped
    assert GUILD_ID not in rig.activation._pipelines


async def test_a_member_leaving_drops_their_state(rig):
    rig.activation.ensure_listening(rig.client)
    pipeline = rig.activation._pipelines[GUILD_ID]
    departed = []
    pipeline.forget = departed.append
    guild = make_guild(rig.client)

    rig.activation.voice_state_changed(member(7, guild), state(70), state(None))

    assert departed == [7]
    assert not pipeline.stopped


async def test_a_voice_state_update_rearms_a_dead_listener(rig):
    rig.activation.ensure_listening(rig.client)
    rig.client.listening = False
    guild = make_guild(rig.client)

    rig.activation.voice_state_changed(member(7, guild), state(70), state(70))

    assert rig.client.listen_calls == 2


async def test_a_voice_state_update_while_listening_does_not_listen_again(rig):
    rig.activation.ensure_listening(rig.client)
    guild = make_guild(rig.client)

    rig.activation.voice_state_changed(member(7, guild), state(70), state(70))

    assert rig.client.listen_calls == 1


async def test_a_session_is_built_lazily_for_the_text_channel_and_reused(rig):
    guild = SimpleNamespace(id=GUILD_ID)

    first = rig.activation._session_for(guild)
    second = rig.activation._session_for(guild)

    assert first is second


async def test_without_a_text_channel_there_is_no_session():
    rig = Rig(text_channel=None)

    assert rig.activation._session_for(SimpleNamespace(id=GUILD_ID)) is None


async def test_disable_stops_listening_everywhere_and_drops_the_open_session(rig):
    rig.activation.ensure_listening(rig.client)
    pipeline = rig.activation._pipelines[GUILD_ID]
    rig.activation._session_for(SimpleNamespace(id=GUILD_ID))

    rig.activation.disable()

    assert rig.activation.enabled is False
    assert pipeline.stopped
    assert rig.client.stop_listening_calls == 1
    assert rig.activation.listening_in(GUILD_ID) is False
    assert rig.activation._sessions == {}


async def test_new_triggers_are_prevented_while_disabled(rig):
    rig.activation.disable()

    assert rig.activation.ensure_listening(rig.client) is False
    assert rig.client.listen_calls == 0

    guild = make_guild(rig.client)
    rig.activation.voice_state_changed(member(7, guild), state(70), state(70))
    assert rig.client.listen_calls == 0


async def test_a_command_already_captured_never_reaches_the_transcriber_after_disable(rig):
    rig.activation._listener = session_listener()
    transcriber = rig.activation._transcriber
    session = rig.activation._session_for(SimpleNamespace(id=GUILD_ID))

    rig.activation.disable()
    await session._transcribe_and_handle(SimpleNamespace(id=7, display_name="Maxi"), b"\x00\x01")

    assert transcriber.calls == []


async def test_enable_rearms_listening_on_the_connected_clients(rig):
    rig.activation.disable()

    rig.activation.enable([rig.client])

    assert rig.activation.enabled is True
    assert rig.client.listen_calls == 1
    assert rig.activation.listening_in(GUILD_ID) is True


async def test_enable_does_not_repeat_the_privacy_notice(rig):
    rig.activation.ensure_listening(rig.client)
    await rig.settle()
    rig.activation.disable()

    rig.activation.enable([rig.client])
    await rig.settle()

    assert rig.notices == [PRIVACY_NOTICE]
