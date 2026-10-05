import asyncio
import threading
from types import SimpleNamespace

import pytest

from music.player import GuildPlayer
from music.ports import ConnectResult
from music.service import MusicService
from tests.fakes import (
    FakeAudioSource,
    FakeExtractor,
    FakeMember,
    FakeVoiceClient,
    FakeVoiceGateway,
    RecordingNotifier,
    settle,
)

GID = 1


def build_service(tmp_path, client=None, connect_result=None):
    player = GuildPlayer(GID)
    voice = FakeVoiceGateway(client, connect_result)
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


def queue_songs(player, *video_ids):
    for video_id in video_ids:
        player.enqueue(f"https://www.youtube.com/watch?v={video_id}", f"Song {video_id}")


def channel_with(*members):
    return SimpleNamespace(members=list(members))


async def test_connect_through_gateway_remembers_text_channel(tmp_path):
    rig = build_service(tmp_path)
    text_channel = object()
    voice_channel = object()

    result = await rig.service.connect(voice_channel, text_channel)

    assert result is ConnectResult.CONNECTED
    assert rig.voice.connect_calls == [voice_channel]
    assert rig.player.text_channel is text_channel


async def test_connect_when_already_connected_skips_the_gateway_but_updates_text_channel(tmp_path):
    rig = build_service(tmp_path, client=FakeVoiceClient())
    text_channel = object()

    result = await rig.service.connect(object(), text_channel)

    assert result is ConnectResult.ALREADY_CONNECTED
    assert rig.voice.connect_calls == []
    assert rig.player.text_channel is text_channel


@pytest.mark.parametrize("failure", [ConnectResult.TIMEOUT, ConnectResult.REFUSED])
async def test_connect_failure_is_returned_and_text_channel_stays_unset(tmp_path, failure):
    rig = build_service(tmp_path, connect_result=failure)

    result = await rig.service.connect(object(), object())

    assert result is failure
    assert rig.player.text_channel is None
    assert rig.voice.client is None


async def test_ensure_playing_starts_the_first_queued_song_and_announces_it(tmp_path):
    rig = build_service(tmp_path, client=FakeVoiceClient())
    queue_songs(rig.player, "a", "b")

    await rig.service.ensure_playing()

    assert rig.player.current["title"] == "Song a"
    assert rig.voice.client.play_calls == 1
    assert rig.notifier.embed_titles == ["🎵 Reproduciendo ahora"]
    assert [track.title for track in rig.player.queue] == ["Song b"]


async def test_ensure_playing_without_connected_client_does_nothing(tmp_path):
    rig = build_service(tmp_path)
    queue_songs(rig.player, "a")

    await rig.service.ensure_playing()

    assert rig.extractor.download_calls == []
    assert len(rig.player.queue) == 1


async def test_after_callback_fired_from_another_thread_plays_the_next_song(tmp_path):
    rig = build_service(tmp_path, client=FakeVoiceClient())
    queue_songs(rig.player, "a", "b")
    await rig.service.ensure_playing()
    voice_client = rig.voice.client

    voice_thread = threading.Thread(target=voice_client.finish_song)
    voice_thread.start()
    voice_thread.join()
    await settle(50)

    assert rig.player.current["title"] == "Song b"
    assert voice_client.play_calls == 2


async def test_after_callback_uses_the_voice_client_current_at_that_moment(tmp_path):
    first_client = FakeVoiceClient()
    rig = build_service(tmp_path, client=first_client)
    queue_songs(rig.player, "a", "b")
    await rig.service.ensure_playing()
    second_client = FakeVoiceClient()

    rig.voice.client = second_client
    first_client.finish_song()
    await settle(50)

    assert first_client.play_calls == 1
    assert second_client.play_calls == 1
    assert rig.player.current["title"] == "Song b"


async def test_loop_song_reinserts_previous_track_before_the_queue(tmp_path):
    rig = build_service(tmp_path, client=FakeVoiceClient())
    rig.player.loop_mode = "song"
    rig.player.current = {"title": "Song prev", "url": "https://www.youtube.com/watch?v=prev"}
    queue_songs(rig.player, "other")

    await rig.service._play_next_locked()

    assert rig.extractor.download_calls == ["https://www.youtube.com/watch?v=prev"]
    assert [track.title for track in rig.player.queue] == ["Song other"]


async def test_empty_queue_disconnects_the_client_found_at_that_moment(tmp_path):
    rig = build_service(tmp_path, client=FakeVoiceClient())
    rig.player.current = {"title": "Song prev", "url": "u"}
    replacement = FakeVoiceClient()

    rig.voice.client = replacement
    await rig.service._play_next_locked()

    assert rig.player.current is None
    assert replacement.disconnect_calls == 1


async def test_failures_notify_once_then_clear_queue_and_disconnect(tmp_path):
    rig = build_service(tmp_path, client=FakeVoiceClient())
    queue_songs(rig.player, "a", "b", "c", "d")
    for _ in range(3):
        rig.extractor.download_outcomes.append(RuntimeError("boom"))

    await rig.service._play_next_locked()

    assert rig.player.queue == []
    assert rig.voice.client.disconnect_calls == 1
    assert len([t for t in rig.notifier.texts if "Error al reproducir" in t]) == 1
    assert rig.notifier.has_text_containing("Falló la reproducción varias veces")


async def test_search_and_enqueue_adds_normalized_track_and_reports_idle(tmp_path):
    rig = build_service(tmp_path, client=FakeVoiceClient())
    rig.extractor.search_entries = [rig.extractor.entry("abc", title="Tusa")]

    result = await rig.service.search_and_enqueue("tusa")

    assert result.track.title == "Tusa"
    assert result.track.url == "https://www.youtube.com/watch?v=abc"
    assert result.preview["title"] == "Tusa"
    assert result.busy is False
    assert [track.title for track in rig.player.queue] == ["Tusa"]
    assert rig.extractor.search_calls == [{"query": "tusa", "search_count": 1}]


async def test_search_and_enqueue_reports_busy_while_playing_or_loading(tmp_path):
    playing_client = FakeVoiceClient()
    playing_client.playing = True
    rig = build_service(tmp_path, client=playing_client)
    rig.extractor.search_entries = [rig.extractor.entry("abc")]

    playing_result = await rig.service.search_and_enqueue("x")
    playing_client.playing = False
    await rig.player.playback_lock.acquire()
    loading_result = await rig.service.search_and_enqueue("x")
    rig.player.playback_lock.release()

    assert playing_result.busy is True
    assert loading_result.busy is True


async def test_search_and_enqueue_returns_none_without_results(tmp_path):
    rig = build_service(tmp_path, client=FakeVoiceClient())
    rig.extractor.search_entries = []

    result = await rig.service.search_and_enqueue("nada")

    assert result is None
    assert rig.player.queue == []


async def test_search_and_enqueue_propagates_extractor_errors(tmp_path):
    rig = build_service(tmp_path, client=FakeVoiceClient())
    rig.extractor.search_error = RuntimeError("network down")

    with pytest.raises(RuntimeError, match="network down"):
        await rig.service.search_and_enqueue("x")


async def test_skip_stops_only_when_something_is_playing(tmp_path):
    client = FakeVoiceClient()
    rig = build_service(tmp_path, client=client)

    idle_result = rig.service.skip()
    client.playing = True
    playing_result = rig.service.skip()

    assert (idle_result, playing_result) == (False, True)
    assert client.stop_calls == 1


async def test_pause_and_resume_report_whether_they_acted(tmp_path):
    client = FakeVoiceClient()
    rig = build_service(tmp_path, client=client)

    assert rig.service.pause() is False
    assert rig.service.resume() is False
    client.playing = True
    assert rig.service.pause() is True
    assert client.paused is True
    assert rig.service.resume() is True
    assert client.playing is True


async def test_stop_resets_the_session_and_disconnects(tmp_path):
    client = FakeVoiceClient()
    client.playing = True
    rig = build_service(tmp_path, client=client)
    queue_songs(rig.player, "a")
    rig.player.current = {"title": "x", "url": "u"}
    rig.player.loop_mode = "queue"
    rig.player.radio.query = "pop"

    await rig.service.stop()

    assert rig.player.queue == []
    assert rig.player.current is None
    assert rig.player.loop_mode == "off"
    assert rig.player.radio.query is None
    assert client.stop_calls == 1
    assert client.disconnect_calls == 1


async def test_cycle_loop_walks_off_song_queue_and_back(tmp_path):
    rig = build_service(tmp_path)

    modes = [rig.service.cycle_loop() for _ in range(4)]

    assert modes == ["song", "queue", "off", "song"]
    assert rig.player.loop_mode == "song"


async def test_start_radio_sets_the_query_with_a_fresh_pool_and_stop_clears_it(tmp_path):
    rig = build_service(tmp_path)
    rig.player.radio.played.add("old")

    rig.service.start_radio("jazz")
    assert rig.player.radio.query == "jazz"
    assert rig.player.radio.played == set()

    rig.service.stop_radio()
    assert rig.player.radio.query is None


async def test_alone_timeout_disconnects_the_current_client_and_notifies(tmp_path, monkeypatch):
    from music import service as service_module

    monkeypatch.setattr(service_module, "ALONE_TIMEOUT", 0)
    rig = build_service(tmp_path)
    rig.player.text_channel = rig.notifier
    queue_songs(rig.player, "a")
    current_client = FakeVoiceClient(channel_with(FakeMember(is_bot=True)))
    rig.voice.client = current_client

    await rig.service.alone_timeout()

    assert current_client.disconnect_calls == 1
    assert rig.player.queue == []
    assert rig.player.alone_task is None
    assert rig.notifier.has_text_containing("quedé solo en el canal")


async def test_alone_timeout_keeps_the_session_when_a_human_returned(tmp_path, monkeypatch):
    from music import service as service_module

    monkeypatch.setattr(service_module, "ALONE_TIMEOUT", 0)
    client = FakeVoiceClient(channel_with(FakeMember(is_bot=True), FakeMember(is_bot=False)))
    rig = build_service(tmp_path, client=client)
    queue_songs(rig.player, "a")

    await rig.service.alone_timeout()

    assert client.disconnect_calls == 0
    assert len(rig.player.queue) == 1


async def test_refresh_alone_watch_starts_a_task_when_nobody_is_left_and_cancels_it_on_return(tmp_path):
    client = FakeVoiceClient(channel_with(FakeMember(is_bot=True)))
    rig = build_service(tmp_path, client=client)

    rig.service.refresh_alone_watch()
    started = rig.player.alone_task
    assert started is not None and not started.done()

    client.channel.members.append(FakeMember(is_bot=False))
    rig.service.refresh_alone_watch()
    await settle()

    assert rig.player.alone_task is None
    assert started.cancelled()


async def test_refresh_alone_watch_ignores_a_guild_without_a_connected_client(tmp_path):
    rig = build_service(tmp_path)

    rig.service.refresh_alone_watch()

    assert rig.player.alone_task is None
