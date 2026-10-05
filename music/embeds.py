import discord


def format_duration(seconds) -> str:
    if not seconds:
        return "?"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def make_song_embed(song: dict, in_queue: bool = False) -> discord.Embed:
    if in_queue:
        embed = discord.Embed(
            title="✅ Añadido a la cola",
            description=f"**{song['title']}**",
            color=discord.Color.blue(),
        )
    else:
        embed = discord.Embed(
            title="🎵 Reproduciendo ahora",
            description=f"**{song['title']}**",
            color=discord.Color.green(),
        )
    if song.get("uploader"):
        embed.add_field(name="Canal", value=song["uploader"], inline=True)
    if song.get("duration"):
        embed.add_field(name="Duración", value=format_duration(song["duration"]), inline=True)
    if song.get("thumbnail"):
        embed.set_thumbnail(url=song["thumbnail"])
    return embed
