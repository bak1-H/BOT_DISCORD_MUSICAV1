import asyncio
import traceback
from dataclasses import dataclass
from typing import Protocol

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from agent.context import PendingAction, RunContext
from agent.errors import ErrorKind, classify_error, user_message
from agent.graph import build_graph, graph_config
from agent.memory import ConversationMemory
from agent.prompt import build_system_prompt, build_turn_prompt
from agent.tools import ToolRegistry

TURN_TIMEOUT_S = 75
MAX_REPLY_CHARS = 1900
DEFAULT_REPLY = "Listo."
ROLE_MESSAGES = {"user": HumanMessage, "assistant": AIMessage}


@dataclass(frozen=True)
class AgentReply:
    text: str
    pending: tuple = ()
    tool_calls: int = 0
    failed: bool = False


class AgentRunner(Protocol):
    async def run(self, ctx: RunContext, user_text: str) -> AgentReply: ...


def message_text(message) -> str:
    content = message.content
    if isinstance(content, str):
        return content.strip()
    parts = [block.get("text", "") if isinstance(block, dict) else str(block) for block in content]
    return "".join(parts).strip()


def last_model_text(messages) -> str:
    for message in reversed(messages):
        if isinstance(message, AIMessage):
            return message_text(message)
    return ""


def fit_reply(body: str, tail: str) -> str:
    if not tail:
        return body[:MAX_REPLY_CHARS]
    room = MAX_REPLY_CHARS - len(tail) - 1
    head = body[: max(room, 0)].rstrip()
    return f"{head}\n{tail}" if head else tail[:MAX_REPLY_CHARS]


def executed_prefix(ctx: RunContext) -> str:
    if not ctx.ledger.executed:
        return ""
    return "Alcancé a hacer: " + " ".join(ctx.ledger.executed)


class LangGraphAgentRunner:
    def __init__(
        self,
        model,
        registry: ToolRegistry,
        memory: ConversationMemory | None = None,
        turn_timeout: float = TURN_TIMEOUT_S,
    ) -> None:
        self._graph = build_graph(model, registry)
        self._memory = memory or ConversationMemory()
        self._turn_timeout = turn_timeout
        self._system_prompt = build_system_prompt()

    def _initial_messages(self, ctx: RunContext, user_text: str) -> list:
        thread = (ctx.channel_id, ctx.author_id)
        history = [ROLE_MESSAGES[role](content=text) for role, text in self._memory.history(thread)]
        return [SystemMessage(self._system_prompt), *history, HumanMessage(build_turn_prompt(ctx, user_text))]

    async def run(self, ctx: RunContext, user_text: str) -> AgentReply:
        try:
            state = await asyncio.wait_for(
                self._graph.ainvoke({"messages": self._initial_messages(ctx, user_text)}, config=graph_config(ctx)),
                timeout=self._turn_timeout,
            )
        except Exception as error:
            traceback.print_exc()
            return self._failure_reply(ctx, classify_error(error))

        if ctx.ledger.limit_hit:
            return self._failure_reply(ctx, ErrorKind.TOOL_LIMIT, failed=False)

        text = last_model_text(state["messages"])
        self._memory.add_turn((ctx.channel_id, ctx.author_id), user_text, text or DEFAULT_REPLY)
        return self._reply(ctx, text or ("" if ctx.ledger.pending else DEFAULT_REPLY))

    def _reply(self, ctx: RunContext, body: str, failed: bool = False) -> AgentReply:
        pending: tuple[PendingAction, ...] = tuple(ctx.ledger.pending)
        tail = "\n".join(action.prompt for action in pending)
        return AgentReply(
            text=fit_reply(body, tail),
            pending=pending,
            tool_calls=ctx.ledger.tool_calls,
            failed=failed,
        )

    def _failure_reply(self, ctx: RunContext, kind: ErrorKind, failed: bool = True) -> AgentReply:
        prefix = executed_prefix(ctx)
        message = user_message(kind)
        body = f"{prefix} {message}" if prefix else message
        return self._reply(ctx, body, failed=failed)
