import asyncio

from google.genai.errors import ClientError, ServerError
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_google_genai.chat_models import _handle_client_error, _handle_server_error

import help_content
from agent.errors import ErrorKind, user_message
from agent.memory import ConversationMemory
from agent.runner import AgentReply, LangGraphAgentRunner
from agent.tools import build_registry, execute_pending
from tests.agent_support import build_rig, fill_queue, queue_titles
from tests.fakes import FakeExtractor, ScriptedChatModel, ai_calls, ai_text, tool_call


def make_runner(model, **overrides):
    return LangGraphAgentRunner(model, build_registry(), **overrides)


def wrapped_error(handler, original, *extra):
    try:
        handler(original, *extra)
    except Exception as wrapped:
        return wrapped
    raise AssertionError("wrapper did not raise")


def tool_messages(call):
    return [message for message in call if isinstance(message, ToolMessage)]


async def test_compound_request_runs_play_next_and_leaves_remove_pending(tmp_path):
    rig = build_rig(tmp_path)
    rig.voice.client.playing = True
    tracks = fill_queue(rig.player, *[f"s{index}" for index in range(1, 13)])
    rig.extractor.search_by_query = {"feid": [FakeExtractor.entry("f1", title="Feid song")]}
    model = ScriptedChatModel(
        ai_calls(tool_call("remove_from_queue", position=10), tool_call("play_next", query="feid")),
        ai_text("Puse algo de Feid después de esta."),
    )

    reply = await make_runner(model).run(rig.ctx, "saca la 10 y pon algo de Feid después de esta")

    assert isinstance(reply, AgentReply)
    assert queue_titles(rig.player)[0] == "Feid song"
    assert len(rig.player.queue) == 13
    assert [action.kind for action in reply.pending] == ["remove_entry"]
    assert reply.pending[0].payload["entry_id"] == tracks[9].entry_id
    assert "¿Seguro que quieres quitar «s10» (#10) de la cola?" in reply.text
    assert "Feid" in reply.text
    assert len(model.calls) == 2

    message = await execute_pending(rig.ctx, reply.pending[0])

    assert "s10" in message
    assert "s10" not in queue_titles(rig.player)
    assert queue_titles(rig.player)[0] == "Feid song"
    assert len(rig.player.queue) == 12


async def test_tool_results_are_fed_back_to_the_model_as_tool_messages(tmp_path):
    rig = build_rig(tmp_path)
    fill_queue(rig.player, "a")
    model = ScriptedChatModel(ai_calls(tool_call("get_queue")), ai_text("Hay una canción."))

    await make_runner(model).run(rig.ctx, "qué hay en la cola")

    returned = tool_messages(model.calls[1])
    assert len(returned) == 1
    assert "1. a" in returned[0].content
    assert returned[0].name == "get_queue"


async def test_capability_question_is_answered_from_prompt_with_zero_tool_calls(tmp_path):
    rig = build_rig(tmp_path)
    model = ScriptedChatModel(ai_text("Puedo reproducir, hacer cola y más."))

    reply = await make_runner(model).run(rig.ctx, "¿qué puedes hacer?")

    assert rig.ctx.ledger.tool_calls == 0
    assert len(model.calls) == 1
    system = model.calls[0][0]
    assert isinstance(system, SystemMessage)
    for title, _ in help_content.HELP_SECTIONS:
        assert title in system.content
    assert reply.text == "Puedo reproducir, hacer cola y más."


async def test_seventh_tool_call_is_not_executed_and_turn_ends_with_partial_result(tmp_path):
    rig = build_rig(tmp_path)
    model = ScriptedChatModel(repeat=ai_calls(tool_call("set_loop", mode="song")))

    reply = await make_runner(model).run(rig.ctx, "haz cosas")

    assert rig.ctx.ledger.tool_calls == 6
    assert rig.ctx.ledger.limit_hit
    assert len(model.calls) == 7
    assert user_message(ErrorKind.TOOL_LIMIT) in reply.text
    assert "Alcancé a hacer:" in reply.text
    assert reply.failed is False


async def test_invalid_arguments_return_an_error_to_the_model_with_no_effect(tmp_path):
    rig = build_rig(tmp_path)
    fill_queue(rig.player, "a", "b")
    model = ScriptedChatModel(
        ai_calls(tool_call("remove_from_queue", position="la décima")),
        ai_text("No pude hacerlo."),
    )

    reply = await make_runner(model).run(rig.ctx, "saca la décima")

    assert queue_titles(rig.player) == ["a", "b"]
    assert tool_messages(model.calls[1])[0].content.startswith("Argumentos inválidos")
    assert reply.pending == ()
    assert reply.text == "No pude hacerlo."


async def test_model_supplied_guild_id_is_ignored_in_the_full_turn(tmp_path):
    rig = build_rig(tmp_path)
    rig.voice.client.playing = True
    model = ScriptedChatModel(ai_calls(tool_call("skip", guild_id=999)), ai_text("Saltada."))

    await make_runner(model).run(rig.ctx, "salta")

    assert rig.voice.client.stop_calls == 1


async def test_tool_exception_is_reported_to_the_model_and_the_turn_completes(tmp_path):
    rig = build_rig(tmp_path)
    rig.voice.client.playing = True

    def exploding_skip():
        raise RuntimeError("voz caída")

    rig.service.skip = exploding_skip
    model = ScriptedChatModel(ai_calls(tool_call("skip")), ai_text("No pude saltarla."))

    reply = await make_runner(model).run(rig.ctx, "salta")

    assert "La herramienta falló" in tool_messages(model.calls[1])[0].content
    assert reply.text == "No pude saltarla."


async def test_rate_limit_from_real_wrapper_gives_friendly_message_and_next_turn_works(tmp_path):
    rig = build_rig(tmp_path)
    rate_limited = wrapped_error(
        _handle_client_error, ClientError(429, {"error": {"message": "quota"}}), {"model": "m"}
    )
    model = ScriptedChatModel(rate_limited, ai_text("Ya estoy de vuelta."))
    runner = make_runner(model)

    failed = await runner.run(rig.ctx, "hola")
    recovered = await runner.run(build_rig(tmp_path).ctx, "hola otra vez")

    assert failed.failed
    assert failed.text == user_message(ErrorKind.RATE_LIMIT)
    assert "!play" in failed.text
    assert recovered.text == "Ya estoy de vuelta."
    assert recovered.failed is False


async def test_server_error_is_reported_as_unavailable(tmp_path):
    rig = build_rig(tmp_path)
    server_down = wrapped_error(_handle_server_error, ServerError(503, {"error": {"message": "down"}}))

    reply = await make_runner(ScriptedChatModel(server_down)).run(rig.ctx, "hola")

    assert reply.text == user_message(ErrorKind.UNAVAILABLE)


async def test_unknown_exception_is_reported_as_other(tmp_path):
    rig = build_rig(tmp_path)

    reply = await make_runner(ScriptedChatModel(ValueError("boom"))).run(rig.ctx, "hola")

    assert reply.text == user_message(ErrorKind.OTHER)
    assert reply.failed


async def test_hanging_model_hits_the_turn_timeout(tmp_path):
    rig = build_rig(tmp_path)

    class HangingModel:
        async def ainvoke(self, messages, config=None, **kwargs):
            await asyncio.sleep(30)

    reply = await make_runner(HangingModel(), turn_timeout=0.05).run(rig.ctx, "hola")

    assert reply.text == user_message(ErrorKind.TIMEOUT)
    assert reply.failed


async def test_failure_after_tools_ran_reports_what_was_done(tmp_path):
    rig = build_rig(tmp_path)
    model = ScriptedChatModel(
        ai_calls(tool_call("set_loop", mode="song")),
        ClientError(429, {"error": {"message": "quota"}}),
    )

    reply = await make_runner(model).run(rig.ctx, "pon loop")

    assert rig.player.loop_mode == "song"
    assert reply.text.startswith("Alcancé a hacer: Loop en modo song.")
    assert user_message(ErrorKind.RATE_LIMIT) in reply.text


async def test_pending_without_model_text_still_asks_for_confirmation(tmp_path):
    rig = build_rig(tmp_path)
    fill_queue(rig.player, "a", "b")
    model = ScriptedChatModel(ai_calls(tool_call("clear_queue")), ai_text(""))

    reply = await make_runner(model).run(rig.ctx, "vacía la cola")

    assert reply.text == "¿Seguro que quieres vaciar la cola (2 canciones)?"
    assert len(rig.player.queue) == 2


async def test_empty_model_answer_without_pending_gets_a_default_text(tmp_path):
    rig = build_rig(tmp_path)

    reply = await make_runner(ScriptedChatModel(ai_text(""))).run(rig.ctx, "hola")

    assert reply.text == "Listo."


async def test_block_style_model_content_is_flattened_to_text(tmp_path):
    rig = build_rig(tmp_path)
    blocks = AIMessage(content=[{"type": "text", "text": "Hola"}, {"type": "text", "text": " mundo"}])

    reply = await make_runner(ScriptedChatModel(blocks)).run(rig.ctx, "hola")

    assert reply.text == "Hola mundo"


async def test_reply_is_clipped_below_the_discord_limit(tmp_path):
    rig = build_rig(tmp_path)
    fill_queue(rig.player, "a")
    model = ScriptedChatModel(ai_calls(tool_call("clear_queue")), ai_text("x" * 5000))

    reply = await make_runner(model).run(rig.ctx, "vacía la cola")

    assert len(reply.text) <= 1900
    assert reply.text.endswith("¿Seguro que quieres vaciar la cola (1 canciones)?")


async def test_memory_keeps_text_only_and_is_isolated_per_user(tmp_path):
    memory = ConversationMemory()
    rig = build_rig(tmp_path)
    fill_queue(rig.player, "a")
    model = ScriptedChatModel(
        ai_calls(tool_call("get_queue")),
        ai_text("Hay una."),
        ai_text("Sí, una."),
    )
    runner = make_runner(model, memory=memory)

    await runner.run(rig.ctx, "qué hay en la cola")
    await runner.run(rig.ctx, "¿seguro?")

    third_call = model.calls[2]
    assert not tool_messages(third_call)
    humans = [message.content for message in third_call if isinstance(message, HumanMessage)]
    assert "qué hay en la cola" in humans[0]
    assert "Hay una." in [message.content for message in third_call if isinstance(message, AIMessage)]
    assert memory.history((rig.ctx.channel_id, rig.ctx.author_id + 1)) == []


async def test_other_users_text_never_reaches_this_users_model_context(tmp_path):
    memory = ConversationMemory()
    first = build_rig(tmp_path)
    second = build_rig(tmp_path)
    second.ctx.author_id = first.ctx.author_id + 1
    model = ScriptedChatModel(ai_text("uno"), ai_text("dos"))
    runner = make_runner(model, memory=memory)

    await runner.run(first.ctx, "secreto del primero")
    await runner.run(second.ctx, "hola")

    flattened = " ".join(str(message.content) for message in model.calls[1])
    assert "secreto del primero" not in flattened


async def test_failed_turns_are_not_remembered(tmp_path):
    memory = ConversationMemory()
    rig = build_rig(tmp_path)
    runner = make_runner(ScriptedChatModel(ValueError("boom")), memory=memory)

    await runner.run(rig.ctx, "hola")

    assert memory.history((rig.ctx.channel_id, rig.ctx.author_id)) == []


async def test_graph_compiles_once_and_runs_concurrent_turns_independently(tmp_path):
    first = build_rig(tmp_path)
    second = build_rig(tmp_path)
    model = ScriptedChatModel(ai_text("a"), ai_text("b"))
    runner = make_runner(model)

    replies = await asyncio.gather(runner.run(first.ctx, "uno"), runner.run(second.ctx, "dos"))

    assert sorted(reply.text for reply in replies) == ["a", "b"]
    assert first.ctx.ledger is not second.ctx.ledger
