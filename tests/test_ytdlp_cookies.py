import logging
from pathlib import Path

from config import Settings
from services.ytdlp import _command_base, _friendly_error
from utils.models import ParsedInput


def make_cookie_settings(tmp_path: Path, twitter_cookies: str) -> Settings:
    return Settings(
        bot_token="token",
        owner_id=99,
        auth_users={99},
        download_location=tmp_path / "downloads",
        chunk_size=1024,
        http_proxy="",
        process_max_timeout=120,
        auto_best_quality=False,
        max_video_height=1080,
        twitter_cookies=twitter_cookies,
    )


def make_parsed_input() -> ParsedInput:
    return ParsedInput(source_url="https://x.com/a/status/1")


def test_command_base_includes_cookies_file(tmp_path):
    cookies_path = tmp_path / "cookies.txt"
    cookies_path.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
    settings = make_cookie_settings(tmp_path, str(cookies_path))

    command = _command_base(make_parsed_input(), settings)

    assert ["--cookies", str(cookies_path)] == command[command.index("--cookies"):command.index("--cookies") + 2]
    assert command[:2] == ["yt-dlp", "--no-warnings"]
    assert "--username" not in command
    assert "--password" not in command


def test_command_base_missing_cookies_file_is_omitted(tmp_path, caplog):
    settings = make_cookie_settings(tmp_path, str(tmp_path / "nope.txt"))

    with caplog.at_level(logging.WARNING, logger="services.ytdlp"):
        command = _command_base(make_parsed_input(), settings)

    assert "--cookies" not in command
    assert any(
        "Twitter cookies file not found" in record.message and record.levelno == logging.WARNING
        for record in caplog.records
    )


def test_command_base_inline_cookies_written_to_file(tmp_path):
    inline = "# Netscape HTTP Cookie File\n.x.com\tTRUE\t/\tTRUE\t0\tauth_token\tabc\n"
    settings = make_cookie_settings(tmp_path, inline)

    command = _command_base(make_parsed_input(), settings)

    cookies_path = tmp_path / "downloads" / "twitter-cookies.txt"
    assert "--cookies" in command
    assert cookies_path.read_text(encoding="utf-8") == inline
    assert command[command.index("--cookies") + 1] == str(cookies_path)


def test_friendly_error_rewrites_nsfw_message():
    error_text = (
        "ERROR: [twitter] 123: Twitter extractor says: "
        "NSFW tweet requires authentication. "
        "See https://github.com/yt-dlp/yt-dlp/wiki/FAQ#how-do-i-pass-cookies-to-yt-dlp"
    )

    result = _friendly_error(error_text)

    assert "Twitter 18+ post: login cookies required" in result
    assert "NSFW tweet requires authentication" not in result
    assert "https://github.com/yt-dlp/yt-dlp/wiki/FAQ#how-do-i-pass-cookies-to-yt-dlp" in result


def test_friendly_error_leaves_other_errors_alone():
    error_text = "ERROR: something else"

    assert _friendly_error(error_text) == error_text
