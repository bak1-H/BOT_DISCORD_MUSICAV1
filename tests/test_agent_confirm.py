import asyncio

from agent.confirm import (
    ALREADY_RESOLVED,
    CANCELLED,
    CONFIRMATION_TIMEOUT_S,
    EXECUTION_FAILED,
    EXPIRED,
    NOT_YOUR_CONFIRMATION,
    ConfirmView,
)
from agent.context import PendingAction
from tests.discord_support import HUMAN_ID, FakeInteraction, FakeSentMessage

CTX = object()


class RecordingExecutor:
    def __init__(self, fail_on=None, gate=None):
        self.executed = []
        self.fail_on = fail_on
        self.gate = gate

    async def __call__(self, ctx, action):
        if self.gate is not None:
            await self.gate.wait()
        if action.kind == self.fail_on:
            raise RuntimeError("secret internal detail")
        self.executed.append((ctx, action.kind))
        return f"hecho {action.kind}"


def action(kind="stop"):
    return PendingAction(kind=kind, prompt=f"¿{kind}?", payload={})


def build_view(*actions, executor=None, author_id=HUMAN_ID):
    executor = executor or RecordingExecutor()
    view = ConfirmView(actions or (action(),), CTX, author_id, execute=executor)
    view.message = FakeSentMessage("prompt", {})
    return view, executor


def final_text(interaction):
    return interaction.original_edits[-1]["content"]


async def test_view_expires_after_sixty_seconds():
    view, _ = build_view()

    assert view.timeout == CONFIRMATION_TIMEOUT_S == 60


async def test_only_the_author_passes_the_interaction_check():
    view, _ = build_view()
    stranger = FakeInteraction(user_id=123)
    author = FakeInteraction()

    assert await view.interaction_check(stranger) is False
    assert await view.interaction_check(author) is True
    assert stranger.response.sent == [(NOT_YOUR_CONFIRMATION, {"ephemeral": True})]
    assert author.response.sent == []


async def test_confirm_executes_the_action_with_the_turn_context_and_reports_the_result():
    view, executor = build_view(action("remove_entry"))
    interaction = FakeInteraction()

    await view.handle_confirm(interaction)

    assert executor.executed == [(CTX, "remove_entry")]
    assert final_text(interaction) == "hecho remove_entry"
    assert interaction.original_edits[-1]["view"] is None
    assert interaction.response.deferred == 1
    assert view.is_finished()


async def test_confirm_runs_every_pending_action_in_order():
    view, executor = build_view(action("stop"), action("clear_queue"))
    interaction = FakeInteraction()

    await view.handle_confirm(interaction)

    assert [kind for _, kind in executor.executed] == ["stop", "clear_queue"]
    assert final_text(interaction) == "hecho stop\nhecho clear_queue"


async def test_second_click_after_confirm_does_not_execute_again():
    view, executor = build_view()
    first, second = FakeInteraction(), FakeInteraction()

    await view.handle_confirm(first)
    await view.handle_confirm(second)

    assert len(executor.executed) == 1
    assert second.response.sent == [(ALREADY_RESOLVED, {"ephemeral": True})]


async def test_simultaneous_double_click_executes_exactly_once():
    gate = asyncio.Event()
    view, executor = build_view(executor=RecordingExecutor(gate=gate))
    first, second = FakeInteraction(), FakeInteraction()

    clicks = asyncio.gather(view.handle_confirm(first), view.handle_confirm(second))
    await asyncio.sleep(0)
    gate.set()
    await clicks

    assert len(executor.executed) == 1
    assert second.response.sent == [(ALREADY_RESOLVED, {"ephemeral": True})]


async def test_cancel_executes_nothing_and_edits_the_message():
    view, executor = build_view()
    interaction = FakeInteraction()

    await view.handle_cancel(interaction)

    assert executor.executed == []
    assert interaction.response.edited == [{"content": CANCELLED, "view": None}]
    assert view.is_finished()


async def test_confirm_after_cancel_does_nothing():
    view, executor = build_view()

    await view.handle_cancel(FakeInteraction())
    late = FakeInteraction()
    await view.handle_confirm(late)

    assert executor.executed == []
    assert late.response.sent == [(ALREADY_RESOLVED, {"ephemeral": True})]


async def test_cancel_after_confirm_does_nothing():
    view, executor = build_view()

    await view.handle_confirm(FakeInteraction())
    late = FakeInteraction()
    await view.handle_cancel(late)

    assert len(executor.executed) == 1
    assert late.response.edited == []
    assert late.response.sent == [(ALREADY_RESOLVED, {"ephemeral": True})]


async def test_timeout_disables_the_buttons_and_marks_the_message_expired():
    view, executor = build_view()
    message = view.message

    await view.on_timeout()

    assert executor.executed == []
    assert message.edits == [{"content": EXPIRED, "view": None}]


async def test_click_after_timeout_never_executes():
    view, executor = build_view()
    await view.on_timeout()
    late = FakeInteraction()

    await view.handle_confirm(late)

    assert executor.executed == []
    assert late.response.sent == [(ALREADY_RESOLVED, {"ephemeral": True})]


async def test_timeout_after_resolution_leaves_the_message_alone():
    view, _ = build_view()
    await view.handle_confirm(FakeInteraction())
    message = view.message

    await view.on_timeout()

    assert message.edits == []


async def test_timeout_survives_a_failing_message_edit():
    view, _ = build_view()
    view.message.edit_error = RuntimeError("message deleted")

    await view.on_timeout()

    assert view.is_finished()


async def test_timeout_without_a_known_message_does_not_crash():
    view, _ = build_view()
    view.message = None

    await view.on_timeout()


async def test_executor_failure_reports_a_generic_error_without_leaking_details():
    view, executor = build_view(action("stop"), action("clear_queue"), executor=RecordingExecutor(fail_on="stop"))
    interaction = FakeInteraction()

    await view.handle_confirm(interaction)

    text = final_text(interaction)
    assert EXECUTION_FAILED in text
    assert "secret" not in text


async def test_failed_original_edit_falls_back_to_a_followup():
    view, _ = build_view()
    interaction = FakeInteraction()
    interaction.original_edit_error = RuntimeError("edit failed")

    await view.handle_confirm(interaction)

    assert interaction.followups == [("hecho stop", {})]


async def test_failed_original_edit_and_followup_is_swallowed():
    view, _ = build_view()
    interaction = FakeInteraction()
    interaction.original_edit_error = RuntimeError("edit failed")
    interaction._followup_send = _always_fail

    await view.handle_confirm(interaction)

    assert view.is_finished()


async def test_failed_defer_executes_nothing_and_lets_the_author_click_again():
    view, executor = build_view()
    broken = FakeInteraction()
    broken.response.error = RuntimeError("interaction expired")

    await view.handle_confirm(broken)

    assert executor.executed == []
    assert not view.is_finished()

    await view.handle_confirm(FakeInteraction())

    assert len(executor.executed) == 1


async def test_failed_cancel_edit_is_swallowed():
    view, executor = build_view()
    interaction = FakeInteraction()
    interaction.response.error = RuntimeError("interaction expired")

    await view.handle_cancel(interaction)

    assert executor.executed == []
    assert view.is_finished()


async def test_buttons_are_wired_to_the_handlers():
    view, executor = build_view()
    labels = {child.label for child in view.children}
    confirm_button = next(child for child in view.children if child.label == "Confirmar")

    await confirm_button.callback(FakeInteraction())

    assert labels == {"Confirmar", "Cancelar"}
    assert len(executor.executed) == 1


async def test_confirm_label_covers_all_actions_when_there_are_several():
    view, _ = build_view(action("stop"), action("clear_queue"))

    assert "Confirmar todo" in {child.label for child in view.children}


async def _always_fail(*args, **kwargs):
    raise RuntimeError("followup failed")
