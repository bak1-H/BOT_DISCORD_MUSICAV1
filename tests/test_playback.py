import asyncio
import os

import discord

from tests.fakes import FakeContext, FakeMember, FakeVoiceChannel, settle

GID = 1


def queue_urls(bot_module, *video_ids):
    for video_id in video_ids:
        url = f"https://www.youtube.com/watch?v={video_id}"
        bot_module.queues.setdefault(GID, []).append((url, f"Song {video_id}"))


async def test_play_connects_enqueues_and_starts_playback(isolated_bot, patch_extractor, disconnected_ctx):
    ctx = disconnected_ctx
    patch_extractor.search_entries = [patch_extractor.entry("abc")]

    await isolated_bot.play.callback(ctx, search="tusa karol g")

    assert ctx.voice_channel.connect_calls == 1
    assert ctx.voice_client.play_calls == 1
    assert isolated_bot.current_song[GID]["title"] == "Song abc"
    assert isolated_bot.queues[GID] == []
    assert patch_extractor.download_calls == ["https://www.youtube.com/watch?v=abc"]
    assert ctx.notifier.has_text_containing("Buscando")
    assert ctx.notifier.has_embed_titled("Reproduciendo ahora")
    assert isolated_bot.last_text_channel[GID] is ctx.channel


async def test_play_without_query_asks_for_a_song(isolated_bot, ctx):
    await isolated_bot.play.callback(ctx, search=None)

    assert ctx.notifier.has_text_containing("Escribe el nombre de una canción")
    assert ctx.voice_client.play_calls == 0


async def test_play_requires_author_in_voice_channel(isolated_bot, patch_extractor):
    ctx = FakeContext(in_voice=False)

    await isolated_bot.play.callback(ctx, search="algo")

    assert ctx.notifier.has_text_containing("Debes estar en un canal de voz")
    assert patch_extractor.search_calls == []


async def test_play_while_something_is_playing_only_queues(isolated_bot, patch_extractor, ctx):
    ctx.voice_client.playing = True
    patch_extractor.search_entries = [patch_extractor.entry("next1")]

    await isolated_bot.play.callback(ctx, search="otra")

    assert ctx.notifier.has_embed_titled("Añadido a la cola")
    assert not ctx.notifier.has_embed_titled("Reproduciendo ahora")
    assert ctx.voice_client.play_calls == 0
    assert patch_extractor.download_calls == []
    assert isolated_bot.queues[GID] == [("https://www.youtube.com/watch?v=next1", "Song next1")]


async def test_play_while_playback_lock_is_held_reports_in_queue_then_starts(isolated_bot, patch_extractor, ctx):
    patch_extractor.search_entries = [patch_extractor.entry("busy1")]
    lock = isolated_bot.get_playback_lock(GID)
    await lock.acquire()

    task = asyncio.create_task(isolated_bot.play.callback(ctx, search="x"))
    await settle(20)

    assert ctx.notifier.has_embed_titled("Añadido a la cola")
    assert ctx.voice_client.play_calls == 0

    lock.release()
    await task

    assert ctx.voice_client.play_calls == 1
    assert ctx.notifier.has_embed_titled("Reproduciendo ahora")


async def test_play_search_without_results_reports_not_found(isolated_bot, patch_extractor, ctx):
    patch_extractor.search_entries = []

    await isolated_bot.play.callback(ctx, search="nada")

    assert ctx.notifier.has_text_containing("No se encontraron resultados")
    assert ctx.voice_client.play_calls == 0


async def test_play_search_login_block_reports_bot_check(isolated_bot, patch_extractor, ctx):
    patch_extractor.search_error = RuntimeError("Sign in to confirm you're not a bot")

    await isolated_bot.play.callback(ctx, search="x")

    assert ctx.notifier.has_text_containing("YouTube bloqueó la búsqueda")


async def test_play_search_generic_error_reports_search_failure(isolated_bot, patch_extractor, ctx):
    patch_extractor.search_error = RuntimeError("network down")

    await isolated_bot.play.callback(ctx, search="x")

    assert ctx.notifier.has_text_containing("Hubo un error procesando la búsqueda")


async def test_after_callback_schedules_play_next_on_bot_loop(isolated_bot, patch_extractor, ctx, monkeypatch):
    patch_extractor.search_entries = [patch_extractor.entry("abc")]
    await isolated_bot.play.callback(ctx, search="x")
    scheduled = []

    async def recording_play_next(received_ctx):
        scheduled.append(received_ctx)

    monkeypatch.setattr(isolated_bot, "play_next", recording_play_next)

    ctx.voice_client.finish_song()
    await asyncio.sleep(0.05)

    assert scheduled == [ctx]


async def test_song_end_advances_to_next_queued_song(isolated_bot, patch_extractor, ctx):
    queue_urls(isolated_bot, "a", "b")

    await isolated_bot.ensure_playing(ctx)
    assert isolated_bot.current_song[GID]["title"] == "Song a"

    ctx.voice_client.playing = False
    await isolated_bot.play_next(ctx)

    assert isolated_bot.current_song[GID]["title"] == "Song b"
    assert isolated_bot.queues[GID] == []
    assert ctx.voice_client.play_calls == 2


async def test_ensure_playing_does_nothing_when_already_playing(isolated_bot, patch_extractor, ctx):
    ctx.voice_client.playing = True
    queue_urls(isolated_bot, "a")

    await isolated_bot.ensure_playing(ctx)

    assert patch_extractor.download_calls == []
    assert len(isolated_bot.queues[GID]) == 1


async def test_ensure_playing_does_nothing_when_paused_or_disconnected(isolated_bot, patch_extractor, ctx):
    queue_urls(isolated_bot, "a")
    ctx.voice_client.paused = True
    await isolated_bot.ensure_playing(ctx)
    ctx.voice_client.paused = False
    ctx.voice_client.connected = False
    await isolated_bot.ensure_playing(ctx)

    assert patch_extractor.download_calls == []


async def test_loop_song_reinserts_previous_song_at_index_zero(isolated_bot, patch_extractor, ctx):
    isolated_bot.loop_mode[GID] = "song"
    isolated_bot.current_song[GID] = {"title": "Song prev", "url": "https://www.youtube.com/watch?v=prev"}
    queue_urls(isolated_bot, "other")

    await isolated_bot._play_next_locked(ctx)

    assert patch_extractor.download_calls == ["https://www.youtube.com/watch?v=prev"]
    assert isolated_bot.current_song[GID]["title"] == "Song prev"
    assert isolated_bot.queues[GID] == [("https://www.youtube.com/watch?v=other", "Song other")]


async def test_loop_queue_appends_previous_song_at_the_end(isolated_bot, patch_extractor, ctx):
    isolated_bot.loop_mode[GID] = "queue"
    isolated_bot.current_song[GID] = {"title": "Song prev", "url": "https://www.youtube.com/watch?v=prev"}
    queue_urls(isolated_bot, "first")

    await isolated_bot._play_next_locked(ctx)

    assert patch_extractor.download_calls == ["https://www.youtube.com/watch?v=first"]
    assert isolated_bot.queues[GID] == [("https://www.youtube.com/watch?v=prev", "Song prev")]


async def test_loop_off_discards_previous_song(isolated_bot, patch_extractor, ctx):
    isolated_bot.current_song[GID] = {"title": "Song prev", "url": "https://www.youtube.com/watch?v=prev"}
    queue_urls(isolated_bot, "first")

    await isolated_bot._play_next_locked(ctx)

    assert isolated_bot.queues[GID] == []


async def test_loop_command_cycles_off_song_queue(isolated_bot, ctx):
    await isolated_bot.loop.callback(ctx)
    await isolated_bot.loop.callback(ctx)
    await isolated_bot.loop.callback(ctx)

    assert isolated_bot.loop_mode[GID] == "off"
    assert ctx.notifier.has_text_containing("canción actual")
    assert ctx.notifier.has_text_containing("cola completa")
    assert ctx.notifier.has_text_containing("desactivado")


async def test_empty_queue_without_radio_clears_song_and_disconnects(isolated_bot, patch_extractor, ctx):
    isolated_bot.current_song[GID] = {"title": "Song prev", "url": "u"}

    await isolated_bot._play_next_locked(ctx)

    assert GID not in isolated_bot.current_song
    assert ctx.voice_client.disconnect_calls == 1
    assert patch_extractor.download_calls == []


async def test_empty_queue_with_radio_enqueues_ai_suggestion_and_plays_it(isolated_bot, patch_extractor, ctx, monkeypatch):
    isolated_bot.radio_query[GID] = "reggaeton viejo"
    isolated_bot.reset_radio_state(GID)
    seen_requests = []

    async def fake_suggest_songs(seed, recent_titles, count):
        seen_requests.append((seed, list(recent_titles), count))
        return ["Daddy Yankee - Gasolina"]

    monkeypatch.setattr(isolated_bot.ai_dj, "suggest_songs", fake_suggest_songs)
    patch_extractor.search_entries = [patch_extractor.entry("gaso")]

    await isolated_bot._play_next_locked(ctx)

    assert seen_requests == [("reggaeton viejo", [], 5)]
    assert patch_extractor.search_calls[0]["query"] == "Daddy Yankee - Gasolina"
    assert isolated_bot.current_song[GID]["title"] == "Song gaso"
    assert "gaso" in isolated_bot.radio_played[GID]
    assert list(isolated_bot.radio_history[GID]) == ["Song gaso"]
    assert ctx.voice_client.play_calls == 1


async def test_radio_falls_back_to_plain_search_when_ai_has_no_suggestions(isolated_bot, patch_extractor, ctx, monkeypatch):
    isolated_bot.radio_query[GID] = "salsa"
    isolated_bot.reset_radio_state(GID)

    async def no_suggestions(seed, recent_titles, count):
        return []

    monkeypatch.setattr(isolated_bot.ai_dj, "suggest_songs", no_suggestions)
    patch_extractor.search_entries = [patch_extractor.entry("only")]

    assert await isolated_bot.radio_next(ctx) is True

    assert patch_extractor.search_calls == [{"query": "salsa", "search_count": 5}]
    assert isolated_bot.queues[GID] == [("https://www.youtube.com/watch?v=only", "Song only")]


async def test_radio_skips_recently_played_and_last_video(isolated_bot, patch_extractor, ctx, monkeypatch):
    isolated_bot.radio_query[GID] = "rock"
    isolated_bot.reset_radio_state(GID)
    isolated_bot.radio_played[GID].add("seen")
    isolated_bot.last_video_id[GID] = "last"

    async def no_suggestions(seed, recent_titles, count):
        return []

    monkeypatch.setattr(isolated_bot.ai_dj, "suggest_songs", no_suggestions)
    patch_extractor.search_entries = [
        patch_extractor.entry("seen"),
        patch_extractor.entry("last"),
        patch_extractor.entry("fresh"),
    ]

    assert await isolated_bot.radio_next(ctx) is True

    assert isolated_bot.queues[GID] == [("https://www.youtube.com/watch?v=fresh", "Song fresh")]


async def test_radio_next_without_active_query_returns_false(isolated_bot, ctx):
    assert await isolated_bot.radio_next(ctx) is False


async def test_three_consecutive_failures_clear_queue_and_disconnect(isolated_bot, patch_extractor, ctx):
    queue_urls(isolated_bot, "a", "b", "c", "d", "e")
    for _ in range(3):
        patch_extractor.download_outcomes.append(RuntimeError("boom"))

    await isolated_bot._play_next_locked(ctx)

    assert isolated_bot.queues[GID] == []
    assert ctx.voice_client.disconnect_calls == 1
    assert len(patch_extractor.download_calls) == 3
    assert isolated_bot.playnext_fail_count[GID] == 3
    error_messages = [t for t in ctx.notifier.texts if "Error al reproducir" in t]
    assert len(error_messages) == 1
    assert ctx.notifier.has_text_containing("Falló la reproducción varias veces")


async def test_a_success_after_failures_resets_the_failure_counter(isolated_bot, patch_extractor, ctx):
    queue_urls(isolated_bot, "a", "b")
    patch_extractor.download_outcomes.append(RuntimeError("boom"))

    await isolated_bot._play_next_locked(ctx)

    assert isolated_bot.playnext_fail_count[GID] == 0
    assert isolated_bot.current_song[GID]["title"] == "Song b"
    assert ctx.voice_client.disconnect_calls == 0


async def test_login_block_skips_song_without_counting_failure(isolated_bot, patch_extractor, ctx):
    queue_urls(isolated_bot, "blocked", "good")
    patch_extractor.download_outcomes.append(RuntimeError("Sign in to confirm you're not a bot"))

    await isolated_bot._play_next_locked(ctx)

    assert ctx.notifier.has_text_containing("Song blocked")
    assert ctx.notifier.has_text_containing("bloqueado por YouTube")
    assert isolated_bot.playnext_fail_count[GID] == 0
    assert isolated_bot.current_song[GID]["title"] == "Song good"
    assert ctx.voice_client.disconnect_calls == 0


async def test_already_playing_error_aborts_without_counting_or_retrying(isolated_bot, patch_extractor, ctx, monkeypatch):
    queue_urls(isolated_bot, "a", "b")

    def refuse_to_play(source, after=None):
        raise discord.ClientException("Already playing audio.")

    monkeypatch.setattr(ctx.voice_client, "play", refuse_to_play)

    await isolated_bot._play_next_locked(ctx)

    assert isolated_bot.playnext_fail_count[GID] == 0
    assert patch_extractor.download_calls == ["https://www.youtube.com/watch?v=a"]
    assert isolated_bot.queues[GID] == [("https://www.youtube.com/watch?v=b", "Song b")]
    assert not ctx.notifier.has_text_containing("Error al reproducir")


async def test_playback_aborts_and_cleans_file_when_voice_disconnected_during_download(isolated_bot, patch_extractor, ctx):
    queue_urls(isolated_bot, "a")
    ctx.voice_client.connected = False

    await isolated_bot._play_next_locked(ctx)

    assert ctx.voice_client.play_calls == 0
    assert GID not in isolated_bot.current_audio_file
    assert os.listdir(isolated_bot.DOWNLOAD_DIR) == []


async def test_previous_audio_file_is_removed_when_advancing(isolated_bot, patch_extractor, ctx):
    queue_urls(isolated_bot, "a", "b")
    await isolated_bot.ensure_playing(ctx)
    first_file = isolated_bot.current_audio_file[GID]
    assert os.path.exists(first_file)

    ctx.voice_client.playing = False
    await isolated_bot.play_next(ctx)

    assert not os.path.exists(first_file)
    assert isolated_bot.current_audio_file[GID] != first_file


async def test_skip_stops_current_song(isolated_bot, ctx):
    ctx.voice_client.playing = True

    await isolated_bot.skip.callback(ctx)

    assert ctx.voice_client.stop_calls == 1
    assert ctx.notifier.has_text_containing("Canción saltada")


async def test_skip_without_playback_reports_nothing_playing(isolated_bot, ctx):
    await isolated_bot.skip.callback(ctx)

    assert ctx.voice_client.stop_calls == 0
    assert ctx.notifier.has_text_containing("No hay nada reproduciéndose")


async def test_stop_clears_all_guild_state_and_disconnects(isolated_bot, patch_extractor, ctx, tmp_path):
    audio_file = tmp_path / "current.webm"
    audio_file.write_bytes(b"audio")
    queue_urls(isolated_bot, "a", "b")
    isolated_bot.current_song[GID] = {"title": "Song x", "url": "u"}
    isolated_bot.loop_mode[GID] = "song"
    isolated_bot.radio_query[GID] = "pop"
    isolated_bot.reset_radio_state(GID)
    isolated_bot.current_audio_file[GID] = str(audio_file)
    ctx.voice_client.playing = True

    await isolated_bot.stop.callback(ctx)

    assert isolated_bot.queues[GID] == []
    assert GID not in isolated_bot.current_song
    assert GID not in isolated_bot.loop_mode
    assert GID not in isolated_bot.radio_query
    assert GID not in isolated_bot.radio_played
    assert GID not in isolated_bot.radio_history
    assert GID not in isolated_bot.radio_suggestions
    assert not audio_file.exists()
    assert ctx.voice_client.stop_calls == 1
    assert ctx.voice_client.disconnect_calls == 1
    assert ctx.notifier.has_text_containing("Reproducción detenida")


async def test_pause_and_resume_toggle_playback(isolated_bot, ctx):
    ctx.voice_client.playing = True

    await isolated_bot.pause.callback(ctx)
    assert ctx.voice_client.paused is True
    await isolated_bot.resume.callback(ctx)

    assert ctx.voice_client.playing is True
    assert ctx.notifier.has_text_containing("Pausado")
    assert ctx.notifier.has_text_containing("Reanudado")


async def test_queue_command_lists_current_song_and_first_ten_entries(isolated_bot, ctx):
    isolated_bot.current_song[GID] = {"title": "Now", "url": "u", "duration": 65}
    queue_urls(isolated_bot, *[f"q{i}" for i in range(12)])
    isolated_bot.radio_query[GID] = "jazz"

    await isolated_bot.queue.callback(ctx)

    embed = ctx.notifier.messages[-1]["embed"]
    fields = {field.name: field.value for field in embed.fields}
    assert "**Now** `1:05`" == fields["▶️ Reproduciendo ahora"]
    listing = fields["📋 En cola"].splitlines()
    assert listing[0] == "`1.` Song q0"
    assert listing[9] == "`10.` Song q9"
    assert listing[10] == "*...y 2 más*"
    assert embed.footer.text == "📻 Radio activa: jazz"


async def test_queue_command_reports_empty_queue(isolated_bot, ctx):
    await isolated_bot.queue.callback(ctx)

    assert ctx.notifier.messages[-1]["embed"].description == "La cola está vacía."


async def test_alone_timeout_clears_state_and_disconnects(isolated_bot, patch_extractor, ctx, monkeypatch):
    monkeypatch.setattr(isolated_bot, "ALONE_TIMEOUT", 0)
    voice_client = ctx.voice_client
    voice_client.channel = FakeVoiceChannel(ctx=ctx, members=[FakeMember(is_bot=True)])
    queue_urls(isolated_bot, "a")
    isolated_bot.current_song[GID] = {"title": "Song x", "url": "u"}
    isolated_bot.loop_mode[GID] = "queue"
    isolated_bot.radio_query[GID] = "pop"
    isolated_bot.last_text_channel[GID] = ctx.channel

    await isolated_bot._alone_timeout(voice_client, GID)

    assert GID not in isolated_bot.queues
    assert GID not in isolated_bot.current_song
    assert GID not in isolated_bot.loop_mode
    assert GID not in isolated_bot.radio_query
    assert voice_client.disconnect_calls == 1
    assert ctx.notifier.has_text_containing("quedé solo en el canal")


async def test_alone_timeout_does_nothing_when_a_human_is_present(isolated_bot, ctx, monkeypatch):
    monkeypatch.setattr(isolated_bot, "ALONE_TIMEOUT", 0)
    voice_client = ctx.voice_client
    voice_client.channel = FakeVoiceChannel(ctx=ctx, members=[FakeMember(is_bot=False)])
    queue_urls(isolated_bot, "a")

    await isolated_bot._alone_timeout(voice_client, GID)

    assert voice_client.disconnect_calls == 0
    assert len(isolated_bot.queues[GID]) == 1


async def test_guilds_do_not_share_queues(isolated_bot, patch_extractor):
    first = FakeContext(guild_id=1, connected=True)
    second = FakeContext(guild_id=2, connected=True)
    first.voice_client.playing = True
    second.voice_client.playing = True
    patch_extractor.search_entries = [patch_extractor.entry("only-in-one")]

    await isolated_bot.play.callback(first, search="x")

    assert len(isolated_bot.queues[1]) == 1
    assert isolated_bot.queues.get(2, []) == []


async def test_connect_timeout_reports_error_and_aborts(isolated_bot, patch_extractor, disconnected_ctx):
    disconnected_ctx.voice_channel.connect_error = asyncio.TimeoutError()

    await isolated_bot.play.callback(disconnected_ctx, search="x")

    assert disconnected_ctx.notifier.has_text_containing("No pude conectarme al canal de voz (timeout)")
    assert patch_extractor.search_calls == []

