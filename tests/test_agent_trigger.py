import pytest

from agent.trigger import extract_request
from tests.discord_support import BOT_ID, FakeBotUser, FakeIncomingMessage, FakeSender

BOT = FakeBotUser()


def message(content, **kwargs):
    return FakeIncomingMessage(content, **kwargs)


@pytest.mark.parametrize(
    "content",
    ["MAKAKIÑO pon algo", "makakiño pon algo", "makakino pon algo", "Makakiño pon algo", "  makakiño pon algo"],
)
def test_name_prefix_triggers_case_and_accent_insensitive(content):
    assert extract_request(message(content), BOT) == "pon algo"


def test_decomposed_tilde_in_name_triggers():
    assert extract_request(message("makakiño pon algo"), BOT) == "pon algo"


@pytest.mark.parametrize("content", ["makakiño, pon algo", "makakiño: pon algo", "makakiño - pon algo"])
def test_punctuation_after_the_name_is_dropped(content):
    assert extract_request(message(content), BOT) == "pon algo"


def test_name_followed_by_letters_is_not_the_name():
    assert extract_request(message("makakinoides pon algo"), BOT) is None


def test_name_in_the_middle_does_not_trigger():
    assert extract_request(message("hola makakiño pon algo"), BOT) is None


@pytest.mark.parametrize("mention", [f"<@{BOT_ID}>", f"<@!{BOT_ID}>"])
def test_mention_triggers_and_is_removed_from_the_text(mention):
    incoming = message(f"{mention} pon algo {mention}", mentions=[BOT])

    assert extract_request(incoming, BOT) == "pon algo"


def test_mention_of_someone_else_does_not_trigger():
    other = FakeBotUser(123)

    assert extract_request(message("<@123> hola", mentions=[other]), BOT) is None


def test_reply_to_a_bot_message_triggers_with_the_full_text():
    previous = message("algo", author=FakeSender(user_id=BOT_ID, is_bot=True))
    previous.author = BOT

    assert extract_request(message("quita la 3", reply_to=previous), BOT) == "quita la 3"


def test_reply_to_a_human_message_does_not_trigger():
    previous = message("algo", author=FakeSender(user_id=55))

    assert extract_request(message("quita la 3", reply_to=previous), BOT) is None


def test_reply_to_a_deleted_message_does_not_trigger():
    assert extract_request(message("quita la 3", reply_to=object()), BOT) is None


def test_plain_chatter_does_not_trigger():
    assert extract_request(message("hola a todos"), BOT) is None


@pytest.mark.parametrize("content", ["!skip", "!ayuda", "  !play algo", f"!play <@{BOT_ID}>"])
def test_bang_commands_never_reach_the_agent(content):
    assert extract_request(message(content, mentions=[BOT]), BOT) is None


def test_bang_command_with_the_name_prefix_is_still_a_command():
    assert extract_request(message("!makakiño pon algo"), BOT) is None


def test_bot_authors_are_ignored_even_when_mentioning():
    sender = FakeSender(user_id=5, is_bot=True)

    assert extract_request(message("makakiño pon algo", author=sender), BOT) is None


def test_own_messages_are_ignored():
    assert extract_request(message("makakiño pon algo", author=BOT), BOT) is None


def test_direct_messages_are_ignored():
    assert extract_request(message("makakiño pon algo", in_guild=False), BOT) is None


def test_bare_name_returns_empty_text_so_the_listener_can_answer_without_llm():
    assert extract_request(message("makakiño"), BOT) == ""


def test_unready_bot_user_never_triggers():
    assert extract_request(message("makakiño pon algo"), None) is None
