import os
import asyncio
import re
import traceback
from datetime import datetime, timezone, timedelta
import discord
from discord.ext import commands
from dotenv import load_dotenv
import base64
import ai_dj
import help_content
from lol.embeds import build_comparison_embed, build_summoner_embed
from lol.riot import RiotApi, RiotLookupError
from lol.service import LolService
from music import playlists as playlist_store, radio as radio_engine
from music.discord_adapters import ChannelNotifier, DiscordVoiceGateway, ffmpeg_audio_source
from music.embeds import format_duration, make_song_embed
from music.extractor import YtdlpExtractor
from music.lyrics import clean_title_for_lyrics, get_genius
from music.player import PlayerRegistry
from music.ports import ConnectResult
from music.service import MusicService
from music.ytdl import YtdlpSettings, is_youtube_login_block

load_dotenv()

os.environ["YT_DLP_JS_RUNTIME"] = "node"

# Agrega ffmpeg local al PATH si no está disponible globalmente
import shutil
if not shutil.which("ffmpeg"):
    _local_ffmpeg = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ffmpeg.exe")
    if os.path.exists(_local_ffmpeg):
        os.environ["PATH"] = os.path.dirname(_local_ffmpeg) + os.pathsep + os.environ["PATH"]
        print(f"[ffmpeg] usando binario local: {_local_ffmpeg}")

# ──────────────────── COOKIES ────────────────────
COOKIES_FILE = None
_here = os.path.dirname(os.path.abspath(__file__))

cookies_b64 = os.getenv("YOUTUBE_COOKIES_B64", "").strip()
if cookies_b64:
    try:
        _path = os.path.join(_here, "cookies.txt")
        with open(_path, "wb") as f:
            f.write(base64.b64decode(cookies_b64))
        COOKIES_FILE = _path
        print("[cookies] Cargadas desde variable de entorno")
    except Exception as e:
        print(f"[cookies] Error con env var: {e}")

if not COOKIES_FILE:
    _bundled = os.path.join(_here, "cookies.txt")
    if os.path.exists(_bundled):
        COOKIES_FILE = _bundled
        print(f"[cookies] Usando archivo bundled")

# ──────────────────── DISCORD ────────────────────
intents = discord.Intents.default()
intents.message_content = True


class MusicBot(commands.Bot):
    def __init__(self, **options):
        super().__init__(**options)
        self.playback_loop = None
        self.agent_listener = None
        self.voice_cls = None
        self.voice_activation = None

    def on_voice_connected(self, client):
        if self.voice_activation is not None:
            self.voice_activation.ensure_listening(client)

    async def setup_hook(self):
        self.playback_loop = asyncio.get_running_loop()
        if self.voice_cls is None:
            self.voice_cls = install_voice()
        if self.agent_listener is None:
            self.agent_listener = install_agent()
        if self.voice_cls is not None and self.voice_activation is None:
            self.voice_activation = install_voice_activation(self.agent_listener, self.playback_loop)


bot = MusicBot(command_prefix="!", intents=intents, case_insensitive=True, help_command=None)

# ──────────────────── CONFIG ────────────────────
YTDLP_PROXY = os.getenv("YTDLP_PROXY", "").strip() or None
RIOT_API_KEY = os.getenv("RIOT_API_KEY", "").strip()
RIOT_PLATFORM = os.getenv("RIOT_REGION", "la2")   # plataforma: la1/la2/na1/euw1...
RIOT_ROUTING = "americas"                          # LAS/LAN/NA usan americas
MAX_PLAYNEXT_FAILS = 3
PO_TOKEN = os.getenv("YOUTUBE_PO_TOKEN", "").strip()
VISITOR_DATA = os.getenv("YOUTUBE_VISITOR_DATA", "").strip()
DJ_MAX_SONGS = 15
DJ_DEFAULT_MAX_DURATION_S = 600
DJ_DURATION_CAP_S = 1800

YTDLP_SETTINGS = YtdlpSettings(
    proxy=YTDLP_PROXY,
    cookies_file=COOKIES_FILE,
    po_token=PO_TOKEN,
    visitor_data=VISITOR_DATA,
)

# ──────────────────── STATE ────────────────────
players = PlayerRegistry()


DOWNLOAD_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "downloads")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)


# ──────────────────── AUDIO EXTRACTION ────────────────────

extractor = YtdlpExtractor(YTDLP_SETTINGS, DOWNLOAD_DIR)

music_services = {}


def get_music_service(guild_id: int) -> MusicService:
    service = music_services.get(guild_id)
    if service is None:
        player = players.get(guild_id)
        service = MusicService(
            player=player,
            voice=DiscordVoiceGateway(
                bot,
                guild_id,
                voice_cls=bot.voice_cls,
                on_connected=bot.on_voice_connected if bot.voice_cls is not None else None,
            ),
            notifier=ChannelNotifier(player),
            extractor=extractor,
            audio_source_factory=ffmpeg_audio_source,
            loop=bot.playback_loop,
        )
        music_services[guild_id] = service
    return service


# ──────────────────── COMANDOS ────────────────────

async def send_quietly(ctx, message: str) -> None:
    try:
        await ctx.send(message)
    except Exception:
        pass


CONNECT_FAILURE_MESSAGES = {
    ConnectResult.TIMEOUT: "❌ No pude conectarme al canal de voz (timeout). Verifica que el bot tenga permisos y que no haya un firewall bloqueando UDP.",
    ConnectResult.REFUSED: "❌ No pude conectarme al canal de voz (permisos/capacidad).",
}


async def connect_to_author_voice(ctx) -> bool:
    if not ctx.author.voice:
        await ctx.send("❌ Debes estar en un canal de voz.")
        return False

    result = await get_music_service(ctx.guild.id).connect(ctx.author.voice.channel, ctx.channel)
    failure_message = CONNECT_FAILURE_MESSAGES.get(result)
    if failure_message:
        await send_quietly(ctx, failure_message)
        return False
    return True


@bot.command()
async def play(ctx, *, search: str = None):
    if not search:
        return await ctx.send("❌ Escribe el nombre de una canción.")
    if not await connect_to_author_voice(ctx):
        return

    await ctx.send(f"🔍 Buscando: **{search}**...")

    service = get_music_service(ctx.guild.id)
    try:
        result = await service.search_and_enqueue(search)
        if result is None:
            return await ctx.send("❌ No se encontraron resultados.")
        if result.busy:
            await ctx.send(embed=make_song_embed(result.preview, in_queue=True))
        await service.ensure_playing()

    except Exception as e:
        traceback.print_exc()
        print(f"Error en comando play: {e}")
        if is_youtube_login_block(e):
            return await ctx.send(
                "❌ YouTube bloqueó la búsqueda (bot-check). "
                "Prueba con `YTDLP_PROXY` o ejecuta el bot en una IP residencial."
            )
        await ctx.send("❌ Hubo un error procesando la búsqueda.")


@bot.command()
async def skip(ctx):
    if get_music_service(ctx.guild.id).skip():
        await ctx.send("⏭️ Canción saltada.")
    else:
        await ctx.send("❌ No hay nada reproduciéndose.")


@bot.command()
async def stop(ctx):
    await get_music_service(ctx.guild.id).stop()
    await ctx.send("⏹️ Reproducción detenida.")


@bot.command()
async def pause(ctx):
    if get_music_service(ctx.guild.id).pause():
        await ctx.send("⏸️ Pausado.")
    else:
        await ctx.send("❌ No hay nada reproduciéndose.")


@bot.command()
async def resume(ctx):
    if get_music_service(ctx.guild.id).resume():
        await ctx.send("▶️ Reanudado.")
    else:
        await ctx.send("❌ No hay nada pausado.")


@bot.command(aliases=["q"])
async def queue(ctx):
    player = players.get(ctx.guild.id)
    q = player.queue
    song = player.current

    embed = discord.Embed(title="🎵 Cola de reproducción", color=discord.Color.blurple())

    if song:
        duration_str = f" `{format_duration(song['duration'])}`" if song.get("duration") else ""
        embed.add_field(
            name="▶️ Reproduciendo ahora",
            value=f"**{song['title']}**{duration_str}",
            inline=False,
        )
    if q:
        lines = [f"`{i + 1}.` {track.title}" for i, track in enumerate(q[:10])]
        if len(q) > 10:
            lines.append(f"*...y {len(q) - 10} más*")
        embed.add_field(name="📋 En cola", value="\n".join(lines), inline=False)
    elif not song:
        embed.description = "La cola está vacía."

    rq = player.radio.query
    if rq:
        embed.set_footer(text=f"📻 Radio activa: {rq}")

    await ctx.send(embed=embed)


@bot.command(aliases=["nowplaying"])
async def np(ctx):
    song = players.get(ctx.guild.id).current
    if not song:
        return await ctx.send("❌ No hay nada reproduciéndose ahora.")
    await ctx.send(embed=make_song_embed(song))


@bot.command()
async def lyrics(ctx, *, song: str = None):
    if not song:
        song_data = players.get(ctx.guild.id).current
        song = song_data["title"] if song_data else None
    if not song:
        return await ctx.send("❌ Escribe el nombre de la canción o reproduce una primero.")

    title = clean_title_for_lyrics(song)
    try:
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(None, lambda: get_genius().search_song(title))
        if not result or not result.lyrics:
            return await ctx.send("❌ Letra no encontrada.")
        text = result.lyrics
        if len(text) > 2000:
            text = text[:1990] + "..."
        await ctx.send(f"🎶 **{result.title} – {result.artist}**\n\n{text}")
    except Exception as e:
        print(f"Lyrics error: {e}")
        await ctx.send("❌ Error al obtener la letra.")


@bot.command()
async def radio(ctx, *, query: str = None):
    service = get_music_service(ctx.guild.id)

    if not query or query.lower() == "off":
        service.stop_radio()
        await ctx.send("📻 Radio desactivada.")
        return

    if not await connect_to_author_voice(ctx):
        return

    service.start_radio(query)

    embed = discord.Embed(
        title="📻 Radio activada",
        description=f"Reproduciendo canciones de **{query}** en bucle.",
        color=discord.Color.og_blurple(),
    )
    await ctx.send(embed=embed)

    if await service.radio_next():
        await service.ensure_playing()
    else:
        service.stop_radio()
        await ctx.send("❌ No se encontraron canciones para ese estilo.")


@bot.command()
async def dj(ctx, *, request: str = None):
    if not request:
        return await ctx.send("❌ Dime qué quieres escuchar. Ej: `!dj 10 de Bad Bunny sin videos de 1 hora`")
    if not await connect_to_author_voice(ctx):
        return

    gid = ctx.guild.id
    service = get_music_service(gid)
    await ctx.send("🤔 Pensando...")

    plan = await ai_dj.plan_playlist(request, DJ_MAX_SONGS, DJ_DEFAULT_MAX_DURATION_S)
    if plan is None:
        return await ctx.send("❌ No pude armar la selección con IA. Revisa que `GEMINI_API_KEY` esté configurada o usa `!play`.")
    if not plan.is_playlist:
        understood = f" Entendí: *{plan.summary}*." if plan.summary else ""
        return await ctx.send(
            f"🤷 Por ahora `!dj` solo arma selecciones de canciones.{understood}\n"
            "Para manejar la cola usa `!queue`, `!skip` o `!stop`. No agregué nada."
        )

    await ctx.send(f"🎧 Entendí: **{(plan.summary or request).rstrip('.')}**. Buscando canciones...")

    max_duration_s = min(plan.max_duration_s, DJ_DURATION_CAP_S)
    added: list[str] = []
    skipped: list[str] = []
    seen_ids: set[str] = set()

    for suggestion in plan.songs:
        if not ctx.voice_client or not ctx.voice_client.is_connected():
            break
        try:
            entries = await radio_engine.search_entries(service.extract_info, suggestion, search_count=3)
        except Exception as e:
            print(f"DJ error buscando '{suggestion}': {e}")
            skipped.append(suggestion)
            continue

        pick = next((e for e in entries if radio_engine.is_playable_candidate(e, max_duration_s, seen_ids)), None)
        title = radio_engine.enqueue_entry(players.get(gid), pick) if pick else None
        if title is None:
            skipped.append(suggestion)
            continue

        seen_ids.add(pick["id"])
        added.append(title)
        if len(added) == 1:
            await service.ensure_playing()

    if not added:
        return await ctx.send("❌ No encontré canciones que cumplan con el pedido.")

    embed = discord.Embed(
        title="🎧 Selección del DJ",
        description="\n".join(f"`{i}.` {title}" for i, title in enumerate(added, start=1)),
        color=discord.Color.og_blurple(),
    )
    footer = f"Duración máxima: {format_duration(max_duration_s)}"
    if skipped:
        footer += f" · {len(skipped)} sugerencias descartadas (no encontradas o muy largas)"
    embed.set_footer(text=footer)
    await ctx.send(embed=embed)


@bot.command()
async def comandos(ctx):
    embed = discord.Embed(title="🎵 Comandos del Bot de Música", color=discord.Color.blurple())
    embed.add_field(name="!play <canción o URL>", value="Reproduce o añade a la cola.", inline=False)
    embed.add_field(name="!skip", value="Salta la canción actual.", inline=False)
    embed.add_field(name="!stop", value="Detiene y desconecta el bot.", inline=False)
    embed.add_field(name="!pause / !resume", value="Pausa o reanuda la reproducción.", inline=False)
    embed.add_field(name="!queue / !q", value="Muestra la cola de reproducción.", inline=False)
    embed.add_field(name="!np / !nowplaying", value="Muestra la canción actual.", inline=False)
    embed.add_field(name="!lyrics [canción]", value="Muestra la letra de la canción.", inline=False)
    embed.add_field(name="!loop", value="Cicla entre: sin loop → repetir canción → repetir cola.", inline=False)
    embed.add_field(name="!dj <pedido>", value="Arma una selección con IA. Ej: `!dj 10 de Bad Bunny sin videos de 1 hora`.", inline=False)
    embed.add_field(name="!radio <estilo>", value="Reproduce canciones del estilo en bucle. `!radio off` para detener.", inline=False)
    embed.add_field(name="!clear <n>", value="Elimina los últimos n mensajes (requiere permisos).", inline=False)
    embed.add_field(name="!invocador <Nombre#TAG>", value="Muestra el rango y stats de un invocador de LoL.", inline=False)
    embed.add_field(name="!vs <Nombre#TAG> <Nombre#TAG>", value="Compara dos invocadores de LoL lado a lado.", inline=False)
    embed.add_field(name="!playlist create <nombre>", value="Crea una playlist vacía.", inline=False)
    embed.add_field(name="!playlist add <nombre>", value="Agrega la canción actual a la playlist.", inline=False)
    embed.add_field(name="!playlist load <nombre>", value="Carga la playlist en la cola.", inline=False)
    embed.add_field(name="!playlist list", value="Muestra todas las playlists del servidor.", inline=False)
    embed.add_field(name="!playlist show <nombre>", value="Muestra las canciones de una playlist.", inline=False)
    embed.add_field(name="!playlist remove <nombre> <pos>", value="Saca una canción de la playlist.", inline=False)
    embed.add_field(name="!playlist delete <nombre>", value="Elimina la playlist completa.", inline=False)
    embed.add_field(name="!reiniciar", value="Reinicia el bot si se quedó bugueado.", inline=False)
    embed.add_field(name="!repo", value="Enlace al repositorio del bot.", inline=False)
    await ctx.send(embed=embed)


def build_help_embed():
    embed = discord.Embed(title=help_content.HELP_TITLE, color=discord.Color.blurple())
    for title, body in help_content.HELP_SECTIONS:
        embed.add_field(name=title, value=body, inline=False)
    embed.add_field(name="Comandos sin IA", value=", ".join(help_content.FALLBACK_COMMANDS), inline=False)
    return embed


@bot.command()
async def ayuda(ctx):
    await ctx.send(embed=build_help_embed())


@bot.command()
async def repo(ctx):
    await ctx.send("🔗 Repositorio: https://github.com/bak1-H/BOT_DISCORD_MUSICA")


@bot.command()
async def loop(ctx):
    next_mode = get_music_service(ctx.guild.id).cycle_loop()
    labels = {
        "off":   "➡️ Loop **desactivado**.",
        "song":  "🔂 Repitiendo **canción actual**.",
        "queue": "🔁 Repitiendo **cola completa**.",
    }
    await ctx.send(labels[next_mode])


@bot.command()
@commands.has_permissions(manage_messages=True)
async def clear(ctx, num: int):
    if num < 1:
        return await ctx.send("❌ Usa un número mayor a 0.")
    # Discord only allows bulk-delete for messages < 14 days old.
    # Deleting older messages one-by-one hammers the rate limit and can drop the voice session.
    cutoff = datetime.now(timezone.utc) - timedelta(days=13, hours=23)
    deleted = await ctx.channel.purge(
        limit=num + 1,
        check=lambda m: m.created_at > cutoff,
        bulk=True,
    )
    count = len(deleted) - 1  # -1 for the !clear command itself
    await ctx.send(f"🧹 Eliminados {count} mensajes.", delete_after=5)


lol_service = LolService(RiotApi(RIOT_API_KEY, RIOT_PLATFORM, RIOT_ROUTING))


@bot.command()
async def invocador(ctx, *, nombre: str = None):
    if not nombre:
        return await ctx.send("❌ Uso: `!invocador NombreJugador#TAG`")
    if "#" not in nombre:
        return await ctx.send("❌ Incluye el tag. Ejemplo: `!invocador Faker#KR1`")
    if not RIOT_API_KEY:
        return await ctx.send("❌ RIOT_API_KEY no configurada en el servidor.")

    await ctx.send(f"🔍 Buscando **{nombre}**...")
    try:
        player = await lol_service.summoner(nombre)
    except RiotLookupError as error:
        return await ctx.send(f"❌ {error}")
    await ctx.send(embed=build_summoner_embed(player, RIOT_PLATFORM))


@bot.command()
async def vs(ctx, *, nombres: str = None):
    if not nombres:
        return await ctx.send("❌ Uso: `!vs Jugador1#TAG1 Jugador2#TAG2`")
    if not RIOT_API_KEY:
        return await ctx.send("❌ RIOT_API_KEY no configurada.")

    ids = re.findall(r'\S+#\S+', nombres)
    if len(ids) < 2:
        return await ctx.send("❌ Necesito dos Riot IDs. Ejemplo: `!vs maxipepsi#CHL FatReign#KFC`")

    await ctx.send(f"⚔️ Comparando **{ids[0]}** vs **{ids[1]}**...")
    try:
        comparison = await lol_service.compare(ids[0], ids[1])
    except RiotLookupError as error:
        return await ctx.send(f"❌ {error}")
    await ctx.send(embed=build_comparison_embed(comparison))


# ──────────────────── PLAYLISTS ────────────────────

PLAYLISTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "playlists")
os.makedirs(PLAYLISTS_DIR, exist_ok=True)


@bot.group(name="playlist", aliases=["pl"], invoke_without_command=True)
async def playlist(ctx):
    await ctx.send(
        "📋 **Comandos de playlist:**\n"
        "`!playlist create <nombre>` — crea una playlist vacía\n"
        "`!playlist add <nombre>` — agrega la canción actual\n"
        "`!playlist load <nombre>` — carga la playlist en la cola\n"
        "`!playlist list` — muestra todas las playlists\n"
        "`!playlist show <nombre>` — muestra las canciones\n"
        "`!playlist remove <nombre> <posición>` — saca una canción\n"
        "`!playlist delete <nombre>` — elimina la playlist completa"
    )


@playlist.command(name="create")
async def pl_create(ctx, *, nombre: str = None):
    if not nombre:
        return await ctx.send("❌ Uso: `!playlist create <nombre>`")
    nombre = nombre.lower().strip()
    async with playlist_store.guild_lock(ctx.guild.id):
        data = playlist_store.load_playlists(PLAYLISTS_DIR, ctx.guild.id)
        if nombre in data:
            return await ctx.send(f"❌ Ya existe la playlist **{nombre}**.")
        data[nombre] = []
        playlist_store.save_playlists(PLAYLISTS_DIR, ctx.guild.id, data)
    await ctx.send(f"✅ Playlist **{nombre}** creada. Agrega canciones con `!playlist add {nombre}`.")


@playlist.command(name="add")
async def pl_add(ctx, *, nombre: str = None):
    if not nombre:
        return await ctx.send("❌ Uso: `!playlist add <nombre>`")
    nombre = nombre.lower().strip()
    song = players.get(ctx.guild.id).current
    if not song:
        return await ctx.send("❌ No hay ninguna canción sonando ahora.")
    entry = {"title": song["title"], "url": song["url"]}
    async with playlist_store.guild_lock(ctx.guild.id):
        data = playlist_store.load_playlists(PLAYLISTS_DIR, ctx.guild.id)
        if nombre not in data:
            return await ctx.send(f"❌ No existe la playlist **{nombre}**. Créala con `!playlist create {nombre}`.")
        if entry in data[nombre]:
            return await ctx.send(f"⚠️ **{song['title']}** ya está en **{nombre}**.")
        data[nombre].append(entry)
        playlist_store.save_playlists(PLAYLISTS_DIR, ctx.guild.id, data)
    await ctx.send(f"✅ **{song['title']}** agregada a **{nombre}** ({len(data[nombre])} canciones).")


@playlist.command(name="load")
async def pl_load(ctx, *, nombre: str = None):
    if not nombre:
        return await ctx.send("❌ Uso: `!playlist load <nombre>`")
    nombre = nombre.lower().strip()
    data = playlist_store.load_playlists(PLAYLISTS_DIR, ctx.guild.id)
    if nombre not in data or not data[nombre]:
        return await ctx.send(f"❌ La playlist **{nombre}** no existe o está vacía.")
    if not await connect_to_author_voice(ctx):
        return
    songs = data[nombre]
    for entry in songs:
        players.get(ctx.guild.id).enqueue(entry["url"], entry["title"])
    await ctx.send(f"📋 **{nombre}** cargada — {len(songs)} canciones añadidas a la cola.")
    await get_music_service(ctx.guild.id).ensure_playing()


@playlist.command(name="list")
async def pl_list(ctx):
    data = playlist_store.load_playlists(PLAYLISTS_DIR, ctx.guild.id)
    if not data:
        return await ctx.send("❌ No hay playlists guardadas en este servidor.")
    embed = discord.Embed(title="📋 Playlists del servidor", color=discord.Color.blurple())
    lines = [f"**{nombre}** — {len(canciones)} canciones" for nombre, canciones in data.items()]
    embed.description = "\n".join(lines)
    await ctx.send(embed=embed)


@playlist.command(name="show")
async def pl_show(ctx, *, nombre: str = None):
    if not nombre:
        return await ctx.send("❌ Uso: `!playlist show <nombre>`")
    nombre = nombre.lower().strip()
    data = playlist_store.load_playlists(PLAYLISTS_DIR, ctx.guild.id)
    if nombre not in data:
        return await ctx.send(f"❌ No existe la playlist **{nombre}**.")
    songs = data[nombre]
    if not songs:
        return await ctx.send(f"📋 **{nombre}** está vacía.")
    embed = discord.Embed(title=f"📋 {nombre} ({len(songs)} canciones)", color=discord.Color.blurple())
    lines = [f"`{i+1}.` {s['title']}" for i, s in enumerate(songs[:20])]
    if len(songs) > 20:
        lines.append(f"*...y {len(songs) - 20} más*")
    embed.description = "\n".join(lines)
    await ctx.send(embed=embed)


@playlist.command(name="remove")
async def pl_remove(ctx, nombre: str = None, posicion: int = None):
    if not nombre or posicion is None:
        return await ctx.send("❌ Uso: `!playlist remove <nombre> <posición>`")
    nombre = nombre.lower().strip()
    async with playlist_store.guild_lock(ctx.guild.id):
        data = playlist_store.load_playlists(PLAYLISTS_DIR, ctx.guild.id)
        if nombre not in data:
            return await ctx.send(f"❌ No existe la playlist **{nombre}**.")
        songs = data[nombre]
        if posicion < 1 or posicion > len(songs):
            return await ctx.send(f"❌ Posición inválida. La playlist tiene {len(songs)} canciones.")
        removed = songs.pop(posicion - 1)
        playlist_store.save_playlists(PLAYLISTS_DIR, ctx.guild.id, data)
    await ctx.send(f"🗑️ **{removed['title']}** eliminada de **{nombre}**.")


@playlist.command(name="delete")
async def pl_delete(ctx, *, nombre: str = None):
    if not nombre:
        return await ctx.send("❌ Uso: `!playlist delete <nombre>`")
    nombre = nombre.lower().strip()
    async with playlist_store.guild_lock(ctx.guild.id):
        data = playlist_store.load_playlists(PLAYLISTS_DIR, ctx.guild.id)
        if nombre not in data:
            return await ctx.send(f"❌ No existe la playlist **{nombre}**.")
        del data[nombre]
        playlist_store.save_playlists(PLAYLISTS_DIR, ctx.guild.id, data)
    await ctx.send(f"🗑️ Playlist **{nombre}** eliminada.")


@bot.command(name="reiniciar", aliases=["restart"])
async def reiniciar(ctx):
    """Reinicia el proceso del bot. systemd (Restart=always) lo vuelve a levantar."""
    await ctx.send("🔄 Reiniciando el bot... vuelvo en unos segundos.")
    for vc in list(bot.voice_clients):
        try:
            await vc.disconnect(force=True)
        except Exception:
            pass
    await bot.close()
    # Salida limpia: en el mini PC systemd lo reinicia automaticamente.
    os._exit(0)


AGENT_DISABLED_VALUES = {"false", "0", "no", "off"}


def agent_enabled(env=os.environ) -> bool:
    return env.get("AGENT_ENABLED", "true").strip().lower() not in AGENT_DISABLED_VALUES


VOICE_ENABLED_VALUES = {"true", "1", "yes", "on"}


def voice_activation_enabled(env=os.environ) -> bool:
    return env.get("VOICE_ACTIVATION_ENABLED", "false").strip().lower() in VOICE_ENABLED_VALUES


def install_voice(env=os.environ):
    if not voice_activation_enabled(env):
        return None
    try:
        from discord.ext.voice_recv import VoiceRecvClient
    except Exception as error:
        print(f"[voz] deshabilitado: {type(error).__name__}")
        return None
    print("[voz] cliente de voz con recepcion instalado")
    return VoiceRecvClient


def install_voice_activation(listener, loop, env=os.environ):
    if listener is None:
        print("[voz] escucha deshabilitada: el agente no está activo")
        return None
    try:
        from voice.activation import VoiceActivation
        from voice.audio import rms_threshold_from_env
        from voice.transcriber import create_gemini_transcriber
        from voice.wake import create_vosk_detector
        from voice.window import window_options_from_env

        import importlib.util

        detector = create_vosk_detector(env)
        if detector is None:
            raise ValueError("VOICE_VOSK_MODEL_DIR no está configurada")
        if importlib.util.find_spec("vosk") is None:
            raise ValueError("el paquete vosk no está instalado")
        activation = VoiceActivation(
            detector,
            listener,
            create_gemini_transcriber(env),
            players.get,
            loop,
            rms_threshold_from_env(env),
            window_options_from_env(env),
        )
    except Exception as error:
        print(f"[voz] escucha deshabilitada: {type(error).__name__}: {error}")
        return None
    print("[voz] escucha activa")
    return activation


def install_agent(env=os.environ):
    if not agent_enabled(env):
        print("[agente] deshabilitado por AGENT_ENABLED")
        return None
    try:
        from agent.adapters import GeniusLyrics, RunContextFactory
        from agent.listener import AgentListener
        from agent.model import build_runner

        runner = build_runner(env)
        context_factory = RunContextFactory(get_music_service, lol_service, GeniusLyrics(), lambda: PLAYLISTS_DIR)
        listener = AgentListener(runner, context_factory, bot)
    except Exception as error:
        print(f"[agente] deshabilitado: {type(error).__name__}")
        return None
    bot.add_listener(listener.on_message, "on_message")
    print("[agente] activo")
    return listener


@bot.event
async def on_voice_state_update(member, before, after):
    get_music_service(member.guild.id).refresh_alone_watch()
    if bot.voice_activation is not None:
        try:
            bot.voice_activation.voice_state_changed(member, before, after)
        except Exception as error:
            print(f"[voz] on_voice_state_update: {type(error).__name__}")


@bot.event
async def on_ready():
    print(f"[OK] {bot.user} listo.")


if __name__ == "__main__":
    bot.run(os.getenv("DISCORD_TOKEN"))
