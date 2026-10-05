import os
import subprocess
import sys
import textwrap

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

ISOLATED_ENV = {
    "GENIUS_TOKEN": "dummy-genius-token",
    "DISCORD_TOKEN": "dummy-discord-token",
    "YOUTUBE_COOKIES_B64": "",
    "GEMINI_API_KEY": "",
    "RIOT_API_KEY": "",
}


def run_python(code):
    env = {**os.environ, **ISOLATED_ENV}
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(code)],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_import_does_not_start_the_bot():
    result = run_python(
        """
        import discord

        def forbidden_run(*args, **kwargs):
            raise AssertionError("bot.run was called during import")

        discord.Client.run = forbidden_run
        import bot
        print("imported", type(bot.bot).__name__)
        """
    )
    assert result.returncode == 0, result.stderr
    assert "imported Bot" in result.stdout


def test_running_as_main_starts_the_bot_with_discord_token():
    result = run_python(
        """
        import runpy
        import discord

        received = []
        discord.Client.run = lambda self, token, *a, **k: received.append(token)
        runpy.run_path("bot.py", run_name="__main__")
        print("token", received)
        """
    )
    assert result.returncode == 0, result.stderr
    assert "token ['dummy-discord-token']" in result.stdout


def test_import_exposes_the_registered_commands(isolated_bot):
    expected = {
        "play", "skip", "stop", "pause", "resume", "queue", "np", "lyrics",
        "radio", "dj", "comandos", "repo", "loop", "clear", "invocador", "vs",
        "playlist", "reiniciar",
    }
    assert expected <= {command.name for command in isolated_bot.bot.commands}


def test_data_directories_default_next_to_bot_module():
    from tests.conftest import ORIGINAL_DOWNLOAD_DIR, ORIGINAL_PLAYLISTS_DIR

    assert ORIGINAL_DOWNLOAD_DIR == os.path.join(REPO_ROOT, "downloads")
    assert ORIGINAL_PLAYLISTS_DIR == os.path.join(REPO_ROOT, "playlists")
