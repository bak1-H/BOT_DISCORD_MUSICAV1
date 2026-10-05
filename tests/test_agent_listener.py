import asyncio

from google.genai.errors import ClientError
from langchain_google_genai.chat_models import _handle_client_error

from agent.confirm import ConfirmView
from agent.context import PendingAction
from agent.errors import ErrorKind, user_message
from agent.listener import (
    EMPTY_REQUEST,
    GUILD_BUSY,
    RATE_LIMITED,
    AgentListener,
    UserRateLimiter,
)
from agent.runner import AgentReply, LangGraphAgentRunner
from agent.tools import build_registry
from tests.agent_support import build_rig
from tests.discord_support import BOT_ID, FakeBotUser, FakeIncomingMessage, FakeInteraction, FakeSender
from tests.fakes import ScriptedChatModel, ai_calls, ai_text, tool_call

BOT = FakeBotUser()
SECOND_USER = 8


class StubRunner:
    def __init__(self, reply=None, error=None, gate=None):
        self.reply = reply or AgentReply("respuesta")
        self.error = error
        self.gate = gate
        self.calls = []
        self.active = 0
        self.max_active = 0

    async def run(self, ctx, text):
        self.calls.append((ctx, text))
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            if self.gate is not None:
                await self.gate.wait()
            if self.error is not None:
                raise self.error
            return self.reply
        finally:
            self.active -= 1


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


def build_listener(tmp_path, runner=None, **overrides):
    rig = build_rig(tmp_path)
    runner = runner or StubRunner()
    options = {"rate_limiter": UserRateLimiter(clock=Clock())}
    options.update(overrides)
    listener = AgentListener(runner, lambda message: rig.ctx, SimpleBot(), **options)
    return listener, runner, rig


class SimpleBot:
    user = BOT


def triggered(content="makakiño pon algo", **kwargs):
    return FakeIncomingMessage(content, **kwargs)


def sent_texts(message):
    return [sent.content for sent in message.all_sent]


async def test_non_triggering_message_makes_zero_runner_calls(tmp_path):
    listener, runner, _ = build_listener(tmp_path)
    chatter = triggered("hola a todos")

    await listener.on_message(chatter)

    assert runner.calls == []
    assert chatter.all_sent == []


async def test_commands_never_reach_the_runner(tmp_path):
    listener, runner, _ = build_listener(tmp_path)

    await listener.on_message(triggered("!skip", mentions=[BOT]))

    assert runner.calls == []


async def test_triggered_message_runs_the_agent_with_clean_text_and_replies(tmp_path):
    listener, runner, rig = build_listener(tmp_path, StubRunner(AgentReply("Puse Tusa.")))
    incoming = triggered("MAKAKIÑO pon Tusa")

    await listener.on_message(incoming)

    assert runner.calls == [(rig.ctx, "pon Tusa")]
    assert sent_texts(incoming) == ["Puse Tusa."]
    assert incoming.replies[0].kwargs["allowed_mentions"].everyone is False
    assert incoming.replies[0].kwargs["allowed_mentions"].users is False
    assert "view" not in incoming.replies[0].kwargs


async def test_bare_name_gets_a_hint_without_calling_the_agent(tmp_path):
    listener, runner, _ = build_listener(tmp_path)
    incoming = triggered("makakiño")

    await listener.on_message(incoming)

    assert runner.calls == []
    assert sent_texts(incoming) == [EMPTY_REQUEST]


async def test_bare_name_does_not_consume_the_rate_limit_slot(tmp_path):
    listener, runner, _ = build_listener(tmp_path)
    bare, real = triggered("makakiño"), triggered("makakiño pon Tusa")

    await listener.on_message(bare)
    await listener.on_message(real)

    assert sent_texts(bare) == [EMPTY_REQUEST]
    assert sent_texts(real) == ["respuesta"]
    assert len(runner.calls) == 1


async def test_bare_name_is_answered_while_the_user_is_not_rate_limited(tmp_path):
    listener, runner, _ = build_listener(tmp_path)
    first, second = triggered("makakiño"), triggered("makakiño")

    await listener.on_message(first)
    await listener.on_message(second)

    assert sent_texts(first) == [EMPTY_REQUEST]
    assert sent_texts(second) == [EMPTY_REQUEST]
    assert runner.calls == []


async def test_rate_limited_user_is_warned_once_per_window_and_then_ignored(tmp_path):
    clock = Clock()
    listener, runner, _ = build_listener(tmp_path, rate_limiter=UserRateLimiter(interval_s=5, clock=clock))
    first, second, third, fourth = triggered(), triggered(), triggered(), triggered()

    await listener.on_message(first)
    await listener.on_message(second)
    await listener.on_message(third)
    await listener.on_message(fourth)

    assert sent_texts(second) == [RATE_LIMITED]
    assert sent_texts(third) == []
    assert sent_texts(fourth) == []
    assert len(runner.calls) == 1


async def test_rate_limit_warning_is_available_again_in_the_next_window(tmp_path):
    clock = Clock()
    listener, runner, _ = build_listener(tmp_path, rate_limiter=UserRateLimiter(interval_s=5, clock=clock))

    await listener.on_message(triggered())
    await listener.on_message(triggered())
    clock.now += 5
    await listener.on_message(triggered())
    warned_again = triggered()
    await listener.on_message(warned_again)

    assert sent_texts(warned_again) == [RATE_LIMITED]
    assert len(runner.calls) == 2


async def test_bare_name_from_a_rate_limited_user_gets_no_extra_reply(tmp_path):
    listener, runner, _ = build_listener(tmp_path)
    await listener.on_message(triggered())
    await listener.on_message(triggered())
    bare = triggered("makakiño")

    await listener.on_message(bare)

    assert sent_texts(bare) == []


async def test_typing_indicator_wraps_the_turn(tmp_path):
    listener, _, _ = build_listener(tmp_path)
    incoming = triggered()

    await listener.on_message(incoming)

    assert incoming.channel.typing_entered == 1
    assert incoming.channel.typing_exited == 1


async def test_typing_failure_does_not_block_the_turn(tmp_path):
    listener, runner, _ = build_listener(tmp_path)
    incoming = triggered()
    incoming.channel.typing_error = RuntimeError("no permission")

    await listener.on_message(incoming)

    assert len(runner.calls) == 1
    assert sent_texts(incoming) == ["respuesta"]


async def test_second_turn_from_the_same_user_within_the_window_is_rate_limited(tmp_path):
    listener, runner, _ = build_listener(tmp_path)
    first, second = triggered(), triggered()

    await listener.on_message(first)
    await listener.on_message(second)

    assert len(runner.calls) == 1
    assert sent_texts(second) == [RATE_LIMITED]


async def test_rate_limit_is_per_user_and_expires(tmp_path):
    clock = Clock()
    listener, runner, _ = build_listener(tmp_path, rate_limiter=UserRateLimiter(interval_s=5, clock=clock))

    await listener.on_message(triggered())
    await listener.on_message(triggered(author=FakeSender(user_id=SECOND_USER)))
    clock.now += 5
    await listener.on_message(triggered())

    assert len(runner.calls) == 3


def test_rate_limiter_forgets_old_users_when_it_grows():
    clock = Clock()
    limiter = UserRateLimiter(interval_s=5, clock=clock, max_tracked=3)
    for user in range(3):
        assert limiter.allow(user)
    clock.now += 10

    assert limiter.allow(99)
    assert limiter.tracked <= 2


async def test_turns_in_the_same_guild_are_serialized(tmp_path):
    gate = asyncio.Event()
    listener, runner, _ = build_listener(
        tmp_path, StubRunner(gate=gate), rate_limiter=UserRateLimiter(interval_s=0, clock=Clock())
    )
    first = triggered("makakiño uno")
    second = triggered("makakiño dos", author=FakeSender(user_id=SECOND_USER))

    running = asyncio.gather(listener.on_message(first), listener.on_message(second))
    await asyncio.sleep(0.01)
    assert runner.active == 1
    gate.set()
    await running

    assert runner.max_active == 1
    assert len(runner.calls) == 2


async def test_busy_guild_gives_up_waiting_and_tells_the_user(tmp_path):
    gate = asyncio.Event()
    listener, runner, _ = build_listener(
        tmp_path,
        StubRunner(gate=gate),
        lock_wait_s=0.05,
        rate_limiter=UserRateLimiter(interval_s=0, clock=Clock()),
    )
    first = triggered("makakiño uno")
    second = triggered("makakiño dos", author=FakeSender(user_id=SECOND_USER))

    running = asyncio.create_task(listener.on_message(first))
    await asyncio.sleep(0.01)
    await listener.on_message(second)
    gate.set()
    await running

    assert sent_texts(second) == [GUILD_BUSY]
    assert len(runner.calls) == 1


async def test_hanging_agent_times_out_and_releases_the_guild_lock(tmp_path):
    hanging = StubRunner(gate=asyncio.Event())
    listener, runner, _ = build_listener(
        tmp_path, hanging, turn_timeout_s=0.05, rate_limiter=UserRateLimiter(interval_s=0, clock=Clock())
    )
    stuck = triggered("makakiño uno")

    await listener.on_message(stuck)
    runner.gate = None
    follow_up = triggered("makakiño dos")
    await listener.on_message(follow_up)

    assert sent_texts(stuck) == [user_message(ErrorKind.TIMEOUT)]
    assert sent_texts(follow_up) == ["respuesta"]


async def test_unexpected_runner_exception_is_reported_and_the_next_turn_works(tmp_path):
    listener, runner, _ = build_listener(
        tmp_path, StubRunner(error=RuntimeError("boom")), rate_limiter=UserRateLimiter(interval_s=0, clock=Clock())
    )
    broken = triggered()

    await listener.on_message(broken)
    runner.error = None
    healthy = triggered()
    await listener.on_message(healthy)

    assert sent_texts(broken) == [user_message(ErrorKind.OTHER)]
    assert sent_texts(healthy) == ["respuesta"]


async def test_gemini_429_through_the_real_runner_gives_the_friendly_message_and_keeps_playback(tmp_path):
    rig = build_rig(tmp_path)
    rig.voice.client.playing = True
    quota = ClientError(429, {"error": {"message": "quota"}})
    try:
        _handle_client_error(quota, {"model": "m"})
    except Exception as wrapped:
        failure = wrapped
    model = ScriptedChatModel(failure, ai_calls(tool_call("get_queue")), ai_text("Hay una canción."))
    runner = LangGraphAgentRunner(model, build_registry())
    listener = AgentListener(
        runner, lambda message: rig.ctx, SimpleBot(), rate_limiter=UserRateLimiter(interval_s=0, clock=Clock())
    )
    limited, recovered = triggered("makakiño qué hay"), triggered("makakiño qué hay")

    await listener.on_message(limited)
    await listener.on_message(recovered)

    assert sent_texts(limited) == [user_message(ErrorKind.RATE_LIMIT)]
    assert rig.voice.client.playing is True
    assert sent_texts(recovered) == ["Hay una canción."]


async def test_context_factory_failure_is_reported_without_raising(tmp_path):
    def broken_factory(message):
        raise RuntimeError("no guild")

    listener = AgentListener(StubRunner(), broken_factory, SimpleBot(), rate_limiter=UserRateLimiter(clock=Clock()))
    incoming = triggered()

    await listener.on_message(incoming)

    assert sent_texts(incoming) == [user_message(ErrorKind.OTHER)]


async def test_pending_actions_attach_a_confirm_view_owned_by_the_author(tmp_path):
    pending = PendingAction("stop", "¿Seguro?", {})
    listener, _, rig = build_listener(tmp_path, StubRunner(AgentReply("¿Seguro?", pending=(pending,))))
    incoming = triggered("makakiño detén todo")

    await listener.on_message(incoming)

    sent = incoming.replies[0]
    view = sent.view
    assert isinstance(view, ConfirmView)
    assert view.message is sent
    assert await view.interaction_check(FakeInteraction(user_id=incoming.author.id)) is True
    assert await view.interaction_check(FakeInteraction(user_id=SECOND_USER)) is False


async def test_reply_failure_falls_back_to_a_channel_send(tmp_path):
    listener, _, _ = build_listener(tmp_path)
    incoming = triggered()
    incoming.reply_error = RuntimeError("unknown message")

    await listener.on_message(incoming)

    assert incoming.replies == []
    assert sent_texts(incoming) == ["respuesta"]


async def test_total_send_failure_is_swallowed_and_nothing_is_executed(tmp_path):
    pending = PendingAction("stop", "¿Seguro?", {})
    listener, _, _ = build_listener(tmp_path, StubRunner(AgentReply("¿Seguro?", pending=(pending,))))
    incoming = triggered()
    incoming.reply_error = RuntimeError("down")
    incoming.channel.send_error = RuntimeError("down")

    await listener.on_message(incoming)

    assert incoming.all_sent == []


async def test_long_reply_is_cut_to_the_discord_limit(tmp_path):
    listener, _, _ = build_listener(tmp_path, StubRunner(AgentReply("x" * 5000)))
    incoming = triggered()

    await listener.on_message(incoming)

    assert len(incoming.replies[0].content) == 2000


async def test_message_from_the_bot_user_is_ignored(tmp_path):
    listener, runner, _ = build_listener(tmp_path)

    await listener.on_message(triggered(author=FakeSender(user_id=BOT_ID, is_bot=True)))

    assert runner.calls == []


def test_guild_lock_wait_covers_a_full_turn_of_the_previous_request():
    from agent.listener import LOCK_WAIT_S, TURN_TIMEOUT_S

    assert LOCK_WAIT_S == 45
    assert LOCK_WAIT_S < TURN_TIMEOUT_S
