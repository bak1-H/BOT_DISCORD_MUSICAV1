import help_content

VOSEO_FORMS = ("podés", "tenés", "querés", "necesitás", "pedí", "elegí", "tené", "revisá", "acá", "dale")


def full_text():
    return "\n".join(f"{title}\n{body}" for title, body in help_content.HELP_SECTIONS)


def test_sections_are_titled_nonempty_pairs():
    assert len(help_content.HELP_SECTIONS) >= 5
    for title, body in help_content.HELP_SECTIONS:
        assert title.strip()
        assert body.strip()


def test_explains_how_to_address_the_bot():
    text = full_text().lower()
    assert "makakiño" in text
    assert "@" in text
    assert "responde" in text


def test_mentions_confirmation_flow():
    assert "confirmar" in full_text().lower()
    assert "60" in full_text()


def test_lists_every_fallback_command():
    text = full_text()
    for command in ("!play", "!skip", "!stop", "!salir", "!listen", "!reiniciar", "!ayuda"):
        assert command in text
    assert help_content.FALLBACK_COMMANDS == ("!play", "!skip", "!stop", "!salir", "!listen", "!reiniciar", "!ayuda")


def test_contains_example_phrases_for_each_capability():
    text = full_text().lower()
    for fragment in ("después de esta", "saca la 3", "cola", "radio", "letra", "playlist", "faker#kr1"):
        assert fragment in text


def test_repo_url_is_in_help_text():
    assert help_content.REPO_URL == "https://github.com/bak1-H/BOT_DISCORD_MUSICA"
    assert help_content.REPO_URL in full_text()


def test_no_voseo_in_user_facing_text():
    text = (full_text() + help_content.HELP_TITLE).lower()
    for form in VOSEO_FORMS:
        assert form not in text


def test_module_has_no_third_party_or_discord_dependency():
    import ast
    import pathlib

    source = pathlib.Path(help_content.__file__).read_text(encoding="utf-8")
    imported = {
        node.names[0].name.split(".")[0]
        for node in ast.walk(ast.parse(source))
        if isinstance(node, (ast.Import, ast.ImportFrom)) and getattr(node, "names", None)
    }
    assert imported <= {"__future__"}


async def test_ayuda_command_sends_one_embed_built_from_the_help_content(isolated_bot):
    from tests.fakes import FakeContext

    ctx = FakeContext()

    await isolated_bot.ayuda.callback(ctx)

    embed = ctx.notifier.messages[0]["embed"]
    assert embed.title == help_content.HELP_TITLE
    assert [field.name for field in embed.fields][: len(help_content.HELP_SECTIONS)] == [
        title for title, _ in help_content.HELP_SECTIONS
    ]
    assert [field.value for field in embed.fields][: len(help_content.HELP_SECTIONS)] == [
        body for _, body in help_content.HELP_SECTIONS
    ]
    assert all(command in embed.fields[-1].value for command in help_content.FALLBACK_COMMANDS)
    assert all(len(field.value) <= 1024 for field in embed.fields)


def test_ayuda_and_comandos_are_both_registered(isolated_bot):
    names = {command.name for command in isolated_bot.bot.commands}

    assert {"ayuda", "comandos"} <= names
