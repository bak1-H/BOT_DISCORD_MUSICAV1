from music import ytdl


class YtdlpExtractor:
    def __init__(self, settings: ytdl.YtdlpSettings, download_dir: str) -> None:
        self._settings = settings
        self._download_dir = download_dir

    async def search(self, query: str, count: int) -> list[dict]:
        info = await ytdl.ytdlp_extract(self._settings, query, True, "web", count)
        entries = info.get("entries") if isinstance(info, dict) else None
        return list(entries) if entries else []

    async def download(self, guild_id: int, url: str) -> tuple[dict, str, str]:
        return await ytdl.download_audio_with_fallback(self._settings, self._download_dir, guild_id, url)

    def discard_download(self, guild_id: int, url: str) -> None:
        ytdl.discard_download_leftovers(self._download_dir, guild_id, url)
