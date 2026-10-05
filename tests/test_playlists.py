import json
import os

from music import playlists as playlist_store
from tests.conftest import ORIGINAL_PLAYLISTS_DIR
from tests.fakes import FakeContext, queued_pairs

GID = 1


def stored(isolated_bot, gid=GID):
    path = os.path.join(isolated_bot.PLAYLISTS_DIR, f"{gid}.json")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def seed(isolated_bot, data, gid=GID):
    path = os.path.join(isolated_bot.PLAYLISTS_DIR, f"{gid}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)


def song(title, video_id):
    return {"title": title, "url": f"https://www.youtube.com/watch?v={video_id}"}


def test_default_playlists_directory_is_next_to_bot_module(isolated_bot):
    bot_directory = os.path.dirname(os.path.abspath(isolated_bot.__file__))
    assert ORIGINAL_PLAYLISTS_DIR == os.path.join(bot_directory, "playlists")


def test_playlist_file_is_named_after_guild_id(isolated_bot):
    assert playlist_store.playlist_path(isolated_bot.PLAYLISTS_DIR, 42) == os.path.join(isolated_bot.PLAYLISTS_DIR, "42.json")


def test_loading_unknown_guild_returns_empty(isolated_bot):
    assert playlist_store.load_playlists(isolated_bot.PLAYLISTS_DIR, 999) == {}


def test_save_then_load_roundtrips_unicode(isolated_bot):
    data = {"favoritas": [song("Canción ñandú", "x")]}

    playlist_store.save_playlists(isolated_bot.PLAYLISTS_DIR, GID, data)

    assert playlist_store.load_playlists(isolated_bot.PLAYLISTS_DIR, GID) == data
    with open(playlist_store.playlist_path(isolated_bot.PLAYLISTS_DIR, GID), encoding="utf-8") as f:
        assert "ñandú" in f.read()


async def test_create_normalizes_name_and_persists(isolated_bot, ctx):
    await isolated_bot.pl_create.callback(ctx, nombre="  Favoritas ")

    assert stored(isolated_bot) == {"favoritas": []}
    assert ctx.notifier.has_text_containing("**favoritas** creada")


async def test_create_rejects_duplicates(isolated_bot, ctx):
    seed(isolated_bot, {"favoritas": []})

    await isolated_bot.pl_create.callback(ctx, nombre="favoritas")

    assert ctx.notifier.has_text_containing("Ya existe la playlist")


async def test_create_without_name_shows_usage(isolated_bot, ctx):
    await isolated_bot.pl_create.callback(ctx, nombre=None)

    assert ctx.notifier.has_text_containing("Uso: `!playlist create <nombre>`")


async def test_add_stores_current_song_once(isolated_bot, ctx):
    seed(isolated_bot, {"favoritas": []})
    isolated_bot.players.get(GID).current = {"title": "Tusa", "url": "https://www.youtube.com/watch?v=tusa"}

    await isolated_bot.pl_add.callback(ctx, nombre="favoritas")
    await isolated_bot.pl_add.callback(ctx, nombre="favoritas")

    assert stored(isolated_bot) == {"favoritas": [song("Tusa", "tusa")]}
    assert ctx.notifier.has_text_containing("agregada a **favoritas** (1 canciones)")
    assert ctx.notifier.has_text_containing("ya está en **favoritas**")


async def test_add_without_current_song_is_rejected(isolated_bot, ctx):
    seed(isolated_bot, {"favoritas": []})

    await isolated_bot.pl_add.callback(ctx, nombre="favoritas")

    assert ctx.notifier.has_text_containing("No hay ninguna canción sonando")
    assert stored(isolated_bot) == {"favoritas": []}


async def test_add_to_unknown_playlist_is_rejected(isolated_bot, ctx):
    isolated_bot.players.get(GID).current = {"title": "Tusa", "url": "u"}

    await isolated_bot.pl_add.callback(ctx, nombre="nada")

    assert ctx.notifier.has_text_containing("No existe la playlist **nada**")


async def test_load_enqueues_every_song_and_starts_playback(isolated_bot, patch_extractor, disconnected_ctx):
    seed(isolated_bot, {"favoritas": [song("Uno", "uno"), song("Dos", "dos")]})

    await isolated_bot.pl_load.callback(disconnected_ctx, nombre="Favoritas")

    assert disconnected_ctx.voice_client.play_calls == 1
    assert patch_extractor.download_calls == ["https://www.youtube.com/watch?v=uno"]
    assert queued_pairs(isolated_bot, GID) == [("https://www.youtube.com/watch?v=dos", "Dos")]
    assert disconnected_ctx.notifier.has_text_containing("2 canciones añadidas a la cola")


async def test_load_of_empty_or_missing_playlist_is_rejected(isolated_bot, patch_extractor, ctx):
    seed(isolated_bot, {"vacia": []})

    await isolated_bot.pl_load.callback(ctx, nombre="vacia")
    await isolated_bot.pl_load.callback(ctx, nombre="inexistente")

    assert len([t for t in ctx.notifier.texts if "no existe o está vacía" in t]) == 2
    assert patch_extractor.download_calls == []


async def test_load_requires_author_in_voice(isolated_bot, patch_extractor):
    ctx = FakeContext(in_voice=False)
    seed(isolated_bot, {"favoritas": [song("Uno", "uno")]})

    await isolated_bot.pl_load.callback(ctx, nombre="favoritas")

    assert ctx.notifier.has_text_containing("Debes estar en un canal de voz")
    assert queued_pairs(isolated_bot, GID) == []


async def test_list_shows_playlists_with_song_counts(isolated_bot, ctx):
    seed(isolated_bot, {"a": [song("Uno", "uno")], "b": []})

    await isolated_bot.pl_list.callback(ctx)

    description = ctx.notifier.messages[-1]["embed"].description
    assert "**a** — 1 canciones" in description
    assert "**b** — 0 canciones" in description


async def test_list_without_playlists_reports_none(isolated_bot, ctx):
    await isolated_bot.pl_list.callback(ctx)

    assert ctx.notifier.has_text_containing("No hay playlists guardadas")


async def test_show_lists_songs_with_positions_capped_at_twenty(isolated_bot, ctx):
    seed(isolated_bot, {"larga": [song(f"T{i}", f"v{i}") for i in range(23)]})

    await isolated_bot.pl_show.callback(ctx, nombre="larga")

    embed = ctx.notifier.messages[-1]["embed"]
    lines = embed.description.splitlines()
    assert embed.title == "📋 larga (23 canciones)"
    assert lines[0] == "`1.` T0"
    assert lines[19] == "`20.` T19"
    assert lines[20] == "*...y 3 más*"


async def test_show_of_empty_playlist_says_so(isolated_bot, ctx):
    seed(isolated_bot, {"vacia": []})

    await isolated_bot.pl_show.callback(ctx, nombre="vacia")

    assert ctx.notifier.has_text_containing("**vacia** está vacía")


async def test_remove_deletes_one_based_position(isolated_bot, ctx):
    seed(isolated_bot, {"p": [song("Uno", "1"), song("Dos", "2"), song("Tres", "3")]})

    await isolated_bot.pl_remove.callback(ctx, nombre="p", posicion=2)

    assert [s["title"] for s in stored(isolated_bot)["p"]] == ["Uno", "Tres"]
    assert ctx.notifier.has_text_containing("**Dos** eliminada de **p**")


async def test_remove_with_invalid_position_leaves_playlist_untouched(isolated_bot, ctx):
    seed(isolated_bot, {"p": [song("Uno", "1")]})

    await isolated_bot.pl_remove.callback(ctx, nombre="p", posicion=0)
    await isolated_bot.pl_remove.callback(ctx, nombre="p", posicion=5)

    assert len(stored(isolated_bot)["p"]) == 1
    assert len([t for t in ctx.notifier.texts if "Posición inválida" in t]) == 2


async def test_delete_removes_whole_playlist(isolated_bot, ctx):
    seed(isolated_bot, {"p": [song("Uno", "1")], "q": []})

    await isolated_bot.pl_delete.callback(ctx, nombre="p")

    assert stored(isolated_bot) == {"q": []}
    assert ctx.notifier.has_text_containing("Playlist **p** eliminada")


async def test_playlists_are_isolated_per_guild(isolated_bot, ctx):
    seed(isolated_bot, {"p": []}, gid=1)

    other = FakeContext(guild_id=2, connected=True)
    await isolated_bot.pl_list.callback(other)

    assert other.notifier.has_text_containing("No hay playlists guardadas")
