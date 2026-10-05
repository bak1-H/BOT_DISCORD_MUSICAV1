import time
from collections import OrderedDict

MAX_MESSAGES_PER_THREAD = 6
MAX_CHARS_PER_MESSAGE = 500
TTL_SECONDS = 20 * 60
MAX_THREADS = 100


class ConversationMemory:
    def __init__(
        self,
        clock=time.monotonic,
        max_messages: int = MAX_MESSAGES_PER_THREAD,
        max_chars: int = MAX_CHARS_PER_MESSAGE,
        ttl_seconds: float = TTL_SECONDS,
        max_threads: int = MAX_THREADS,
    ) -> None:
        self._clock = clock
        self._max_messages = max_messages
        self._max_chars = max_chars
        self._ttl = ttl_seconds
        self._max_threads = max_threads
        self._threads: OrderedDict = OrderedDict()

    def _live_thread(self, thread_id):
        entry = self._threads.get(thread_id)
        if entry is None:
            return None
        if self._clock() - entry["touched"] > self._ttl:
            del self._threads[thread_id]
            return None
        return entry

    def history(self, thread_id) -> list:
        entry = self._live_thread(thread_id)
        if entry is None:
            return []
        self._threads.move_to_end(thread_id)
        return list(entry["messages"])

    def add_turn(self, thread_id, user_text: str, reply_text: str) -> None:
        entry = self._live_thread(thread_id)
        if entry is None:
            entry = {"messages": [], "touched": self._clock()}
            self._threads[thread_id] = entry
        entry["messages"].append(("user", user_text[: self._max_chars]))
        entry["messages"].append(("assistant", reply_text[: self._max_chars]))
        entry["messages"] = entry["messages"][-self._max_messages :]
        entry["touched"] = self._clock()
        self._threads.move_to_end(thread_id)
        while len(self._threads) > self._max_threads:
            self._threads.popitem(last=False)

    def clear(self, thread_id) -> None:
        self._threads.pop(thread_id, None)
