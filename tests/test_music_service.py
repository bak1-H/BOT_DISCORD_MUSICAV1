import asyncio
import threading
from types import SimpleNamespace

import pytest

from music.player import GuildPlayer
from music.ports import ConnectResult
from music.service import DEFAULT_DOWNLOAD_TIMEOUT_S, MusicService, download_timeout_from_env
from tests.fakes import (
    FakeAudioSource,
    FakeExtractor,
    FakeMember,
    FakeRecvClient,
    FakeSleeper,
    FakeVoiceClient,
    FakeVoiceGateway,
    RecordingNotifier,
    settle,
)

GID = 1


def build_service(tmp_path, client=None, connect_result=None, **options):
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
        **options,
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
    assert len([t for t in rig.notifier.texts if "No pude descargar" in t]) == 1
    assert rig.notifier.has_text_containing("Falló la reproducción varias veces")


async def test_a_failed_download_names_the_song_without_leaking_the_error_text(tmp_path):
    rig = build_service(tmp_path, client=FakeVoiceClient())
    queue_songs(rig.player, "a", "b")
    rig.extractor.download_outcomes.append(RuntimeError("Requested format is not available https://secret.example/v"))

    await rig.service._play_next_locked()

    assert [t for t in rig.notifier.texts if "No pude descargar" in t] == ["❌ No pude descargar «Song a»."]
    assert not rig.notifier.has_text_containing("secret.example")
    assert rig.player.current["title"] == "Song b"
    assert rig.voice.client.play_calls == 1


async def test_a_hung_download_times_out_releases_the_lock_and_plays_the_next_song(tmp_path, monkeypatch):
    monkeypatch.setenv("MUSIC_DOWNLOAD_TIMEOUT_S", "0.05")
    rig = build_service(tmp_path, client=FakeVoiceClient())
    queue_songs(rig.player, "a", "b")
    leftover = tmp_path / f"{GID}_a.webm.part"
    leftover.write_bytes(b"partial")
    other_song = tmp_path / f"{GID}_ab.webm"
    other_song.write_bytes(b"other")
    other_guild = tmp_path / "2_a.webm"
    other_guild.write_bytes(b"other")
    original = rig.extractor.download
    hung = []

    async def hang_once(guild_id, url):
        if not hung:
            hung.append(url)
            await asyncio.Event().wait()
        return await original(guild_id, url)

    rig.extractor.download = hang_once

    await asyncio.wait_for(rig.service.ensure_playing(), 3)

    assert hung == ["https://www.youtube.com/watch?v=a"]
    assert rig.notifier.has_text_containing("No pude descargar «Song a»")
    assert not rig.player.playback_lock.locked()
    assert rig.player.current["title"] == "Song b"
    assert rig.player.audio_file == str(tmp_path / f"{GID}_b.webm")
    assert rig.player.fail_count == 0
    assert not leftover.exists()
    assert other_song.exists()
    assert other_guild.exists()
    assert rig.extractor.discard_calls == ["https://www.youtube.com/watch?v=a"]


async def test_a_failed_non_timeout_download_does_not_discard_files(tmp_path):
    rig = build_service(tmp_path, client=FakeVoiceClient())
    queue_songs(rig.player, "a", "b")
    rig.extractor.download_outcomes.append(RuntimeError("boom"))

    await rig.service._play_next_locked()

    assert rig.extractor.discard_calls == []


async def test_a_player_error_is_reported_as_playback_not_download(tmp_path):
    rig = build_service(tmp_path, client=FakeVoiceClient())
    queue_songs(rig.player, "a", "b")
    calls = []

    def failing_once(path):
        calls.append(path)
        if len(calls) == 1:
            raise RuntimeError("ffmpeg secret.example exploded")
        return FakeAudioSource(path, options="-vn")

    rig.service.audio_source_factory = failing_once

    await rig.service._play_next_locked()

    assert rig.notifier.texts.count("❌ No pude reproducir «Song a».") == 1
    assert not rig.notifier.has_text_containing("No pude descargar")
    assert not rig.notifier.has_text_containing("secret.example")
    assert rig.player.current["title"] == "Song b"


async def test_an_already_playing_collision_requeues_the_track_without_a_notice(tmp_path):
    client = FakeVoiceClient()
    client.playing = True
    rig = build_service(tmp_path, client=client)
    queue_songs(rig.player, "a")

    await rig.service.play_next()

    assert [track.title for track in rig.player.queue] == ["Song a"]
    assert rig.player.current is None
    assert rig.player.audio_file is None
    assert list(tmp_path.rglob("*.webm")) == []
    assert rig.player.fail_count == 0
    assert rig.notifier.messages == []

    client.playing = False
    await rig.service.ensure_playing()

    assert client.play_calls == 1
    assert rig.player.current["title"] == "Song a"
    assert rig.player.queue == []


async def test_an_already_playing_collision_in_loop_song_mode_does_not_double_queue(tmp_path):
    client = FakeVoiceClient()
    client.playing = True
    rig = build_service(tmp_path, client=client)
    rig.player.loop_mode = "song"
    queue_songs(rig.player, "a")

    await rig.service.play_next()
    await rig.service.play_next()

    assert [track.title for track in rig.player.queue] == ["Song a"]


async def test_other_play_errors_still_count_a_failure_and_notify(tmp_path):
    rig = build_service(tmp_path, client=FakeVoiceClient())
    queue_songs(rig.player, "a")
    rig.service.audio_source_factory = lambda path: (_ for _ in ()).throw(RuntimeError("ffmpeg exploded"))

    await rig.service._play_next_locked()

    assert rig.player.fail_count == 1
    assert rig.notifier.texts.count("❌ No pude reproducir «Song a».") == 1


async def test_a_youtube_login_block_sends_only_the_blocked_notice(tmp_path):
    rig = build_service(tmp_path, client=FakeVoiceClient())
    queue_songs(rig.player, "a", "b")
    rig.extractor.download_outcomes.append(RuntimeError("Sign in to confirm you're not a bot"))

    await rig.service._play_next_locked()

    assert not rig.notifier.has_text_containing("No pude")
    assert rig.notifier.has_text_containing("bloqueado por YouTube")
    assert rig.player.current["title"] == "Song b"


@pytest.mark.parametrize("raw", [None, "", "abc", "0", "-5", "nan", "inf"])
def test_download_timeout_falls_back_to_the_default_when_invalid(raw):
    env = {} if raw is None else {"MUSIC_DOWNLOAD_TIMEOUT_S": raw}

    assert download_timeout_from_env(env) == DEFAULT_DOWNLOAD_TIMEOUT_S == 180.0


def test_download_timeout_reads_a_positive_value_from_the_environment():
    assert download_timeout_from_env({"MUSIC_DOWNLOAD_TIMEOUT_S": " 45.5 "}) == 45.5


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


async def test_skip_on_a_listening_client_stops_playback_only(tmp_path):
    client = FakeRecvClient()
    client.playing = True
    rig = build_service(tmp_path, client=client)

    assert rig.service.skip() is True

    assert client.stop_playing_calls == 1
    assert client.stop_calls == 0
    assert client.listening is True


async def test_stop_on_a_listening_client_stops_playback_before_disconnecting(tmp_path):
    client = FakeRecvClient()
    client.playing = True
    rig = build_service(tmp_path, client=client)

    await rig.service.stop()

    assert client.stop_playing_calls == 1
    assert client.stop_calls == 0
    assert client.listening is True
    assert client.disconnect_calls == 1


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


IDLE_SECONDS = 900


def build_kept_alive(tmp_path, client=None, enabled=True):
    sleeper = FakeSleeper()
    rig = build_service(
        tmp_path,
        client=client or FakeVoiceClient(),
        keep_alive=lambda: enabled,
        idle_timeout_s=IDLE_SECONDS,
        sleep=sleeper.sleep,
    )
    rig.sleeper = sleeper
    rig.player.text_channel = rig.notifier
    return rig


async def finish_queue(rig):
    await rig.service._play_next_locked()
    await settle()


async def test_keep_alive_on_leaves_the_bot_connected_when_the_queue_ends(tmp_path):
    rig = build_kept_alive(tmp_path)

    await finish_queue(rig)

    assert rig.voice.client.disconnect_calls == 0
    assert rig.voice.client.connected
    assert rig.player.current is None


async def test_keep_alive_off_disconnects_when_the_queue_ends_and_arms_no_timer(tmp_path):
    rig = build_kept_alive(tmp_path, enabled=False)

    await finish_queue(rig)

    assert rig.voice.client.disconnect_calls == 1
    assert rig.sleeper.waiters == []


async def test_idle_timeout_fires_at_the_configured_time_and_not_before(tmp_path):
    rig = build_kept_alive(tmp_path)
    client = rig.voice.client
    queue_songs(rig.player, "a")
    await finish_queue(rig)
    await rig.service.ensure_playing()
    client.finish_song()
    await settle(50)

    await rig.sleeper.advance(IDLE_SECONDS - 1)
    assert client.disconnect_calls == 0

    await rig.sleeper.advance(1)
    assert client.disconnect_calls == 1
    assert rig.notifier.has_text_containing("inactividad")
    assert rig.player.queue == []


async def test_idle_timer_is_cancelled_when_music_starts_again(tmp_path):
    rig = build_kept_alive(tmp_path)
    client = rig.voice.client
    await finish_queue(rig)
    armed = rig.service._idle_task
    queue_songs(rig.player, "a")

    await rig.service.ensure_playing()
    await rig.sleeper.advance(IDLE_SECONDS * 2)

    assert armed.cancelled()
    assert client.disconnect_calls == 0
    assert client.playing


async def test_idle_timer_does_not_cut_a_song_that_started_during_the_wait(tmp_path):
    rig = build_kept_alive(tmp_path)
    client = rig.voice.client
    await finish_queue(rig)
    client.playing = True

    await rig.sleeper.advance(IDLE_SECONDS)

    assert client.disconnect_calls == 0


async def test_stop_cancels_the_idle_timer_and_a_reconnect_arms_a_single_new_one(tmp_path):
    rig = build_kept_alive(tmp_path)
    rig.voice.client.connected = True
    await finish_queue(rig)
    first = rig.service._idle_task

    await rig.service.stop()
    await settle()
    assert first.cancelled()

    await rig.service.connect(object(), rig.notifier)
    second = rig.service._idle_task
    new_client = rig.voice.client
    await settle()
    await rig.sleeper.advance(IDLE_SECONDS)

    assert second is not first
    assert new_client.disconnect_calls == 1
    assert rig.voice.client is new_client


class SongEndingClient(FakeVoiceClient):
    def stop(self):
        was_playing = self.playing
        super().stop()
        after, self.after = self.after, None
        if was_playing and after:
            after(None)

    async def disconnect(self, force=False):
        for _ in range(3):
            await asyncio.sleep(0)
        await super().disconnect(force)


def pending_idle_tasks(rig):
    task = rig.service._idle_task
    return [task] if task and not task.done() else []


async def start_song(rig):
    queue_songs(rig.player, "a")
    await rig.service.ensure_playing()
    assert rig.voice.client.playing


async def test_stop_while_playing_leaves_no_idle_task_even_when_the_song_end_reenters(tmp_path):
    rig = build_kept_alive(tmp_path, client=SongEndingClient())
    await start_song(rig)

    await rig.service.stop()
    await settle(50)

    assert pending_idle_tasks(rig) == []
    assert rig.sleeper.waiters == []
    assert rig.voice.client.disconnect_calls == 1


async def test_leave_while_playing_leaves_no_idle_task_even_when_the_song_end_reenters(tmp_path):
    rig = build_kept_alive(tmp_path, client=SongEndingClient())
    await start_song(rig)

    assert await rig.service.leave() is True
    await settle(50)

    assert pending_idle_tasks(rig) == []
    assert rig.sleeper.waiters == []


async def test_a_normal_end_of_queue_still_arms_exactly_one_idle_timer(tmp_path):
    rig = build_kept_alive(tmp_path, client=SongEndingClient())
    await start_song(rig)

    rig.voice.client.finish_song()
    await settle(50)

    assert len(pending_idle_tasks(rig)) == 1
    assert len(rig.sleeper.waiters) == 1


async def test_alone_timeout_still_disconnects_while_kept_alive_and_cancels_the_idle_timer(tmp_path, monkeypatch):
    from music import service as service_module

    monkeypatch.setattr(service_module, "ALONE_TIMEOUT", 0)
    client = FakeVoiceClient(channel_with(FakeMember(is_bot=True)))
    rig = build_kept_alive(tmp_path, client=client)
    await finish_queue(rig)
    armed = rig.service._idle_task

    await rig.service.alone_timeout()
    await settle()

    assert client.disconnect_calls == 1
    assert armed.cancelled()
    assert rig.notifier.has_text_containing("quedé solo en el canal")


async def test_leave_disconnects_clears_the_session_and_cancels_the_idle_timer(tmp_path):
    rig = build_kept_alive(tmp_path)
    client = rig.voice.client
    client.playing = True
    queue_songs(rig.player, "a", "b")
    rig.service._arm_idle()
    armed = rig.service._idle_task

    assert await rig.service.leave() is True
    await settle()

    assert client.disconnect_calls == 1
    assert rig.player.queue == []
    assert rig.player.current is None
    assert armed.cancelled()


async def test_leave_without_a_connected_client_does_nothing(tmp_path):
    rig = build_service(tmp_path)

    assert await rig.service.leave() is False


async def test_song_end_with_an_error_logs_it_and_still_schedules_the_next_song(tmp_path, capsys):
    rig = build_service(tmp_path, client=FakeVoiceClient())
    queue_songs(rig.player, "a", "b")
    await rig.service.ensure_playing()
    rig.voice.client.playing = False

    await asyncio.get_running_loop().run_in_executor(None, rig.service._on_song_end, ValueError("ffmpeg murió"))
    await settle(20)

    assert "[musica] error de reproducción: ValueError: ffmpeg murió" in capsys.readouterr().out
    assert rig.player.current["title"] == "Song b"


async def test_song_end_error_log_is_one_capped_line(tmp_path, capsys):
    rig = build_service(tmp_path, client=FakeVoiceClient())
    error = RuntimeError("ffmpeg\nhttps://rr.googlevideo.com/signed " + "x" * 1000)

    rig.service._on_song_end(error)
    await settle(20)

    lines = [line for line in capsys.readouterr().out.splitlines() if line.startswith("[musica]")]
    assert len(lines) == 1
    assert lines[0].startswith("[musica] error de reproducción: RuntimeError: ffmpeg https://")
    assert len(lines[0]) < 400


async def test_song_end_without_error_prints_nothing(tmp_path, capsys):
    rig = build_service(tmp_path, client=FakeVoiceClient())

    rig.service._on_song_end(None)
    await settle(20)

    assert capsys.readouterr().out == ""
