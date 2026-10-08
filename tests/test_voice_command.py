import asyncio
import re
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from agent.adapters import RunContextFactory
from agent.confirm import ConfirmView
from agent.context import PendingAction
from agent.listener import RATE_LIMITED, AgentListener, UserRateLimiter
from agent.runner import AgentReply
from tests.discord_support import FakeInteraction, FakeTextChannel
from tests.fakes import RecordingNotifier
from tests.voice_support import FRAME_BYTES, FRAME_SECONDS, FakeClock, FakeTranscriber, silence, tone
from voice import debug
from voice.command import STT_FAILED, VoiceCommandSession, strip_wake_phrase
from voice.window import WindowState

GUILD = SimpleNamespace(id=1)
OTHER_USER = 8


def make_member(user_id=7):
    return SimpleNamespace(id=user_id, display_name="Maxi", voice=SimpleNamespace(channel=object()), bot=False)


class StubRunner:
    def __init__(self, reply=None, gate=None):
        self.reply = reply or AgentReply("listo")
        self.gate = gate
        self.calls = []

    async def run(self, ctx, text):
        self.calls.append((ctx, text))
        if self.gate is not None:
            await self.gate.wait()
        return self.reply


class Rig:
    def __init__(self, transcriber, runner=None, stt_timeout_s=5.0):
        self.clock = FakeClock()
        self.runner = runner or StubRunner()
        self.channel = FakeTextChannel(channel_id=50)
        self.notifier = RecordingNotifier()
        self.limiter = UserRateLimiter(clock=self.clock)
        self.transcriber = transcriber
        factory = RunContextFactory(lambda guild_id: object(), None, None, lambda: "playlists")
        self.listener = AgentListener(self.runner, factory, SimpleNamespace(user=SimpleNamespace(id=999)), rate_limiter=self.limiter)
        self.session = VoiceCommandSession(
            GUILD,
            self.channel,
            self.listener,
            transcriber,
            self.notifier,
            clock=self.clock,
            stt_timeout_s=stt_timeout_s,
            rms_threshold=500,
        )
        self.member = make_member()

    def speak(self, seconds=1.0):
        pcm = tone(seconds)
        for start in range(0, len(pcm), FRAME_BYTES):
            self.clock.advance(FRAME_SECONDS)
            self.session.window.feed(self.member.id, pcm[start : start + FRAME_BYTES])

    async def say_command(self):
        assert self.session.trigger(self.member) is True
        self.speak()
        self.clock.advance(1.5)
        self.session.window.tick()
        await self.session.join()

    @property
    def sent_texts(self):
        return [sent.content for sent in self.channel.sent]


@pytest.mark.parametrize(
    "heard",
    [
        "maca quino skipea este tema",
        "maca kino skipea este tema",
        "macaquino skipea este tema",
        "makakino skipea este tema",
        "makakiño skipea este tema",
        "Oye Makakiño, skipea este tema",
        "oye maca quino skipea este tema",
        "OYE MACA KINO: skipea este tema",
        "skipea este tema maca quino",
        "Oye, Maca, Kino, skipea este tema",
        "Maca, Kino. skipea este tema",
    ],
)
def test_wake_phrase_variants_are_stripped_from_the_transcript(heard):
    assert strip_wake_phrase(heard) == "skipea este tema"


@pytest.mark.parametrize("heard", ["", "   ", "maca quino", "Oye, Makakiño.", "oye maca kino!!", "makakino maca quino", "...", "Maca, Kino.", "Oye, Maca, Kino,"])
def test_name_only_or_empty_transcripts_become_nothing(heard):
    assert strip_wake_phrase(heard) == ""


def test_text_without_the_wake_phrase_is_left_alone():
    assert strip_wake_phrase("pon tusa de karol g") == "pon tusa de karol g"


def test_pon_rock_survives_a_punctuated_wake_phrase():
    assert strip_wake_phrase("Oye, Maca, Kino, pon rock") == "pon rock"


@pytest.mark.parametrize("heard", ["macarena", "cinema kino", "maca"])
def test_lookalike_words_are_not_stripped(heard):
    assert strip_wake_phrase(heard) == heard


async def test_spoken_command_reaches_the_agent_and_replies_in_the_text_channel():
    rig = Rig(FakeTranscriber("oye makakiño skipea este tema"))

    await rig.say_command()

    assert [text for _, text in rig.runner.calls] == ["skipea este tema"]
    assert rig.sent_texts == ["listo"]
    assert rig.notifier.texts == ["Te escucho, Maxi."]
    assert len(rig.transcriber.calls) == 1
    assert rig.transcriber.calls[0][:4] == b"RIFF"


async def test_voice_turn_shares_the_text_thread_key():
    rig = Rig(FakeTranscriber("makakino pon tusa"))

    await rig.say_command()

    ctx = rig.runner.calls[0][0]
    assert (ctx.channel_id, ctx.author_id, ctx.guild_id) == (50, rig.member.id, 1)
    assert ctx.text_channel is rig.channel


async def test_command_said_in_the_same_breath_skips_the_listening_notice():
    rig = Rig(FakeTranscriber("maca quino pon tusa"))

    assert rig.session.trigger(rig.member, tone(0.9) + silence(0.8)) is True
    await rig.session.join()

    assert rig.notifier.texts == []
    assert [text for _, text in rig.runner.calls] == ["pon tusa"]


@pytest.mark.parametrize("heard", ["maca quino", "oye makakiño", "", "   "])
async def test_casual_mention_or_empty_transcript_is_a_silent_noop(heard):
    rig = Rig(FakeTranscriber(heard))

    await rig.say_command()

    assert rig.runner.calls == []
    assert rig.sent_texts == []
    assert rig.notifier.texts == ["Te escucho, Maxi."]
    assert rig.session.window.busy is False


async def test_destructive_action_still_waits_for_the_confirm_button_of_the_same_user():
    pending = PendingAction("stop", "¿Seguro?", {})
    rig = Rig(FakeTranscriber("makakino detén todo"), StubRunner(AgentReply("¿Seguro?", pending=(pending,))))

    await rig.say_command()

    sent = rig.channel.sent[0]
    assert isinstance(sent.view, ConfirmView)
    assert sent.view.message is sent
    assert await sent.view.interaction_check(FakeInteraction(user_id=rig.member.id)) is True
    assert await sent.view.interaction_check(FakeInteraction(user_id=OTHER_USER)) is False


async def test_rate_limited_user_never_reaches_the_transcriber():
    rig = Rig(FakeTranscriber("makakino pon tusa"))
    rig.limiter.consume(rig.member.id)

    await rig.say_command()

    assert rig.transcriber.calls == []
    assert rig.runner.calls == []
    assert rig.notifier.texts == ["Te escucho, Maxi.", RATE_LIMITED]
    assert rig.session.window.busy is False


@pytest.mark.parametrize("failure", [{"error": RuntimeError("quota")}, {"hang": True}])
async def test_stt_failure_or_timeout_gives_one_notice_and_releases_the_window(failure):
    rig = Rig(FakeTranscriber(**failure), stt_timeout_s=0.01)

    await rig.say_command()

    assert rig.runner.calls == []
    assert rig.notifier.texts == ["Te escucho, Maxi.", STT_FAILED]
    assert rig.session.window.busy is False
    assert rig.session.trigger(rig.member) is True


async def test_trigger_is_ignored_while_a_voice_turn_is_in_flight():
    rig = Rig(FakeTranscriber("makakino pon tusa"))
    assert rig.session.trigger(rig.member) is True

    assert rig.session.trigger(make_member(OTHER_USER)) is False

    assert rig.notifier.texts == []
    await rig.session.join()
    assert rig.notifier.texts == ["Te escucho, Maxi."]


async def test_trigger_is_ignored_while_a_text_turn_holds_the_guild():
    gate = asyncio.Event()
    rig = Rig(FakeTranscriber(""), StubRunner(gate=gate))
    text_turn = asyncio.create_task(rig.listener.handle_request(make_text_message(), "pon algo"))
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert rig.listener.is_busy(GUILD.id) is True
    assert rig.session.trigger(rig.member) is False

    gate.set()
    await text_turn
    assert rig.listener.is_busy(GUILD.id) is False
    assert rig.session.trigger(rig.member) is True


async def test_trigger_is_ignored_while_a_sent_voice_turn_is_running():
    gate = asyncio.Event()
    rig = Rig(FakeTranscriber("makakino pon tusa"), StubRunner(gate=gate))
    assert rig.session.trigger(rig.member) is True
    rig.speak()
    rig.clock.advance(1.5)
    rig.session.window.tick()
    for _ in range(5):
        await asyncio.sleep(0)

    assert rig.session.window.state is WindowState.SENT
    assert rig.listener.is_busy(GUILD.id) is True
    assert rig.session.trigger(make_member(OTHER_USER)) is False

    gate.set()
    await rig.session.join()
    assert rig.session.trigger(rig.member) is True


async def test_rate_limit_notice_is_sent_once_per_limited_window():
    rig = Rig(FakeTranscriber("makakino pon tusa"))
    rig.limiter.consume(rig.member.id)

    await rig.say_command()
    rig.limiter.consume(rig.member.id)
    await rig.say_command()

    assert rig.notifier.texts.count(RATE_LIMITED) == 1
    assert rig.transcriber.calls == []


async def test_failing_dispatch_releases_the_guild_and_a_later_trigger_works():
    rig = Rig(FakeTranscriber("makakino pon tusa"))
    original = rig.session._spawn
    failures = []

    def failing_spawn(coroutine):
        coroutine.close()
        failures.append(1)
        raise RuntimeError("no loop")

    rig.session._spawn = failing_spawn
    assert rig.session.trigger(rig.member, tone(0.8) + silence(0.8)) is False
    assert rig.session.window.busy is False
    rig.session._spawn = original

    await rig.say_command()

    assert failures == [1]
    assert [text for _, text in rig.runner.calls] == ["pon tusa"]


async def test_dispatch_from_a_plain_thread_uses_the_injected_loop():
    rig = Rig(FakeTranscriber("makakino pon tusa"))
    session = VoiceCommandSession(
        GUILD,
        rig.channel,
        rig.listener,
        rig.transcriber,
        rig.notifier,
        clock=rig.clock,
        loop=asyncio.get_running_loop(),
        rms_threshold=500,
    )

    def speak_from_worker():
        assert session.trigger(rig.member) is True
        pcm = tone(1.0)
        for start in range(0, len(pcm), FRAME_BYTES):
            rig.clock.advance(FRAME_SECONDS)
            session.window.feed(rig.member.id, pcm[start : start + FRAME_BYTES])
        rig.clock.advance(1.5)
        session.window.tick()

    await asyncio.to_thread(speak_from_worker)
    await session.join()

    assert [text for _, text in rig.runner.calls] == ["pon tusa"]
    assert rig.notifier.texts == ["Te escucho, Maxi."]


async def test_dispatch_from_a_plain_thread_without_a_loop_releases_the_window():
    rig = Rig(FakeTranscriber("makakino pon tusa"))

    results = await asyncio.to_thread(rig.session.trigger, rig.member)

    assert results is False
    assert rig.session.window.busy is False


def test_listener_is_limited_follows_the_rate_limiter():
    rig = Rig(FakeTranscriber(""))

    assert rig.listener.is_limited(7) is False
    rig.limiter.consume(7)
    assert rig.listener.is_limited(7) is True
    assert rig.listener.is_limited(OTHER_USER) is False
    rig.clock.advance(3600.0)
    assert rig.listener.is_limited(7) is False


async def test_listener_is_busy_only_while_the_guild_lock_is_held():
    gate = asyncio.Event()
    rig = Rig(FakeTranscriber(""), StubRunner(gate=gate))

    assert rig.listener.is_busy(GUILD.id) is False
    turn = asyncio.create_task(rig.listener.handle_request(make_text_message(), "pon algo"))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert rig.listener.is_busy(GUILD.id) is True
    assert rig.listener.is_busy(999) is False

    gate.set()
    await turn
    assert rig.listener.is_busy(GUILD.id) is False


def make_text_message():
    return SimpleNamespace(
        content="pon algo",
        author=make_member(OTHER_USER),
        guild=GUILD,
        channel=FakeTextChannel(channel_id=51),
        mentions=[],
        reference=None,
        reply=None,
    )


class SlowTranscriber(FakeTranscriber):
    def __init__(self, clock, text="", error=None, latency_s=0.25):
        super().__init__(text, error)
        self.clock = clock
        self.latency_s = latency_s

    async def transcribe(self, wav_bytes):
        self.clock.advance(self.latency_s)
        return await super().transcribe(wav_bytes)


def debug_lines(capsys):
    return [line for line in capsys.readouterr().out.splitlines() if line.startswith("[voz-debug]")]


async def test_debug_on_prints_audio_latency_transcript_and_request(monkeypatch, capsys):
    monkeypatch.setenv("VOICE_DEBUG", "true")
    rig = Rig(None)
    rig.transcriber = rig.session._transcriber = SlowTranscriber(rig.clock, "oye makakiño skipea este tema")

    await rig.say_command()

    lines = debug_lines(capsys)
    assert any(line.startswith("[voz-debug] stt audio=") and line.endswith("s") for line in lines)
    assert (
        "[voz-debug] stt latency=250ms transcript='oye makakiño skipea este tema' request='skipea este tema'" in lines
    )


async def test_debug_on_prints_the_exception_type_and_message_on_stt_errors(monkeypatch, capsys):
    monkeypatch.setenv("VOICE_DEBUG", "on")
    rig = Rig(FakeTranscriber(error=RuntimeError("quota exceeded")))

    await rig.say_command()

    out = capsys.readouterr().out
    assert "[voz] stt error: RuntimeError" in out
    assert "[voz-debug] stt error RuntimeError: quota exceeded" in out


async def test_debug_off_prints_no_diagnostic_lines(monkeypatch, capsys):
    monkeypatch.delenv("VOICE_DEBUG", raising=False)
    rig = Rig(FakeTranscriber(error=RuntimeError("quota exceeded")))

    await rig.say_command()
    out = capsys.readouterr().out

    assert "[voz] stt error: RuntimeError" in out
    assert "[voz-debug]" not in out
    assert "quota exceeded" not in out


class RaisingStdout:
    def write(self, text):
        raise UnicodeEncodeError("charmap", text, 0, 1, "character maps to <undefined>")

    def flush(self):
        pass


def test_debug_emit_never_raises_when_stdout_fails(monkeypatch):
    monkeypatch.setenv("VOICE_DEBUG", "1")
    monkeypatch.setattr("sys.stdout", RaisingStdout())

    debug.emit("transcript='ñandú'")


async def test_stt_failure_still_notifies_when_debug_output_fails(monkeypatch):
    monkeypatch.setenv("VOICE_DEBUG", "1")
    rig = Rig(FakeTranscriber(error=RuntimeError("cuota ñ")))
    monkeypatch.setattr("voice.debug.print", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("broken pipe")), raising=False)

    await rig.say_command()

    assert rig.notifier.texts == ["Te escucho, Maxi.", STT_FAILED]
    assert rig.session.window.busy is False


@pytest.mark.parametrize("value", ["1", "true", "YES", " on "])
def test_debug_helper_accepts_truthy_values(value):
    assert debug.enabled({"VOICE_DEBUG": value}) is True


@pytest.mark.parametrize("env", [{}, {"VOICE_DEBUG": "0"}, {"VOICE_DEBUG": "nope"}])
def test_debug_helper_defaults_to_off(env):
    assert debug.enabled(env) is False


def saved_wavs(directory):
    return sorted(directory.glob("*.wav"))


async def test_debug_dir_receives_the_exact_wav_sent_to_stt(monkeypatch, tmp_path):
    target = tmp_path / "captures" / "nested"
    monkeypatch.setenv("VOICE_DEBUG", "1")
    monkeypatch.setenv("VOICE_DEBUG_DIR", str(target))
    transcriber = FakeTranscriber("hola")
    rig = Rig(transcriber)

    await rig.say_command()

    files = saved_wavs(target)
    assert len(files) == 1
    assert re.fullmatch(r"stt-\d{8}-\d{6}-\d{6}-7\.wav", files[0].name)
    assert files[0].read_bytes() == transcriber.calls[0]


def test_two_saves_in_the_same_second_do_not_overwrite_each_other(tmp_path):
    env = {"VOICE_DEBUG": "1", "VOICE_DEBUG_DIR": str(tmp_path)}
    first = datetime(2026, 1, 2, 3, 4, 5, 100, tzinfo=timezone.utc)
    second = first.replace(microsecond=200)

    debug.save_audio(b"one", 7, env=env, now=first)
    debug.save_audio(b"two", 7, env=env, now=second)

    assert sorted(path.read_bytes() for path in saved_wavs(tmp_path)) == [b"one", b"two"]


@pytest.mark.parametrize("debug_value, directory_set", [("0", True), ("", True), ("1", False)])
async def test_debug_dir_writes_nothing_unless_both_variables_are_set(monkeypatch, tmp_path, debug_value, directory_set):
    monkeypatch.setenv("VOICE_DEBUG", debug_value)
    if directory_set:
        monkeypatch.setenv("VOICE_DEBUG_DIR", str(tmp_path / "captures"))
    else:
        monkeypatch.delenv("VOICE_DEBUG_DIR", raising=False)
    monkeypatch.chdir(tmp_path)
    rig = Rig(FakeTranscriber("hola"))

    await rig.say_command()

    assert list(tmp_path.rglob("*.wav")) == []
    assert not (tmp_path / "captures").exists()


async def test_an_unwritable_debug_dir_never_breaks_transcription(monkeypatch, tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_bytes(b"file")
    monkeypatch.setenv("VOICE_DEBUG", "1")
    monkeypatch.setenv("VOICE_DEBUG_DIR", str(blocker / "inside"))
    transcriber = FakeTranscriber("oye makakiño skipea este tema")
    rig = Rig(transcriber)

    await rig.say_command()

    assert len(transcriber.calls) == 1
    assert rig.runner.calls[0][1] == "skipea este tema"
