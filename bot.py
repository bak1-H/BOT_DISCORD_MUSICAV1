import os
import asyncio
import re
import traceback
from datetime import datetime, timezone, timedelta
import discord
from discord.ext import commands
from dotenv import load_dotenv
import base64
import aiohttp
import ai_dj
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

    async def setup_hook(self):
        self.playback_loop = asyncio.get_running_loop()


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
            voice=DiscordVoiceGateway(bot, guild_id),
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


_RANK_EMOJI = {
    "IRON": "🔩", "BRONZE": "🥉", "SILVER": "🥈", "GOLD": "🥇",
    "PLATINUM": "🌿", "EMERALD": "💚", "DIAMOND": "💎",
    "MASTER": "👑", "GRANDMASTER": "🏆", "CHALLENGER": "🔥",
}

_RANK_COLOR = {
    "IRON": 0x4a4a4a, "BRONZE": 0xcd7f32, "SILVER": 0xa8a9ad,
    "GOLD": 0xffd700, "PLATINUM": 0x4da6a8, "EMERALD": 0x149c50,
    "DIAMOND": 0x5b85f5, "MASTER": 0x9c4dcc,
    "GRANDMASTER": 0xd45a2a, "CHALLENGER": 0xf4c874,
}

_TIER_ORDER = ["IRON","BRONZE","SILVER","GOLD","PLATINUM","EMERALD","DIAMOND","MASTER","GRANDMASTER","CHALLENGER"]


def _fmt_rank(entry: dict | None) -> str:
    if not entry:
        return "*Sin clasificar*"
    tier = entry["tier"]
    division = entry.get("rank", "")
    lp = entry["leaguePoints"]
    wins, losses = entry["wins"], entry["losses"]
    wr = round(wins / (wins + losses) * 100) if (wins + losses) else 0
    emoji = _RANK_EMOJI.get(tier, "")
    return (
        f"{emoji} **{tier.capitalize()} {division}**\n"
        f"`{lp} LP` · {wins}V / {losses}D\n"
        f"**{wr}%** winrate"
    )


def _best_tier(entries: list) -> str | None:
    tiers = [e["tier"] for e in entries if "tier" in e]
    return max(tiers, key=lambda t: _TIER_ORDER.index(t) if t in _TIER_ORDER else -1, default=None)


async def _fetch_lol_player(session: aiohttp.ClientSession, riot_id: str, headers: dict) -> dict | str:
    """Fetches account, summoner and ranked data. Returns a dict or an error string."""
    game_name, tag_line = riot_id.rsplit("#", 1)
    url = f"https://{RIOT_ROUTING}.api.riotgames.com/riot/account/v1/accounts/by-riot-id/{game_name}/{tag_line}"
    async with session.get(url, headers=headers) as r:
        if r.status == 404:
            return f"❌ `{riot_id}` no encontrado."
        if r.status in (401, 403):
            return "❌ API key inválida o expirada."
        if r.status != 200:
            return f"❌ Error Riot API ({r.status})."
        account = await r.json()

    puuid = account["puuid"]

    async with session.get(
        f"https://{RIOT_PLATFORM}.api.riotgames.com/lol/summoner/v4/summoners/by-puuid/{puuid}",
        headers=headers,
    ) as r:
        summoner = await r.json() if r.status == 200 else {}

    async with session.get(
        f"https://{RIOT_PLATFORM}.api.riotgames.com/lol/league/v4/entries/by-puuid/{puuid}",
        headers=headers,
    ) as r:
        entries = await r.json() if r.status == 200 else []

    return {
        "account": account,
        "summoner": summoner,
        "solo": next((e for e in entries if e["queueType"] == "RANKED_SOLO_5x5"), None),
        "flex": next((e for e in entries if e["queueType"] == "RANKED_FLEX_SR"), None),
        "entries": entries,
    }


@bot.command()
async def invocador(ctx, *, nombre: str = None):
    if not nombre:
        return await ctx.send("❌ Uso: `!invocador NombreJugador#TAG`")
    if "#" not in nombre:
        return await ctx.send("❌ Incluí el tag. Ejemplo: `!invocador Faker#KR1`")
    if not RIOT_API_KEY:
        return await ctx.send("❌ RIOT_API_KEY no configurada en el servidor.")

    game_name, tag_line = nombre.rsplit("#", 1)
    headers = {"X-Riot-Token": RIOT_API_KEY}

    await ctx.send(f"🔍 Buscando **{nombre}**...")

    try:
        async with aiohttp.ClientSession() as session:
            # 1. PUUID desde Riot ID
            url = (
                f"https://{RIOT_ROUTING}.api.riotgames.com"
                f"/riot/account/v1/accounts/by-riot-id/{game_name}/{tag_line}"
            )
            async with session.get(url, headers=headers) as r:
                if r.status == 404:
                    return await ctx.send(f"❌ `{nombre}` no encontrado.")
                if r.status in (401, 403):
                    return await ctx.send("❌ API key inválida o expirada.")
                if r.status != 200:
                    return await ctx.send(f"❌ Error Riot API ({r.status}).")
                account = await r.json()

            # 2. Summoner por PUUID — solo para nivel e ícono (id ya no se devuelve)
            url = (
                f"https://{RIOT_PLATFORM}.api.riotgames.com"
                f"/lol/summoner/v4/summoners/by-puuid/{account['puuid']}"
            )
            async with session.get(url, headers=headers) as r:
                summoner = await r.json() if r.status == 200 else {}

            # 3. Ranked por PUUID (endpoint nuevo, no requiere summonerId)
            url = (
                f"https://{RIOT_PLATFORM}.api.riotgames.com"
                f"/lol/league/v4/entries/by-puuid/{account['puuid']}"
            )
            async with session.get(url, headers=headers) as r:
                if r.status != 200:
                    return await ctx.send(f"❌ Error obteniendo ranked ({r.status}).")
                entries = await r.json()

    except aiohttp.ClientError as e:
        print(f"Riot API error: {e}")
        return await ctx.send("❌ Error de red al consultar la API de Riot.")

    solo = next((e for e in entries if e["queueType"] == "RANKED_SOLO_5x5"), None)
    flex = next((e for e in entries if e["queueType"] == "RANKED_FLEX_SR"), None)
    icon_id = summoner.get("profileIconId", 0)
    level = summoner.get("summonerLevel", "?")

    best = _best_tier(entries)
    color = _RANK_COLOR.get(best, 0x5865f2) if best else 0x5865f2

    embed = discord.Embed(
        title=f"{account['gameName']}#{account['tagLine']}",
        description=f"Nivel **{level}** · {RIOT_PLATFORM.upper()}",
        color=color,
    )
    embed.add_field(name="🎯 Solo/Duo", value=_fmt_rank(solo), inline=True)
    embed.add_field(name="👥 Flex 5v5", value=_fmt_rank(flex), inline=True)
    embed.set_thumbnail(
        url=f"https://ddragon.leagueoflegends.com/cdn/15.1.1/img/profileicon/{icon_id}.png"
    )
    embed.set_footer(
        text="League of Legends · Riot Games API",
        icon_url="https://cdn.communitydragon.org/latest/asset/ASSETS/Riot_Games/Logos/LoL_Icon_RGB.png",
    )
    await ctx.send(embed=embed)


def _rank_score(entry: dict | None) -> float:
    if not entry:
        return -1.0
    tier_idx = _TIER_ORDER.index(entry["tier"]) if entry["tier"] in _TIER_ORDER else 0
    div_bonus = {"I": 3, "II": 2, "III": 1, "IV": 0}.get(entry.get("rank", "IV"), 0)
    return tier_idx * 4 + div_bonus + entry["leaguePoints"] / 100


def _wr(entry: dict | None) -> float:
    if not entry:
        return -1.0
    w, l = entry["wins"], entry["losses"]
    return round(w / (w + l) * 100, 1) if (w + l) else 0.0


def _cmp(a, b) -> tuple[str, str]:
    """Returns (indicator_a, indicator_b). 🟢 = wins, 🔴 = loses, ⚪ = tie."""
    if a > b:   return "🟢", "🔴"
    if b > a:   return "🔴", "🟢"
    return "⚪", "⚪"


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

    headers = {"X-Riot-Token": RIOT_API_KEY}
    try:
        async with aiohttp.ClientSession() as session:
            p1, p2 = await asyncio.gather(
                _fetch_lol_player(session, ids[0], headers),
                _fetch_lol_player(session, ids[1], headers),
            )
    except aiohttp.ClientError as e:
        print(f"Riot VS error: {e}")
        return await ctx.send("❌ Error de red al consultar la API de Riot.")

    if isinstance(p1, str):
        return await ctx.send(p1)
    if isinstance(p2, str):
        return await ctx.send(p2)

    name1 = f"{p1['account']['gameName']}#{p1['account']['tagLine']}"
    name2 = f"{p2['account']['gameName']}#{p2['account']['tagLine']}"

    # ── comparación por categoría ──
    solo_r1, solo_r2 = _cmp(_rank_score(p1["solo"]), _rank_score(p2["solo"]))
    solo_w1, solo_w2 = _cmp(_wr(p1["solo"]), _wr(p2["solo"]))
    flex_r1, flex_r2 = _cmp(_rank_score(p1["flex"]), _rank_score(p2["flex"]))
    flex_w1, flex_w2 = _cmp(_wr(p1["flex"]), _wr(p2["flex"]))

    pts1 = sum(2 if i == "🟢" else (1 if i == "⚪" else 0) for i in [solo_r1, solo_w1, flex_r1, flex_w1])
    pts2 = sum(2 if i == "🟢" else (1 if i == "⚪" else 0) for i in [solo_r2, solo_w2, flex_r2, flex_w2])

    def player_field(p, sr, sw, fr, fw) -> str:
        lvl   = p["summoner"].get("summonerLevel", "?")
        s     = p["solo"]
        f     = p["flex"]
        s_wr  = f"{_wr(s):.0f}%" if s else "—"
        f_wr  = f"{_wr(f):.0f}%" if f else "—"
        s_lbl = f"{_RANK_EMOJI.get(s['tier'],'')} {s['tier'].capitalize()} {s.get('rank','')} · {s['leaguePoints']} LP" if s else "Sin clasificar"
        f_lbl = f"{_RANK_EMOJI.get(f['tier'],'')} {f['tier'].capitalize()} {f.get('rank','')} · {f['leaguePoints']} LP" if f else "Sin clasificar"
        return (
            f"Nivel **{lvl}**\n\n"
            f"🎯 **Solo/Duo**\n{sr} {s_lbl}\n{sw} WR: **{s_wr}**\n\n"
            f"👥 **Flex**\n{fr} {f_lbl}\n{fw} WR: **{f_wr}**"
        )

    if pts1 > pts2:
        verdict = f"🏆 **{name1}** es superior ({pts1//2}-{pts2//2})"
        winner_tier = _best_tier(p1["entries"])
    elif pts2 > pts1:
        verdict = f"🏆 **{name2}** es superior ({pts2//2}-{pts1//2})"
        winner_tier = _best_tier(p2["entries"])
    else:
        verdict = "🤝 **Empate** — estadísticas muy parejas"
        winner_tier = _best_tier(p1["entries"] + p2["entries"])

    color = _RANK_COLOR.get(winner_tier, 0x5865f2) if winner_tier else 0x5865f2

    embed = discord.Embed(title=f"⚔️ {name1}  vs  {name2}", color=color)
    embed.add_field(name=name1, value=player_field(p1, solo_r1, solo_w1, flex_r1, flex_w1), inline=True)
    embed.add_field(name=name2, value=player_field(p2, solo_r2, solo_w2, flex_r2, flex_w2), inline=True)
    embed.add_field(name="Veredicto", value=verdict, inline=False)
    embed.set_footer(
        text="League of Legends · Riot Games API",
        icon_url="https://cdn.communitydragon.org/latest/asset/ASSETS/Riot_Games/Logos/LoL_Icon_RGB.png",
    )
    await ctx.send(embed=embed)


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
    data = playlist_store.load_playlists(PLAYLISTS_DIR, ctx.guild.id)
    if nombre not in data:
        return await ctx.send(f"❌ No existe la playlist **{nombre}**. Créala con `!playlist create {nombre}`.")
    entry = {"title": song["title"], "url": song["url"]}
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


@bot.event
async def on_voice_state_update(member, before, after):
    get_music_service(member.guild.id).refresh_alone_watch()


@bot.event
async def on_ready():
    print(f"[OK] {bot.user} listo.")


if __name__ == "__main__":
    bot.run(os.getenv("DISCORD_TOKEN"))
