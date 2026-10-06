import os
import re
import unicodedata

BOT_NAME = "makakino"
COMMAND_PREFIX = "!"
SEPARATORS_AFTER_NAME = " ,:;-"
AUTO_CHANNEL_NAME = os.environ.get("AGENT_AUTO_CHANNEL", "temas")
AUTO_CHANNEL_MIN_WORDS = 2
ACTION_VERB_STEMS = (
    "pon", "toc", "reproduc", "sac", "quit", "borr", "elimin", "paus",
    "deten", "salt", "repit", "mezcl", "baraj", "sub", "baj", "busc",
    "muestr", "guard", "carg", "agreg", "anad", "compar",
)


def fold_char(char: str) -> str:
    decomposed = unicodedata.normalize("NFKD", char)
    return "".join(part for part in decomposed if not unicodedata.combining(part)).casefold()


def name_prefix_end(content: str) -> int | None:
    folded = ""
    origin = []
    for index, char in enumerate(content):
        piece = fold_char(char)
        folded += piece
        origin.extend([index] * len(piece))
    if not folded.startswith(BOT_NAME):
        return None
    if len(folded) > len(BOT_NAME) and folded[len(BOT_NAME)].isalnum():
        return None
    return origin[len(BOT_NAME) - 1] + 1


def fold_word(word: str) -> str:
    return "".join(fold_char(char) for char in word)


def has_action_verb(text: str) -> bool:
    return any(
        fold_word(word).startswith(stem)
        for word in text.split()
        for stem in ACTION_VERB_STEMS
    )


def looks_like_a_request(text: str) -> bool:
    return len(text.split()) >= AUTO_CHANNEL_MIN_WORDS and has_action_verb(text)


def is_auto_respond_channel(channel) -> bool:
    name = getattr(channel, "name", None)
    return name is not None and name.casefold() == AUTO_CHANNEL_NAME.casefold()


def mention_pattern(bot_user) -> re.Pattern:
    return re.compile(rf"<@!?{bot_user.id}>")


def replies_to_bot(message, bot_user) -> bool:
    resolved = getattr(message.reference, "resolved", None)
    author = getattr(resolved, "author", None)
    return author is not None and author.id == bot_user.id


def extract_request(message, bot_user) -> str | None:
    if bot_user is None or message.guild is None:
        return None
    author = message.author
    if getattr(author, "bot", False) or author.id == bot_user.id:
        return None
    content = message.content.lstrip()
    if content.startswith(COMMAND_PREFIX):
        return None

    name_end = name_prefix_end(content)
    if name_end is not None:
        return content[name_end:].lstrip(SEPARATORS_AFTER_NAME).strip()
    if bot_user in message.mentions or replies_to_bot(message, bot_user):
        return mention_pattern(bot_user).sub("", content).strip()
    if is_auto_respond_channel(message.channel):
        stripped = content.strip()
        return stripped if looks_like_a_request(stripped) else None
    return None
