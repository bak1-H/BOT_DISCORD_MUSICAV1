import asyncio
import copy
import glob
import os
import re
from dataclasses import dataclass

import yt_dlp

YT_CLIENTS = ["web", "android_vr", "android", "ios"]
DEFAULT_CLIENT = None
DEFAULT_CLIENT_LABEL = "default"
DOWNLOAD_ATTEMPTS = [DEFAULT_CLIENT, *YT_CLIENTS]
DOWNLOAD_SOCKET_TIMEOUT_S = 30
VIDEO_ID_PATTERN = re.compile(r"(?:[?&]v=|youtu\.be/)([A-Za-z0-9_-]+)")


@dataclass(frozen=True)
class YtdlpSettings:
    proxy: str | None = None
    cookies_file: str | None = None
    po_token: str = ""
    visitor_data: str = ""


def base_ytdlp_options(settings: YtdlpSettings) -> dict:
    return {
        "format": "bestaudio*/best*",
        "noplaylist": True,
        "nocheckcertificate": True,
        "quiet": True,
        "no_warnings": True,
        "proxy": settings.proxy,
        "js_runtimes": {"node": {}},
        "cookiefile": settings.cookies_file,
    }


def normalize_youtube_url(value: str | None) -> str | None:
    if not value:
        return None
    return value if value.startswith("http") else f"https://www.youtube.com/watch?v={value}"


def is_youtube_login_block(err: Exception) -> bool:
    s = str(err).lower()
    return any(phrase in s for phrase in (
        "sign in to confirm you're not a bot",
        "sign in to confirm",
        "bot check",
        "login required",
    ))


def build_ytdlp_opts(settings: YtdlpSettings, is_search: bool, client: str | None = "web", search_count: int = 1) -> dict:
    opts = copy.deepcopy(base_ytdlp_options(settings))
    yt_args: dict = {}
    if client is not None:
        yt_args["player_client"] = [client]
        if settings.po_token:
            yt_args["po_token"] = [f"{client}+{settings.po_token}"]
    if settings.visitor_data:
        yt_args["visitor_data"] = [settings.visitor_data]
    opts["extractor_args"] = {"youtube": yt_args}
    if is_search:
        opts["default_search"] = f"ytsearch{search_count}"
        opts["extract_flat"] = "in_playlist"
    return opts


async def ytdlp_extract(
    settings: YtdlpSettings, query: str, is_search: bool = False, client: str = "web", search_count: int = 1
) -> dict:
    loop = asyncio.get_running_loop()
    opts = build_ytdlp_opts(settings, is_search, client, search_count)

    def _extract():
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(query, download=False)

    return await loop.run_in_executor(None, _extract)


def build_download_opts(settings: YtdlpSettings, download_dir: str, gid: int, client: str | None) -> dict:
    opts = build_ytdlp_opts(settings, is_search=False, client=client)
    opts["format"] = "bestaudio[ext=webm]/bestaudio[ext=opus]/bestaudio[ext=ogg]/bestaudio/best"
    opts["outtmpl"] = os.path.join(download_dir, f"{gid}_%(id)s.%(ext)s")
    opts["socket_timeout"] = DOWNLOAD_SOCKET_TIMEOUT_S
    return opts


def discard_download_leftovers(download_dir: str, gid: int, url: str | None) -> None:
    match = VIDEO_ID_PATTERN.search(url or "")
    if not match:
        return
    pattern = os.path.join(glob.escape(download_dir), f"{gid}_{glob.escape(match.group(1))}.*")
    for path in glob.glob(pattern):
        try:
            os.remove(path)
        except OSError:
            pass


async def download_audio_with_fallback(
    settings: YtdlpSettings, download_dir: str, gid: int, url: str
) -> tuple[dict, str, str]:
    loop = asyncio.get_running_loop()
    last_error = None
    for client in DOWNLOAD_ATTEMPTS:
        opts = build_download_opts(settings, download_dir, gid, client)

        def _download():
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
                if isinstance(info, dict) and info.get("entries"):
                    info = info["entries"][0]
                return info, ydl.prepare_filename(info)

        try:
            info, path = await loop.run_in_executor(None, _download)
            if os.path.exists(path):
                return info, path, client or DEFAULT_CLIENT_LABEL
        except Exception as e:
            last_error = e
    if last_error:
        raise last_error
    raise RuntimeError("No se pudo descargar el audio con ningún client")
