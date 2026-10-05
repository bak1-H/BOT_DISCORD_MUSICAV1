import discord

from lol.riot import LolPlayer
from lol.service import UNRANKED, Comparison, best_tier, winrate_percent

DEFAULT_COLOR = 0x5865F2
FOOTER_TEXT = "League of Legends · Riot Games API"
FOOTER_ICON = "https://cdn.communitydragon.org/latest/asset/ASSETS/Riot_Games/Logos/LoL_Icon_RGB.png"
PROFILE_ICON_URL = "https://ddragon.leagueoflegends.com/cdn/15.1.1/img/profileicon/{icon_id}.png"

RANK_EMOJI = {
    "IRON": "🔩", "BRONZE": "🥉", "SILVER": "🥈", "GOLD": "🥇",
    "PLATINUM": "🌿", "EMERALD": "💚", "DIAMOND": "💎",
    "MASTER": "👑", "GRANDMASTER": "🏆", "CHALLENGER": "🔥",
}

RANK_COLOR = {
    "IRON": 0x4A4A4A, "BRONZE": 0xCD7F32, "SILVER": 0xA8A9AD,
    "GOLD": 0xFFD700, "PLATINUM": 0x4DA6A8, "EMERALD": 0x149C50,
    "DIAMOND": 0x5B85F5, "MASTER": 0x9C4DCC,
    "GRANDMASTER": 0xD45A2A, "CHALLENGER": 0xF4C874,
}


def color_for(tier: str | None) -> int:
    return RANK_COLOR.get(tier, DEFAULT_COLOR) if tier else DEFAULT_COLOR


def format_rank(entry: dict | None) -> str:
    if not entry:
        return f"*{UNRANKED}*"
    tier = entry["tier"]
    wins, losses = entry["wins"], entry["losses"]
    return (
        f"{RANK_EMOJI.get(tier, '')} **{tier.capitalize()} {entry.get('rank', '')}**\n"
        f"`{entry['leaguePoints']} LP` · {wins}V / {losses}D\n"
        f"**{round(winrate_percent(entry))}%** winrate"
    )


def short_rank(entry: dict | None) -> str:
    if not entry:
        return UNRANKED
    tier = entry["tier"]
    return f"{RANK_EMOJI.get(tier, '')} {tier.capitalize()} {entry.get('rank', '')} · {entry['leaguePoints']} LP"


def add_footer(embed: discord.Embed) -> discord.Embed:
    embed.set_footer(text=FOOTER_TEXT, icon_url=FOOTER_ICON)
    return embed


def build_summoner_embed(player: LolPlayer, platform: str) -> discord.Embed:
    level = player.level if player.level is not None else "?"
    embed = discord.Embed(
        title=player.riot_id,
        description=f"Nivel **{level}** · {platform.upper()}",
        color=color_for(best_tier(player.entries)),
    )
    embed.add_field(name="🎯 Solo/Duo", value=format_rank(player.solo), inline=True)
    embed.add_field(name="👥 Flex 5v5", value=format_rank(player.flex), inline=True)
    embed.set_thumbnail(url=PROFILE_ICON_URL.format(icon_id=player.icon_id))
    return add_footer(embed)


def comparison_field(player: LolPlayer, solo_rank, solo_winrate, flex_rank, flex_winrate) -> str:
    level = player.level if player.level is not None else "?"
    solo_percent = f"{winrate_percent(player.solo):.0f}%" if player.solo else "—"
    flex_percent = f"{winrate_percent(player.flex):.0f}%" if player.flex else "—"
    return (
        f"Nivel **{level}**\n\n"
        f"🎯 **Solo/Duo**\n{solo_rank} {short_rank(player.solo)}\n{solo_winrate} WR: **{solo_percent}**\n\n"
        f"👥 **Flex**\n{flex_rank} {short_rank(player.flex)}\n{flex_winrate} WR: **{flex_percent}**"
    )


def verdict_text(comparison: Comparison) -> str:
    if comparison.winner is None:
        return "🤝 **Empate** — estadísticas muy parejas"
    if comparison.winner is comparison.first:
        return f"🏆 **{comparison.first.riot_id}** es superior ({comparison.points_first // 2}-{comparison.points_second // 2})"
    return f"🏆 **{comparison.second.riot_id}** es superior ({comparison.points_second // 2}-{comparison.points_first // 2})"


def build_comparison_embed(comparison: Comparison) -> discord.Embed:
    first, second = comparison.first, comparison.second
    embed = discord.Embed(
        title=f"⚔️ {first.riot_id}  vs  {second.riot_id}",
        color=color_for(comparison.winner_tier),
    )
    embed.add_field(
        name=first.riot_id,
        value=comparison_field(
            first, comparison.solo_rank[0], comparison.solo_winrate[0], comparison.flex_rank[0], comparison.flex_winrate[0]
        ),
        inline=True,
    )
    embed.add_field(
        name=second.riot_id,
        value=comparison_field(
            second, comparison.solo_rank[1], comparison.solo_winrate[1], comparison.flex_rank[1], comparison.flex_winrate[1]
        ),
        inline=True,
    )
    embed.add_field(name="Veredicto", value=verdict_text(comparison), inline=False)
    return add_footer(embed)
