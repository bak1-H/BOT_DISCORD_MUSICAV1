from agent.memory import ConversationMemory


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def make_memory(clock, **overrides):
    return ConversationMemory(clock=clock, **overrides)


def test_stores_text_turns_in_order():
    memory = make_memory(Clock())
    memory.add_turn((1, 10), "pon algo", "listo")
    assert memory.history((1, 10)) == [("user", "pon algo"), ("assistant", "listo")]


def test_threads_are_isolated_per_channel_and_user():
    memory = make_memory(Clock())
    memory.add_turn((1, 10), "hola", "ey")
    memory.add_turn((1, 11), "otra", "cosa")
    assert memory.history((1, 10)) == [("user", "hola"), ("assistant", "ey")]
    assert memory.history((1, 11)) == [("user", "otra"), ("assistant", "cosa")]
    assert memory.history((2, 10)) == []


def test_keeps_only_last_six_messages():
    memory = make_memory(Clock())
    for index in range(5):
        memory.add_turn((1, 10), f"u{index}", f"a{index}")
    history = memory.history((1, 10))
    assert [text for _, text in history] == ["u2", "a2", "u3", "a3", "u4", "a4"]


def test_truncates_each_message_to_500_chars():
    memory = make_memory(Clock())
    memory.add_turn((1, 10), "x" * 900, "y" * 900)
    history = memory.history((1, 10))
    assert [len(text) for _, text in history] == [500, 500]


def test_thread_expires_after_ttl():
    clock = Clock()
    memory = make_memory(clock)
    memory.add_turn((1, 10), "hola", "ey")
    clock.now = 20 * 60 + 1
    assert memory.history((1, 10)) == []


def test_thread_survives_just_before_ttl_and_activity_refreshes_it():
    clock = Clock()
    memory = make_memory(clock)
    memory.add_turn((1, 10), "hola", "ey")
    clock.now = 20 * 60 - 1
    assert memory.history((1, 10)) == [("user", "hola"), ("assistant", "ey")]
    memory.add_turn((1, 10), "otra", "vez")
    clock.now = 2 * 20 * 60 - 2
    assert len(memory.history((1, 10))) == 4


def test_least_recently_used_thread_is_evicted_past_100():
    memory = make_memory(Clock())
    for user in range(100):
        memory.add_turn((1, user), "hola", "ey")
    memory.history((1, 0))
    memory.add_turn((1, 100), "nuevo", "hilo")
    assert memory.history((1, 0)) != []
    assert memory.history((1, 1)) == []
    assert memory.history((1, 100)) != []


def test_clear_drops_a_thread():
    memory = make_memory(Clock())
    memory.add_turn((1, 10), "hola", "ey")
    memory.clear((1, 10))
    assert memory.history((1, 10)) == []
