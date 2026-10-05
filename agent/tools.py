import re
import traceback
import unicodedata
from dataclasses import dataclass
from typing import Annotated, Awaitable, Callable, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError, field_validator

from agent.context import (
    MAX_DESTRUCTIVE_PER_TURN,
    MAX_SONGS_PER_TURN,
    MAX_TOOL_CALLS_PER_TURN,
    PendingAction,
    RunContext,
)
from lol.riot import RiotLookupError
from lol.service import Comparison, rank_summary
from music.lyrics import clean_title_for_lyrics
from music.ports import ConnectResult
from music.service import RemoveStatus

MAX_TEXT_ARG = 200
MAX_TITLE_CHARS = 100
MAX_QUEUE_LINES = 10
MAX_PLAYLIST_LINES = 20
MAX_LYRICS_CHARS = 1500
MAX_SONGS_PER_CALL = 15
MIN_DURATION_S = 30
MAX_DURATION_S = 1800
DEFAULT_MAX_DURATION_S = 600
MAX_ERROR_CHARS = 300

IGNORED_ID_FIELDS = frozenset({"guild_id", "user_id", "channel_id", "author_id", "voice_channel_id", "member_id"})
SCHEMA_KEYS_TO_DROP = frozenset({"title", "additionalProperties", "default"})

NOT_IN_VOICE = "Entra a un canal de voz primero."
NO_PLAYLIST_STORE = "Las playlists no están disponibles en este momento."
NO_LOL_SERVICE = "La consulta de LoL no está disponible en este momento."
RIOT_ID_PATTERN = re.compile(r"^.{3,16}#.{2,5}$")
CONNECT_FAILURES = {
    ConnectResult.TIMEOUT: "No pude conectarme al canal de voz (timeout).",
    ConnectResult.REFUSED: "No pude conectarme al canal de voz (permisos o capacidad).",
}

ShortText = Annotated[str, StringConstraints(min_length=1, max_length=MAX_TEXT_ARG)]


def clean_text(value, limit: int = MAX_TITLE_CHARS) -> str:
    text = "".join(" " if unicodedata.category(char) in ("Cc", "Cf", "Zl", "Zp") else char for char in str(value))
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def quoted(title) -> str:
    return f"«{clean_text(title)}»"


def normalize_name(name: str) -> str:
    return name.lower().strip()


@dataclass(frozen=True)
class ToolOutcome:
    text: str
    ok: bool = True
    pending: PendingAction | None = None


def failure(text: str) -> ToolOutcome:
    return ToolOutcome(text, ok=False)


class ToolArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NoArgs(ToolArgs):
    pass


class QueryArgs(ToolArgs):
    query: ShortText = Field(description="Nombre de la canción o artista a buscar")


class SongArgs(ToolArgs):
    song: str = Field(default="", max_length=MAX_TEXT_ARG, description="Título de la canción; vacío usa la actual")


class NameArgs(ToolArgs):
    name: ShortText = Field(description="Nombre de la playlist")


class QueueSongsArgs(ToolArgs):
    songs: list[ShortText] = Field(
        min_length=1, max_length=MAX_SONGS_PER_CALL, description="Búsquedas de canciones, una por elemento"
    )
    max_duration_s: int = Field(
        default=DEFAULT_MAX_DURATION_S,
        ge=MIN_DURATION_S,
        le=MAX_DURATION_S,
        description="Duración máxima por canción en segundos",
    )


class LoopArgs(ToolArgs):
    mode: Literal["off", "song", "queue"] = Field(description="off, song (repetir canción) o queue (repetir cola)")


class RadioArgs(ToolArgs):
    theme: ShortText = Field(description="Estilo o artista de la radio")


class RemoveArgs(ToolArgs):
    position: int = Field(ge=1, description="Posición en la cola, empezando en 1")
    expected_title: str = Field(default="", max_length=MAX_TEXT_ARG, description="Título esperado en esa posición")


class PlaylistRemoveArgs(ToolArgs):
    name: ShortText = Field(description="Nombre de la playlist")
    position: int = Field(ge=1, description="Posición en la playlist, empezando en 1")


def validate_riot_id(value: str) -> str:
    if not RIOT_ID_PATTERN.match(value):
        raise ValueError("el Riot ID debe tener la forma Nombre#TAG")
    return value


class RiotIdArgs(ToolArgs):
    riot_id: ShortText = Field(description="Riot ID con la forma Nombre#TAG, por ejemplo Faker#KR1")

    _check_riot_id = field_validator("riot_id")(validate_riot_id)


class CompareArgs(ToolArgs):
    riot_id_a: ShortText = Field(description="Primer Riot ID con la forma Nombre#TAG")
    riot_id_b: ShortText = Field(description="Segundo Riot ID con la forma Nombre#TAG")

    _check_riot_id_a = field_validator("riot_id_a")(validate_riot_id)
    _check_riot_id_b = field_validator("riot_id_b")(validate_riot_id)


Handler = Callable[[RunContext, ToolArgs], Awaitable[ToolOutcome]]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    args: type[ToolArgs]
    handler: Handler
    destructive: bool = False
    read_only: bool = False


async def ensure_voice(ctx: RunContext) -> str | None:
    if ctx.voice_channel is None:
        return NOT_IN_VOICE
    result = await ctx.music.connect(ctx.voice_channel, ctx.text_channel)
    return CONNECT_FAILURES.get(result)


def format_queue(ctx: RunContext) -> str:
    player = ctx.music.player
    lines = []
    if player.current:
        lines.append(f"Reproduciendo ahora: {quoted(player.current['title'])}")
    queue = player.queue
    if queue:
        lines.append(f"Cola ({len(queue)}):")
        lines.extend(f"{index}. {clean_text(track.title)}" for index, track in enumerate(queue[:MAX_QUEUE_LINES], 1))
        if len(queue) > MAX_QUEUE_LINES:
            lines.append(f"...y {len(queue) - MAX_QUEUE_LINES} más")
    elif not player.current:
        lines.append("La cola está vacía.")
    lines.append(f"Loop: {player.loop_mode}.")
    if player.radio.query:
        lines.append(f"Radio activa: {clean_text(player.radio.query)}.")
    return "\n".join(lines)


async def get_queue(ctx, args):
    return ToolOutcome(format_queue(ctx))


async def now_playing(ctx, args):
    song = ctx.music.player.current
    if not song:
        return ToolOutcome("Nada se está reproduciendo ahora.")
    return ToolOutcome(f"Reproduciendo ahora: {quoted(song['title'])}")


async def get_lyrics(ctx, args):
    if ctx.lyrics is None:
        return failure("Las letras no están disponibles en este momento.")
    song = args.song.strip()
    if not song and ctx.music.player.current:
        song = ctx.music.player.current["title"]
    if not song:
        return failure("Dime el nombre de la canción o reproduce una primero.")
    try:
        result = await ctx.lyrics.search(clean_title_for_lyrics(song))
    except Exception:
        traceback.print_exc()
        return failure("No pude obtener la letra.")
    if result is None or not result.text:
        return failure("No encontré la letra de esa canción.")
    text = result.text
    if len(text) > MAX_LYRICS_CHARS:
        text = text[: MAX_LYRICS_CHARS - 3] + "..."
    return ToolOutcome(f"Letra de {quoted(result.title)} de {clean_text(result.artist)}:\n{text}")


async def list_playlists(ctx, args):
    if ctx.playlists is None:
        return failure(NO_PLAYLIST_STORE)
    data = ctx.playlists.load()
    if not data:
        return failure("No hay playlists guardadas en este servidor.")
    lines = [f"{clean_text(name)}: {len(songs)} canciones" for name, songs in data.items()]
    return ToolOutcome("\n".join(lines))


async def show_playlist(ctx, args):
    if ctx.playlists is None:
        return failure(NO_PLAYLIST_STORE)
    name = normalize_name(args.name)
    data = ctx.playlists.load()
    if name not in data:
        return failure(f"No existe la playlist {quoted(name)}.")
    songs = data[name]
    if not songs:
        return ToolOutcome(f"La playlist {quoted(name)} está vacía.")
    lines = [f"{index}. {clean_text(song['title'])}" for index, song in enumerate(songs[:MAX_PLAYLIST_LINES], 1)]
    if len(songs) > MAX_PLAYLIST_LINES:
        lines.append(f"...y {len(songs) - MAX_PLAYLIST_LINES} más")
    return ToolOutcome(f"Playlist {quoted(name)} ({len(songs)} canciones):\n" + "\n".join(lines))


async def play_song(ctx, args):
    problem = await ensure_voice(ctx)
    if problem:
        return failure(problem)
    music = ctx.music
    result = await music.search_and_enqueue(args.query)
    if result is None:
        return failure("No encontré resultados para esa búsqueda.")
    title = quoted(result.track.title)
    position = music.player.queue.index(result.track) + 1 if result.track in music.player.queue else None
    await music.ensure_playing()
    if result.busy and position is not None:
        return ToolOutcome(f"Añadida a la cola (#{position}): {title}")
    return ToolOutcome(f"Reproduciendo: {title}")


async def play_next(ctx, args):
    problem = await ensure_voice(ctx)
    if problem:
        return failure(problem)
    result = await ctx.music.enqueue_next(args.query)
    if result is None:
        return failure("No encontré resultados para esa búsqueda.")
    title = quoted(result.track.title)
    if result.busy:
        return ToolOutcome(f"Sonará después de la actual: {title}")
    return ToolOutcome(f"Reproduciendo: {title}")


async def queue_songs(ctx, args):
    problem = await ensure_voice(ctx)
    if problem:
        return failure(problem)
    ledger = ctx.ledger
    budget = max(MAX_SONGS_PER_TURN - ledger.songs_queued, 0)
    accepted = args.songs[:budget]
    over_budget = len(args.songs) - len(accepted)
    if not accepted:
        return failure("Ya alcancé el máximo de canciones por pedido.")
    result = await ctx.music.queue_songs(accepted, args.max_duration_s)
    ledger.songs_queued += len(accepted) - result.skipped_over_limit
    parts = [f"Encolé {len(result.queued)} canciones."]
    omitted = result.skipped_over_limit + over_budget
    if omitted:
        parts.append(f"Omití {omitted} por el límite de canciones por pedido.")
    if result.not_found:
        parts.append("No encontré: " + ", ".join(clean_text(query) for query in result.not_found) + ".")
    if result.too_long:
        parts.append("Descarté por duración: " + ", ".join(clean_text(title) for title in result.too_long) + ".")
    return ToolOutcome(" ".join(parts), ok=bool(result.queued))


async def skip(ctx, args):
    if ctx.music.skip():
        return ToolOutcome("Canción saltada.")
    return failure("No hay nada reproduciéndose.")


async def pause(ctx, args):
    if ctx.music.pause():
        return ToolOutcome("Pausado.")
    return failure("No hay nada reproduciéndose.")


async def resume(ctx, args):
    if ctx.music.resume():
        return ToolOutcome("Reanudado.")
    return failure("No hay nada pausado.")


async def set_loop(ctx, args):
    ctx.music.player.loop_mode = args.mode
    return ToolOutcome(f"Loop en modo {args.mode}.")


async def start_radio(ctx, args):
    problem = await ensure_voice(ctx)
    if problem:
        return failure(problem)
    music = ctx.music
    music.start_radio(args.theme)
    if await music.radio_next():
        await music.ensure_playing()
        return ToolOutcome(f"Radio activada: {clean_text(args.theme)}.")
    music.stop_radio()
    return failure("No encontré canciones para ese estilo.")


async def stop_radio(ctx, args):
    ctx.music.stop_radio()
    return ToolOutcome("Radio desactivada.")


async def playlist_create(ctx, args):
    if ctx.playlists is None:
        return failure(NO_PLAYLIST_STORE)
    name = normalize_name(args.name)
    data = ctx.playlists.load()
    if name in data:
        return failure(f"Ya existe la playlist {quoted(name)}.")
    data[name] = []
    ctx.playlists.save(data)
    return ToolOutcome(f"Playlist {quoted(name)} creada.")


async def playlist_add(ctx, args):
    if ctx.playlists is None:
        return failure(NO_PLAYLIST_STORE)
    name = normalize_name(args.name)
    song = ctx.music.player.current
    if not song:
        return failure("No hay ninguna canción sonando ahora.")
    data = ctx.playlists.load()
    if name not in data:
        return failure(f"No existe la playlist {quoted(name)}.")
    entry = {"title": song["title"], "url": song["url"]}
    if entry in data[name]:
        return failure(f"{quoted(song['title'])} ya está en {quoted(name)}.")
    data[name].append(entry)
    ctx.playlists.save(data)
    return ToolOutcome(f"{quoted(song['title'])} agregada a {quoted(name)} ({len(data[name])} canciones).")


async def playlist_load(ctx, args):
    if ctx.playlists is None:
        return failure(NO_PLAYLIST_STORE)
    name = normalize_name(args.name)
    songs = ctx.playlists.load().get(name)
    if not songs:
        return failure(f"La playlist {quoted(name)} no existe o está vacía.")
    problem = await ensure_voice(ctx)
    if problem:
        return failure(problem)
    for entry in songs:
        ctx.music.player.enqueue(entry["url"], entry["title"])
    await ctx.music.ensure_playing()
    return ToolOutcome(f"Playlist {quoted(name)} cargada: {len(songs)} canciones.")


def describe_player(player) -> str:
    level = player.level if player.level is not None else "desconocido"
    return (
        f"{clean_text(player.riot_id)}: nivel {level}. "
        f"Solo/Duo: {clean_text(rank_summary(player.solo))}. "
        f"Flex: {clean_text(rank_summary(player.flex))}."
    )


def describe_comparison(comparison: Comparison) -> str:
    first, second = clean_text(comparison.first.riot_id), clean_text(comparison.second.riot_id)
    if comparison.winner is None:
        verdict = "Empate: estadísticas muy parejas."
    elif comparison.winner is comparison.first:
        verdict = f"{first} es superior ({comparison.points_first // 2}-{comparison.points_second // 2})."
    else:
        verdict = f"{second} es superior ({comparison.points_second // 2}-{comparison.points_first // 2})."
    return "\n".join([describe_player(comparison.first), describe_player(comparison.second), verdict])


async def lol_summoner(ctx, args):
    if ctx.lol is None:
        return failure(NO_LOL_SERVICE)
    try:
        player = await ctx.lol.summoner(args.riot_id)
    except RiotLookupError as error:
        return failure(clean_text(error, MAX_ERROR_CHARS))
    return ToolOutcome(describe_player(player))


async def lol_compare(ctx, args):
    if ctx.lol is None:
        return failure(NO_LOL_SERVICE)
    try:
        comparison = await ctx.lol.compare(args.riot_id_a, args.riot_id_b)
    except RiotLookupError as error:
        return failure(clean_text(error, MAX_ERROR_CHARS))
    return ToolOutcome(describe_comparison(comparison))


def propose(kind: str, prompt: str, **payload) -> ToolOutcome:
    action = PendingAction(kind=kind, prompt=prompt, payload=payload)
    return ToolOutcome(
        f"Pendiente de confirmación del usuario, todavía no se ejecutó: {prompt}",
        pending=action,
    )


async def stop(ctx, args):
    return propose("stop", "¿Seguro que quieres detener la reproducción y vaciar la cola?")


async def remove_from_queue(ctx, args):
    queue = ctx.music.player.queue
    if args.position > len(queue):
        return failure(f"La cola tiene {len(queue)} canciones; no existe la posición {args.position}.")
    track = queue[args.position - 1]
    if args.expected_title and clean_text(track.title) != clean_text(args.expected_title):
        return failure(f"La cola cambió: en la posición {args.position} está {quoted(track.title)}.")
    return propose(
        "remove_entry",
        f"¿Seguro que quieres quitar {quoted(track.title)} (#{args.position}) de la cola?",
        entry_id=track.entry_id,
        title=track.title,
    )


async def clear_queue(ctx, args):
    count = len(ctx.music.player.queue)
    if not count:
        return failure("La cola ya está vacía.")
    return propose("clear_queue", f"¿Seguro que quieres vaciar la cola ({count} canciones)?")


async def playlist_remove(ctx, args):
    if ctx.playlists is None:
        return failure(NO_PLAYLIST_STORE)
    name = normalize_name(args.name)
    songs = ctx.playlists.load().get(name)
    if songs is None:
        return failure(f"No existe la playlist {quoted(name)}.")
    if args.position > len(songs):
        return failure(f"La playlist tiene {len(songs)} canciones; no existe la posición {args.position}.")
    title = songs[args.position - 1]["title"]
    return propose(
        "playlist_remove",
        f"¿Seguro que quieres quitar {quoted(title)} (#{args.position}) de la playlist {quoted(name)}?",
        name=name,
        position=args.position,
        title=title,
    )


async def playlist_delete(ctx, args):
    if ctx.playlists is None:
        return failure(NO_PLAYLIST_STORE)
    name = normalize_name(args.name)
    songs = ctx.playlists.load().get(name)
    if songs is None:
        return failure(f"No existe la playlist {quoted(name)}.")
    return propose(
        "playlist_delete",
        f"¿Seguro que quieres borrar la playlist {quoted(name)} ({len(songs)} canciones)?",
        name=name,
    )


async def _execute_stop(ctx, payload):
    await ctx.music.stop()
    return "Reproducción detenida."


async def _execute_remove_entry(ctx, payload):
    result = ctx.music.remove_entry(payload["entry_id"])
    if result.status is RemoveStatus.REMOVED:
        return f"Quité {quoted(result.track.title)} de la cola."
    return "Esa canción ya no está en la cola."


async def _execute_clear_queue(ctx, payload):
    removed = ctx.music.clear_queue()
    return f"Vacié la cola ({removed} canciones)."


async def _execute_playlist_remove(ctx, payload):
    if ctx.playlists is None:
        return NO_PLAYLIST_STORE
    data = ctx.playlists.load()
    songs = data.get(payload["name"], [])
    position = payload["position"]
    if position > len(songs) or songs[position - 1]["title"] != payload["title"]:
        return "La playlist cambió; no quité nada."
    removed = songs.pop(position - 1)
    ctx.playlists.save(data)
    return f"Quité {quoted(removed['title'])} de {quoted(payload['name'])}."


async def _execute_playlist_delete(ctx, payload):
    if ctx.playlists is None:
        return NO_PLAYLIST_STORE
    data = ctx.playlists.load()
    if payload["name"] not in data:
        return f"No existe la playlist {quoted(payload['name'])}."
    del data[payload["name"]]
    ctx.playlists.save(data)
    return f"Playlist {quoted(payload['name'])} eliminada."


PENDING_EXECUTORS = {
    "stop": _execute_stop,
    "remove_entry": _execute_remove_entry,
    "clear_queue": _execute_clear_queue,
    "playlist_remove": _execute_playlist_remove,
    "playlist_delete": _execute_playlist_delete,
}


async def execute_pending(ctx: RunContext, action: PendingAction) -> str:
    executor = PENDING_EXECUTORS.get(action.kind)
    if executor is None:
        return "No reconozco esa acción."
    return await executor(ctx, action.payload)


def clean_schema(node):
    if isinstance(node, dict):
        return {key: clean_schema(value) for key, value in node.items() if key not in SCHEMA_KEYS_TO_DROP}
    if isinstance(node, list):
        return [clean_schema(item) for item in node]
    return node


def summarize_validation_error(error: ValidationError) -> str:
    details = "; ".join(
        f"{'.'.join(str(part) for part in item['loc']) or 'args'}: {item['msg']}" for item in error.errors()
    )
    return f"Argumentos inválidos ({details})"[:MAX_ERROR_CHARS]


class ToolRegistry:
    def __init__(self, specs: list) -> None:
        self._specs = {spec.name: spec for spec in specs}

    def names(self) -> list:
        return list(self._specs)

    def is_destructive(self, name: str) -> bool:
        return self._specs[name].destructive

    def schemas(self) -> list:
        return [
            {
                "name": spec.name,
                "description": spec.description,
                "parameters": clean_schema(spec.args.model_json_schema()),
            }
            for spec in self._specs.values()
        ]

    async def execute(self, ctx: RunContext, name: str, raw_args) -> ToolOutcome:
        ledger = ctx.ledger
        if ledger.tool_calls >= MAX_TOOL_CALLS_PER_TURN:
            ledger.limit_hit = True
            return failure("Se alcanzó el máximo de llamadas a herramientas en este turno.")
        ledger.tool_calls += 1

        spec = self._specs.get(name)
        if spec is None:
            return failure(f"Herramienta desconocida: {clean_text(name, 50)}.")

        arguments = raw_args if isinstance(raw_args, dict) else {}
        cleaned = {key: value for key, value in arguments.items() if key not in IGNORED_ID_FIELDS}
        try:
            args = spec.args.model_validate(cleaned)
        except ValidationError as error:
            return failure(summarize_validation_error(error))

        if spec.destructive and ledger.destructive >= MAX_DESTRUCTIVE_PER_TURN:
            return failure("Hay demasiadas acciones destructivas en un solo pedido; divídelo en partes.")

        try:
            outcome = await spec.handler(ctx, args)
        except Exception as error:
            traceback.print_exc()
            return failure(f"La herramienta falló: {clean_text(error, 120)}")

        if outcome.pending is not None:
            ledger.destructive += 1
            ledger.pending.append(outcome.pending)
        elif outcome.ok and not spec.read_only:
            ledger.executed.append(outcome.text.splitlines()[0])
        return outcome


def build_registry() -> ToolRegistry:
    specs = [
        ToolSpec("get_queue", "Muestra la canción actual y la cola con posiciones desde 1.", NoArgs, get_queue, read_only=True),
        ToolSpec("now_playing", "Dice qué canción suena ahora.", NoArgs, now_playing, read_only=True),
        ToolSpec("get_lyrics", "Busca la letra de una canción o de la actual.", SongArgs, get_lyrics, read_only=True),
        ToolSpec("list_playlists", "Lista las playlists del servidor.", NoArgs, list_playlists, read_only=True),
        ToolSpec("show_playlist", "Muestra las canciones de una playlist.", NameArgs, show_playlist, read_only=True),
        ToolSpec("lol_summoner", "Consulta nivel y rango de League of Legends de un invocador por Riot ID (Nombre#TAG).", RiotIdArgs, lol_summoner, read_only=True),
        ToolSpec("lol_compare", "Compara el rango de dos invocadores de League of Legends por Riot ID (Nombre#TAG).", CompareArgs, lol_compare, read_only=True),
        ToolSpec("play_song", "Busca una canción y la reproduce o la agrega al final de la cola.", QueryArgs, play_song),
        ToolSpec("play_next", "Busca una canción y la pone justo después de la actual (posición 1 de la cola).", QueryArgs, play_next),
        ToolSpec("queue_songs", "Agrega varias canciones a la cola (máximo 15 por llamada).", QueueSongsArgs, queue_songs),
        ToolSpec("skip", "Salta la canción actual.", NoArgs, skip),
        ToolSpec("pause", "Pausa la reproducción.", NoArgs, pause),
        ToolSpec("resume", "Reanuda la reproducción pausada.", NoArgs, resume),
        ToolSpec("set_loop", "Cambia el modo de repetición.", LoopArgs, set_loop),
        ToolSpec("start_radio", "Activa una radio automática de un estilo o artista.", RadioArgs, start_radio),
        ToolSpec("stop_radio", "Desactiva la radio automática.", NoArgs, stop_radio),
        ToolSpec("playlist_create", "Crea una playlist vacía.", NameArgs, playlist_create),
        ToolSpec("playlist_add", "Agrega la canción actual a una playlist.", NameArgs, playlist_add),
        ToolSpec("playlist_load", "Carga una playlist en la cola.", NameArgs, playlist_load),
        ToolSpec(
            "remove_from_queue",
            "Propone quitar una canción de la cola por posición (desde 1). Requiere confirmación del usuario.",
            RemoveArgs,
            remove_from_queue,
            destructive=True,
        ),
        ToolSpec("clear_queue", "Propone vaciar la cola. Requiere confirmación del usuario.", NoArgs, clear_queue, destructive=True),
        ToolSpec(
            "playlist_remove",
            "Propone quitar una canción de una playlist. Requiere confirmación del usuario.",
            PlaylistRemoveArgs,
            playlist_remove,
            destructive=True,
        ),
        ToolSpec("playlist_delete", "Propone borrar una playlist. Requiere confirmación del usuario.", NameArgs, playlist_delete, destructive=True),
        ToolSpec("stop", "Propone detener todo y desconectar el bot. Requiere confirmación del usuario.", NoArgs, stop, destructive=True),
    ]
    return ToolRegistry(specs)
