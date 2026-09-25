import json
import logging
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from config import Settings
from services.progress import StatusProgress
from services.ytdlp import (
    _command_base,
    _friendly_error,
    _run_command,
    download_best_quality,
    download_quick_youtube,
    download_selected_format,
    probe_url,
)
from utils.models import DownloadOption, ParsedInput


@pytest.mark.asyncio
async def test_download_best_quality_progress_flags_and_forwarding(monkeypatch, tmp_path):
    settings = make_cookie_settings(tmp_path, "")
    info = {"title": "Track", "formats": [{"vcodec": "none", "acodec": "mp4a"}]}
    run_mock = AsyncMock(return_value=("", ""))
    monkeypatch.setattr("services.ytdlp._run_command", run_mock)
    media = tmp_path / "work" / "Track [id].mp3"
    media.parent.mkdir(parents=True)
    media.write_bytes(b"x")
    parsed = ParsedInput(source_url="https://soundcloud.com/example/track")

    async def edit(_text: str) -> None:
        return None

    progress = StatusProgress(edit, "Track", min_interval=0)

    await download_best_quality(
        parsed_input=parsed,
        settings=settings,
        work_dir=tmp_path / "work",
        info=info,
        progress=progress,
    )

    kwargs = run_mock.await_args.kwargs
    command = run_mock.await_args.args[0]
    assert "--newline" in command
    assert "--progress" in command
    assert kwargs["on_output"] == progress.feed_line
    assert kwargs["timeout"] == settings.process_max_timeout


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


def test_command_base_appends_speed_flags(tmp_path):
    settings = make_cookie_settings(tmp_path, "")

    command = _command_base(make_parsed_input(), settings)

    assert command[-4:] == ["-N", "4", "--http-chunk-size", "10M"]


@pytest.mark.asyncio
async def test_run_command_forwards_to_proc_run_command(monkeypatch):
    proc_run = AsyncMock(return_value=("out", "err"))
    monkeypatch.setattr("services.ytdlp.run_command", proc_run)

    stdout, stderr = await _run_command(
        ["yt-dlp", "--version"], cwd=Path("/tmp"), timeout=12.0
    )

    assert (stdout, stderr) == ("out", "err")
    proc_run.assert_awaited_once_with(
        ["yt-dlp", "--version"], cwd=Path("/tmp"), timeout=12.0, on_output=None
    )


@pytest.mark.asyncio
async def test_run_command_rewrites_friendly_error(monkeypatch):
    proc_run = AsyncMock(
        side_effect=RuntimeError("ERROR: NSFW tweet requires authentication.")
    )
    monkeypatch.setattr("services.ytdlp.run_command", proc_run)

    with pytest.raises(RuntimeError) as exc_info:
        await _run_command(["yt-dlp", "-x"])

    assert "Twitter 18+ post: login cookies required" in str(exc_info.value)


@pytest.mark.asyncio
async def test_probe_url_passes_no_playlist_and_timeout(monkeypatch, tmp_path):
    settings = make_cookie_settings(tmp_path, "")
    payload = {"title": "T", "id": "1", "duration": 61, "width": 1280, "height": 720}
    run_mock = AsyncMock(return_value=(json.dumps(payload), ""))
    monkeypatch.setattr("services.ytdlp._run_command", run_mock)

    result = await probe_url(make_parsed_input(), settings)

    assert result == payload
    command = run_mock.await_args.args[0]
    assert "--no-playlist" in command
    assert run_mock.await_args.kwargs["timeout"] == settings.process_max_timeout


@pytest.mark.asyncio
async def test_download_quick_youtube_metadata_and_flags(monkeypatch, tmp_path):
    settings = make_cookie_settings(tmp_path, "")
    info = {"title": "Clip", "webpage_url": "https://youtu.be/w", "duration": 61, "width": 1280, "height": 720}
    probe_mock = AsyncMock(return_value=info)
    run_mock = AsyncMock(return_value=("", ""))
    monkeypatch.setattr("services.ytdlp.probe_url", probe_mock)
    monkeypatch.setattr("services.ytdlp._run_command", run_mock)
    media = tmp_path / "work" / "Clip [w].mp4"
    media.parent.mkdir(parents=True)
    media.write_bytes(b"x")
    parsed = ParsedInput(source_url="https://youtu.be/w")
    option = DownloadOption(
        option_id="quick_video", label="Video", send_type="video", mode="youtube_quick"
    )

    artifact = await download_quick_youtube(parsed, option, settings, tmp_path / "work")

    assert artifact.duration == 61
    assert artifact.width == 1280
    assert artifact.height == 720
    command = run_mock.await_args.args[0]
    assert "--no-playlist" in command
    assert run_mock.await_args.kwargs["timeout"] == settings.process_max_timeout


@pytest.mark.asyncio
async def test_download_quick_youtube_metadata_defaults_to_none(monkeypatch, tmp_path):
    settings = make_cookie_settings(tmp_path, "")
    info = {"title": "Clip"}
    probe_mock = AsyncMock(return_value=info)
    run_mock = AsyncMock(return_value=("", ""))
    monkeypatch.setattr("services.ytdlp.probe_url", probe_mock)
    monkeypatch.setattr("services.ytdlp._run_command", run_mock)
    media = tmp_path / "work" / "Clip [w].mp3"
    media.parent.mkdir(parents=True)
    media.write_bytes(b"x")
    parsed = ParsedInput(source_url="https://youtu.be/w")
    option = DownloadOption(
        option_id="quick_audio", label="Audio", send_type="audio", mode="youtube_quick"
    )

    artifact = await download_quick_youtube(parsed, option, settings, tmp_path / "work")

    assert artifact.duration is None
    assert artifact.width is None
    assert artifact.height is None


@pytest.mark.asyncio
async def test_download_best_quality_metadata_and_flags(monkeypatch, tmp_path):
    settings = make_cookie_settings(tmp_path, "")
    info = {
        "title": "Track",
        "duration": 91,
        "width": None,
        "height": None,
        "formats": [{"vcodec": "none", "acodec": "mp4a"}],
    }
    run_mock = AsyncMock(return_value=("", ""))
    monkeypatch.setattr("services.ytdlp._run_command", run_mock)
    media = tmp_path / "work" / "Track [id].mp3"
    media.parent.mkdir(parents=True)
    media.write_bytes(b"x")
    parsed = ParsedInput(source_url="https://soundcloud.com/example/track")

    artifact = await download_best_quality(
        parsed_input=parsed,
        settings=settings,
        work_dir=tmp_path / "work",
        info=info,
    )

    assert artifact.duration == 91
    assert artifact.width is None
    assert artifact.height is None
    command = run_mock.await_args.args[0]
    assert "--no-playlist" in command
    assert run_mock.await_args.kwargs["timeout"] == settings.process_max_timeout


@pytest.mark.asyncio
async def test_download_selected_format_metadata_and_flags(monkeypatch, tmp_path):
    settings = make_cookie_settings(tmp_path, "")
    info = {"title": "Frag", "duration": 0, "width": 640, "height": 360}
    run_mock = AsyncMock(return_value=("", ""))
    monkeypatch.setattr("services.ytdlp._run_command", run_mock)
    media = tmp_path / "work" / "Frag [id].mp4"
    media.parent.mkdir(parents=True)
    media.write_bytes(b"x")
    parsed = ParsedInput(source_url="https://youtu.be/w")
    option = DownloadOption(
        option_id="v0",
        label="Video 360p mp4",
        send_type="video",
        mode="ytdlp_format",
        format_id="137",
    )

    artifact = await download_selected_format(
        parsed_input=parsed,
        option=option,
        info=info,
        settings=settings,
        work_dir=tmp_path / "work",
    )

    assert artifact.duration is None
    assert artifact.width == 640
    assert artifact.height == 360
    command = run_mock.await_args.args[0]
    assert "--no-playlist" in command
    assert run_mock.await_args.kwargs["timeout"] == settings.process_max_timeout
