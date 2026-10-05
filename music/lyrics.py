import os
import re

import lyricsgenius

_genius_client = None


def get_genius():
    global _genius_client
    if _genius_client is None:
        _genius_client = lyricsgenius.Genius(
            os.getenv("GENIUS_TOKEN"),
            skip_non_songs=True,
            remove_section_headers=True,
        )
    return _genius_client


def clean_title_for_lyrics(title: str) -> str:
    if not title:
        return ""
    title = title.lower()
    for p in [
        r"\(.*?\)", r"\[.*?\]", r"official video", r"official audio",
        r"lyrics?", r"audio", r"video", r"hd", r"4k",
        r"remastered?", r"feat\.?.*", r"ft\.?.*", r"- topic", r"•.*",
    ]:
        title = re.sub(p, "", title)
    title = re.sub(r"[^\w\s\-]", "", title)
    return re.sub(r"\s{2,}", " ", title).strip()
