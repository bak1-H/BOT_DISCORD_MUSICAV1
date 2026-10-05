import asyncio

from music.player import GuildPlayer, PlayerRegistry, Track


def test_unknown_guild_gets_a_fresh_empty_player():
    player = PlayerRegistry().get(42)

    assert isinstance(player, GuildPlayer)
    assert player.guild_id == 42
    assert player.queue == []
    assert player.current is None
    assert player.loop_mode == "off"
    assert player.radio.query is None
    assert player.radio.played == set()
    assert list(player.radio.history) == []
    assert player.radio.suggestions == []
    assert player.last_video_id is None
    assert player.fail_count == 0
    assert player.audio_file is None
    assert player.text_channel is None
    assert player.alone_task is None


def test_registry_returns_the_same_player_for_the_same_guild():
    registry = PlayerRegistry()

    assert registry.get(1) is registry.get(1)
    assert registry.get(1) is not registry.get(2)


def test_guilds_are_isolated():
    registry = PlayerRegistry()
    registry.get(1).enqueue("https://youtu.be/a", "A")
    registry.get(1).loop_mode = "song"
    registry.get(1).radio.query = "salsa"

    other = registry.get(2)

    assert [track.title for track in registry.get(1).queue] == ["A"]
    assert other.queue == []
    assert other.loop_mode == "off"
    assert other.radio.query is None
    assert other.voice_lock is not registry.get(1).voice_lock
    assert other.playback_lock is not registry.get(1).playback_lock


def test_enqueue_assigns_monotonic_entry_ids_per_guild():
    registry = PlayerRegistry()
    first = registry.get(1).enqueue("u1", "One")
    second = registry.get(1).enqueue("u2", "Two")
    foreign = registry.get(2).enqueue("u3", "Three")

    assert isinstance(first, Track)
    assert (first.entry_id, second.entry_id) == (1, 2)
    assert foreign.entry_id == 1
    assert registry.get(1).queue == [first, second]


def test_enqueue_front_inserts_at_index_zero_with_a_new_id():
    player = PlayerRegistry().get(1)
    player.enqueue("u1", "One")
    front = player.enqueue_front("u0", "Zero")

    assert [(track.url, track.title) for track in player.queue] == [("u0", "Zero"), ("u1", "One")]
    assert front.entry_id == 2


def populated_player(tmp_path):
    player = PlayerRegistry().get(7)
    audio = tmp_path / "song.opus"
    audio.write_bytes(b"x")
    player.enqueue("u1", "One")
    player.current = {"title": "Now", "url": "u"}
    player.loop_mode = "queue"
    player.radio.query = "pop"
    player.radio.played.add("vid")
    player.radio.history.append("Old")
    player.radio.suggestions.append("Next")
    player.audio_file = str(audio)
    player.last_video_id = "last"
    player.fail_count = 2
    return player, audio


def test_reset_session_clears_playback_state_and_audio_file(tmp_path):
    player, audio = populated_player(tmp_path)

    player.reset_session()

    assert player.queue == []
    assert player.current is None
    assert player.loop_mode == "off"
    assert player.radio.query is None
    assert player.radio.played == set()
    assert list(player.radio.history) == []
    assert player.radio.suggestions == []
    assert player.audio_file is None
    assert not audio.exists()


def test_reset_session_keeps_locks_and_counters(tmp_path):
    player, _ = populated_player(tmp_path)
    voice_lock, playback_lock = player.voice_lock, player.playback_lock
    next_id = player.next_entry_id

    player.reset_session()

    assert player.voice_lock is voice_lock
    assert player.playback_lock is playback_lock
    assert player.last_video_id == "last"
    assert player.fail_count == 2
    assert player.next_entry_id == next_id


async def test_reset_session_does_not_break_a_held_lock(tmp_path):
    player, _ = populated_player(tmp_path)

    async with player.playback_lock:
        player.reset_session()
        assert player.playback_lock.locked()

    assert not player.playback_lock.locked()


async def test_reset_session_leaves_the_alone_task_untouched(tmp_path):
    player, _ = populated_player(tmp_path)
    player.alone_task = asyncio.create_task(asyncio.sleep(60))

    player.reset_session()

    assert not player.alone_task.done()
    player.alone_task.cancel()


def test_cleanup_audio_file_tolerates_missing_files(tmp_path):
    player = PlayerRegistry().get(1)
    player.audio_file = str(tmp_path / "gone.opus")

    player.cleanup_audio_file()

    assert player.audio_file is None


def test_radio_clear_and_reset_pool_differ_on_query():
    player = PlayerRegistry().get(1)
    player.radio.query = "rock"
    player.radio.played.add("a")

    player.radio.reset_pool()
    assert player.radio.query == "rock"
    assert player.radio.played == set()

    player.radio.played.add("b")
    player.radio.clear()
    assert player.radio.query is None
    assert player.radio.played == set()
