import asyncio
import time
import traceback
from contextlib import asynccontextmanager

import discord

from agent.confirm import ConfirmView
from agent.errors import ErrorKind, user_message
from agent.runner import AgentReply
from agent.trigger import extract_request

EMPTY_REQUEST = "Dime qué quieres escuchar o escribe `!ayuda` para ver qué puedo hacer."
RATE_LIMITED = "Vas muy rápido, intenta de nuevo en unos segundos."
GUILD_BUSY = "Estoy atendiendo otro pedido en este servidor, intenta en unos segundos."
USER_INTERVAL_S = 5
MAX_TRACKED_USERS = 1000
LOCK_WAIT_S = 15
TURN_TIMEOUT_S = 60
DISCORD_MESSAGE_LIMIT = 2000


class UserRateLimiter:
    def __init__(self, interval_s: float = USER_INTERVAL_S, clock=time.monotonic, max_tracked: int = MAX_TRACKED_USERS):
        self._interval_s = interval_s
        self._clock = clock
        self._max_tracked = max_tracked
        self._last_turn: dict[int, float] = {}

    @property
    def tracked(self) -> int:
        return len(self._last_turn)

    def allow(self, user_id: int) -> bool:
        now = self._clock()
        last = self._last_turn.get(user_id)
        if last is not None and now - last < self._interval_s:
            return False
        if len(self._last_turn) >= self._max_tracked:
            self._forget_stale(now)
        self._last_turn[user_id] = now
        return True

    def _forget_stale(self, now: float) -> None:
        self._last_turn = {user: at for user, at in self._last_turn.items() if now - at < self._interval_s}


@asynccontextmanager
async def typing_indicator(channel):
    manager = channel.typing()
    try:
        await manager.__aenter__()
    except Exception as error:
        print(f"Typing indicator error: {error}")
        yield
        return
    try:
        yield
    finally:
        try:
            await manager.__aexit__(None, None, None)
        except Exception as error:
            print(f"Typing indicator exit error: {error}")


async def deliver(message, text: str, view=None):
    options = {"allowed_mentions": discord.AllowedMentions.none()}
    if view is not None:
        options["view"] = view
    body = text[:DISCORD_MESSAGE_LIMIT]
    try:
        return await message.reply(body, **options)
    except Exception as error:
        print(f"Agent reply error: {error}")
    try:
        return await message.channel.send(body, **options)
    except Exception as error:
        print(f"Agent channel send error: {error}")
    return None


def failure_reply(kind: ErrorKind) -> AgentReply:
    return AgentReply(user_message(kind), failed=True)


class AgentListener:
    def __init__(
        self,
        runner,
        context_factory,
        bot,
        rate_limiter: UserRateLimiter | None = None,
        lock_wait_s: float = LOCK_WAIT_S,
        turn_timeout_s: float = TURN_TIMEOUT_S,
    ) -> None:
        self._runner = runner
        self._context_factory = context_factory
        self._bot = bot
        self._rate_limiter = rate_limiter or UserRateLimiter()
        self._lock_wait_s = lock_wait_s
        self._turn_timeout_s = turn_timeout_s
        self._guild_locks: dict[int, asyncio.Lock] = {}

    async def on_message(self, message) -> None:
        try:
            await self._handle(message)
        except Exception:
            traceback.print_exc()

    async def _handle(self, message) -> None:
        text = extract_request(message, self._bot.user)
        if text is None:
            return
        if not self._rate_limiter.allow(message.author.id):
            await deliver(message, RATE_LIMITED)
            return
        if not text:
            await deliver(message, EMPTY_REQUEST)
            return
        try:
            ctx = self._context_factory(message)
        except Exception:
            traceback.print_exc()
            await deliver(message, user_message(ErrorKind.OTHER))
            return

        async with typing_indicator(message.channel):
            reply = await self._locked_turn(message.guild.id, ctx, text)
        await self._send_reply(message, ctx, reply)

    async def _locked_turn(self, guild_id: int, ctx, text: str) -> AgentReply:
        lock = self._guild_locks.setdefault(guild_id, asyncio.Lock())
        try:
            await asyncio.wait_for(lock.acquire(), self._lock_wait_s)
        except TimeoutError:
            return AgentReply(GUILD_BUSY, failed=True)
        try:
            return await asyncio.wait_for(self._runner.run(ctx, text), self._turn_timeout_s)
        except TimeoutError:
            return failure_reply(ErrorKind.TIMEOUT)
        except Exception:
            traceback.print_exc()
            return failure_reply(ErrorKind.OTHER)
        finally:
            lock.release()

    async def _send_reply(self, message, ctx, reply: AgentReply) -> None:
        view = ConfirmView(reply.pending, ctx, message.author.id) if reply.pending else None
        sent = await deliver(message, reply.text, view)
        if view is not None and sent is not None:
            view.message = sent
