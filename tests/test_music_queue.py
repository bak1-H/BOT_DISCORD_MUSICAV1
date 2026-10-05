import asyncio
from types import SimpleNamespace

import pytest

from music.player import GuildPlayer
from music.service import MAX_QUEUE_SONGS_PER_CALL, MusicService, RemoveStatus
from tests.fakes import (
    FakeAudioSource,
    FakeExtractor,
    FakeVoiceClient,
    FakeVoiceGateway,
    RecordingNotifier,
)

GID = 1


def build_service(tmp_path, client=None):
    player = GuildPlayer(GID)
    voice = FakeVoiceGateway(client)
    notifier = RecordingNotifier()
    extractor = FakeExtractor(str(tmp_path))
    service = MusicService(
        player=player,
        voice=voice,
        notifier=notifier,
        extractor=extractor,
        audio_source_factory=lambda path: FakeAudioSource(path, options="-vn"),
        loop=asyncio.get_running_loop(),
    )
    return SimpleNamespace(service=service, player=player, voice=voice, notifier=notifier, extractor=extractor)


def playing_client():
    client = FakeVoiceClient()
    client.playing = True
    return client


def fill_queue(player, *names):
    return [player.enqueue(f"https://www.youtube.com/watch?v={name}", name) for name in names]


def queue_titles(player):
    return [track.title for track in player.queue]


async def test_remove_takes_the_one_based_position_and_returns_the_removed_track(tmp_path):
    rig = build_service(tmp_path)
    fill_queue(rig.player, "a", "b", "c")

    result = rig.service.remove(2)

    assert result.status is RemoveStatus.REMOVED
    assert result.track.title == "b"
    assert queue_titles(rig.player) == ["a", "c"]


@pytest.mark.parametrize("position", [0, -1, 4, 10])
async def test_remove_with_invalid_position_changes_nothing(tmp_path, position):
    rig = build_service(tmp_path)
    fill_queue(rig.player, "a", "b", "c")

    result = rig.service.remove(position)

    assert result.status is RemoveStatus.OUT_OF_RANGE
    assert result.track is None
    assert queue_titles(rig.player) == ["a", "b", "c"]


async def test_remove_on_empty_queue_is_out_of_range(tmp_path):
    rig = build_service(tmp_path)

    result = rig.service.remove(1)

    assert result.status is RemoveStatus.OUT_OF_RANGE


async def test_remove_with_matching_entry_id_and_title_removes(tmp_path):
    rig = build_service(tmp_path)
    tracks = fill_queue(rig.player, "a", "b", "c")

    result = rig.service.remove(3, expected_entry_id=tracks[2].entry_id, expected_title="c")

    assert result.status is RemoveStatus.REMOVED
    assert result.track is tracks[2]
    assert queue_titles(rig.player) == ["a", "b"]


async def test_remove_with_stale_entry_id_reports_changed_and_keeps_queue(tmp_path):
    rig = build_service(tmp_path)
    tracks = fill_queue(rig.player, "a", "b", "c")
    rig.player.queue.pop(0)

    result = rig.service.remove(2, expected_entry_id=tracks[0].entry_id)
    assert result.status is RemoveStatus.CHANGED
    assert result.track is tracks[2]
    assert queue_titles(rig.player) == ["b", "c"]


async def test_remove_with_title_mismatch_reports_changed_and_keeps_queue(tmp_path):
    rig = build_service(tmp_path)
    fill_queue(rig.player, "a", "b")

    result = rig.service.remove(1, expected_title="b")

    assert result.status is RemoveStatus.CHANGED
    assert queue_titles(rig.player) == ["a", "b"]


async def test_entry_id_stays_valid_after_front_insert_shifts_positions(tmp_path):
    rig = build_service(tmp_path, client=playing_client())
    tracks = fill_queue(rig.player, "a", "b", "c")
    rig.extractor.search_entries = [rig.extractor.entry("feid", title="Feid")]
    await rig.service.enqueue_next("feid")

    stale = rig.service.remove(3, expected_entry_id=tracks[2].entry_id)
    fresh = rig.service.remove(4, expected_entry_id=tracks[2].entry_id)

    assert stale.status is RemoveStatus.CHANGED
    assert fresh.status is RemoveStatus.REMOVED
    assert fresh.track.title == "c"


async def test_enqueue_next_inserts_at_index_zero_while_playing(tmp_path):
    client = playing_client()
    rig = build_service(tmp_path, client=client)
    rig.player.current = {"title": "X"}
    fill_queue(rig.player, "a")
    rig.extractor.search_entries = [rig.extractor.entry("feid", title="Feid")]

    result = await rig.service.enqueue_next("Feid")

    assert queue_titles(rig.player) == ["Feid", "a"]
    assert result.track.title == "Feid"
    assert result.busy is True
    assert client.play_calls == 0


async def test_enqueue_next_starts_playback_when_idle_and_connected(tmp_path):
    client = FakeVoiceClient()
    rig = build_service(tmp_path, client=client)
    rig.extractor.search_entries = [rig.extractor.entry("feid", title="Feid")]

    result = await rig.service.enqueue_next("Feid")

    assert result.busy is False
    assert client.play_calls == 1
    assert rig.player.current["url"] == "https://www.youtube.com/watch?v=feid"
    assert rig.player.queue == []


async def test_enqueue_next_without_results_returns_none(tmp_path):
    rig = build_service(tmp_path, client=playing_client())
    fill_queue(rig.player, "a")

    result = await rig.service.enqueue_next("nada")

    assert result is None
    assert queue_titles(rig.player) == ["a"]


async def test_clear_queue_empties_queue_and_keeps_current_song_playing(tmp_path):
    client = playing_client()
    rig = build_service(tmp_path, client=client)
    rig.player.current = {"title": "X"}
    fill_queue(rig.player, "a", "b")

    removed = rig.service.clear_queue()

    assert removed == 2
    assert rig.player.queue == []
    assert rig.player.current == {"title": "X"}
    assert client.playing is True
    assert client.stop_calls == 0


async def test_clear_queue_on_empty_queue_returns_zero(tmp_path):
    rig = build_service(tmp_path)

    assert rig.service.clear_queue() == 0


async def test_queue_songs_enqueues_each_query_in_order(tmp_path):
    rig = build_service(tmp_path, client=playing_client())
    rig.extractor.search_by_query = {
        "uno": [rig.extractor.entry("u1", title="Uno")],
        "dos": [rig.extractor.entry("d2", title="Dos")],
    }

    result = await rig.service.queue_songs(["uno", "dos"])

    assert [track.title for track in result.queued] == ["Uno", "Dos"]
    assert queue_titles(rig.player) == ["Uno", "Dos"]
    assert result.skipped_over_limit == 0
    assert result.not_found == []
    assert result.too_long == []


async def test_queue_songs_caps_at_fifteen_and_reports_the_excess(tmp_path):
    rig = build_service(tmp_path, client=playing_client())
    queries = [f"q{i}" for i in range(20)]
    rig.extractor.search_by_query = {q: [rig.extractor.entry(f"id{i}", title=f"T{i}")] for i, q in enumerate(queries)}

    result = await rig.service.queue_songs(queries)

    assert MAX_QUEUE_SONGS_PER_CALL == 15
    assert len(result.queued) == 15
    assert len(rig.player.queue) == 15
    assert result.skipped_over_limit == 5
    assert queue_titles(rig.player)[-1] == "T14"
    assert len(rig.extractor.search_calls) == 15


async def test_queue_songs_reports_not_found_and_too_long_without_queueing_them(tmp_path):
    rig = build_service(tmp_path, client=playing_client())
    rig.extractor.search_by_query = {
        "ok": [rig.extractor.entry("o1", title="Ok", duration=100)],
        "largo": [rig.extractor.entry("l1", title="Largo", duration=900)],
        "nada": [],
    }

    result = await rig.service.queue_songs(["ok", "largo", "nada"], max_duration_s=300)

    assert [track.title for track in result.queued] == ["Ok"]
    assert result.too_long == ["Largo"]
    assert result.not_found == ["nada"]
    assert queue_titles(rig.player) == ["Ok"]


async def test_queue_songs_starts_playback_when_idle_and_connected(tmp_path):
    client = FakeVoiceClient()
    rig = build_service(tmp_path, client=client)
    rig.extractor.search_by_query = {"uno": [rig.extractor.entry("u1", title="Uno")]}

    await rig.service.queue_songs(["uno"])

    assert client.play_calls == 1
    assert rig.player.current["url"] == "https://www.youtube.com/watch?v=u1"


async def test_queue_songs_with_empty_list_queues_nothing(tmp_path):
    rig = build_service(tmp_path, client=playing_client())

    result = await rig.service.queue_songs([])

    assert result.queued == []
    assert rig.extractor.search_calls == []


async def test_remove_entry_finds_the_track_after_a_front_insert_shifted_it(tmp_path):
    rig = build_service(tmp_path)
    tracks = fill_queue(rig.player, "a", "b", "c")
    rig.player.enqueue_front("https://youtu.be/feid", "feid")

    result = rig.service.remove_entry(tracks[2].entry_id)

    assert result.status is RemoveStatus.REMOVED
    assert result.track is tracks[2]
    assert queue_titles(rig.player) == ["feid", "a", "b"]


async def test_remove_entry_reports_gone_when_the_track_already_left_the_queue(tmp_path):
    rig = build_service(tmp_path)
    tracks = fill_queue(rig.player, "a", "b")
    rig.player.queue.pop(0)

    result = rig.service.remove_entry(tracks[0].entry_id)

    assert result.status is RemoveStatus.GONE
    assert result.track is None
    assert queue_titles(rig.player) == ["b"]
