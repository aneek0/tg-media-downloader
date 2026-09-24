import json
from pathlib import Path

import pytest
from unittest.mock import AsyncMock

from config import Settings
from services.gallery import (
    _gallery_download_command,
    _gallery_probe_command,
    _parse_gallery_probe,
    download_gallery_media,
)
from utils.models import ParsedInput


def make_gallery_settings(
    tmp_path: Path, twitter_cookies: str, http_proxy: str = "http://proxy:1"
) -> Settings:
    return Settings(
        bot_token="token",
        owner_id=99,
        auth_users={99},
        download_location=tmp_path / "downloads",
        chunk_size=1024,
        http_proxy=http_proxy,
        process_max_timeout=120,
        auto_best_quality=False,
        max_video_height=1080,
        twitter_cookies=twitter_cookies,
        telegram_api_url="",
        telegram_proxy="",
    )


def make_cookies_file(tmp_path: Path) -> Path:
    cookies_path = tmp_path / "cookies.txt"
    cookies_path.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
    return cookies_path


def test_gallery_probe_command_shape(tmp_path):
    cookies_path = make_cookies_file(tmp_path)
    settings = make_gallery_settings(tmp_path, str(cookies_path))
    url = "https://x.com/a/status/1"

    command = _gallery_probe_command(ParsedInput(source_url=url), settings)

    assert command == [
        "gallery-dl",
        "-j",
        "--proxy",
        "http://proxy:1",
        "-C",
        str(cookies_path),
        "-o",
        "extractor.twitter.cookies-update=false",
        url,
    ]


def test_gallery_download_command_shape(tmp_path):
    cookies_path = make_cookies_file(tmp_path)
    settings = make_gallery_settings(tmp_path, str(cookies_path))
    work_dir = tmp_path / "work"
    url = "https://x.com/a/status/1"

    command = _gallery_download_command(ParsedInput(source_url=url), settings, work_dir)

    assert ("-D", str(work_dir)) in zip(command, command[1:])
    assert ("-f", "{tweet_id}_{num}.{extension}") in zip(command, command[1:])
    assert "--no-part" in command
    assert "--no-mtime" in command
    assert ("-C", str(cookies_path)) in zip(command, command[1:])
    assert command[-1] == url

    bare_settings = make_gallery_settings(tmp_path, "", http_proxy="")
    bare_command = _gallery_download_command(
        ParsedInput(source_url=url), bare_settings, work_dir
    )
    assert "--proxy" not in bare_command
    assert "-C" not in bare_command
    assert "-o" not in bare_command


def test_parse_gallery_probe_extracts_media_and_content():
    stdout = json.dumps(
        [
            [2, {"content": "kitty cage"}],
            [
                3,
                "https://pbs.twimg.com/media/abc",
                {"content": "kitty cage", "type": "photo", "extension": "jpg"},
            ],
        ]
    )

    file_dicts, content, error = _parse_gallery_probe(stdout)

    assert len(file_dicts) == 1
    assert file_dicts[0]["extension"] == "jpg"
    assert content == "kitty cage"
    assert error is None


def test_parse_gallery_probe_error_entry():
    stdout = json.dumps([[-1, {"error": "AbortExtraction", "message": "'Unavailable'"}]])

    _, _, error = _parse_gallery_probe(stdout)

    assert error == "'Unavailable'"


@pytest.mark.asyncio
async def test_download_gallery_media_builds_artifacts(monkeypatch, tmp_path):
    cookies_file = make_cookies_file(tmp_path)
    settings = make_gallery_settings(tmp_path, str(cookies_file))
    work_dir = tmp_path / "work"
    probe_json = json.dumps(
        [
            [2, {"content": "kitty cage"}],
            [
                3,
                "https://pbs.twimg.com/media/a",
                {"content": "kitty cage", "type": "photo", "extension": "jpg"},
            ],
            [
                3,
                "https://pbs.twimg.com/media/b",
                {"content": "kitty cage", "type": "video", "extension": "mp4"},
            ],
        ]
    )

    async def fake_run(command, cwd=None):
        if "-j" in command:
            return (probe_json, "")
        (Path(cwd) / "2103014006747439474_1.jpg").write_bytes(b"\xff\xd8\xff\xe0jpg")
        (Path(cwd) / "2103014006747439474_2.mp4").write_bytes(b"\x00\x00\x00\x18mp4")
        return ("", "")

    monkeypatch.setattr("services.gallery._run_command", fake_run)

    artifacts = await download_gallery_media(
        parsed_input=ParsedInput(source_url="https://x.com/AlterKyon/status/2103014006747439474"),
        settings=settings,
        work_dir=work_dir,
    )

    assert len(artifacts) == 2
    assert [artifact.send_type for artifact in artifacts] == ["photo", "video"]
    assert all(artifact.caption == "kitty cage" for artifact in artifacts)
    assert [artifact.file_name for artifact in artifacts] == [
        "2103014006747439474_1.jpg",
        "2103014006747439474_2.mp4",
    ]


@pytest.mark.asyncio
async def test_download_gallery_media_auth_error(monkeypatch, tmp_path):
    settings = make_gallery_settings(tmp_path, "", http_proxy="")
    probe_json = json.dumps([[-1, {"message": "'Unavailable'"}]])
    monkeypatch.setattr(
        "services.gallery._run_command", AsyncMock(return_value=(probe_json, ""))
    )

    with pytest.raises(RuntimeError) as exc_info:
        await download_gallery_media(
            parsed_input=ParsedInput(source_url="https://x.com/a/status/1"),
            settings=settings,
            work_dir=tmp_path / "work",
        )

    assert "login cookies required" in str(exc_info.value)


@pytest.mark.asyncio
async def test_download_gallery_media_no_media(monkeypatch, tmp_path):
    settings = make_gallery_settings(tmp_path, "", http_proxy="")
    monkeypatch.setattr(
        "services.gallery._run_command", AsyncMock(return_value=("[]", ""))
    )

    with pytest.raises(RuntimeError, match="No media found in this tweet"):
        await download_gallery_media(
            parsed_input=ParsedInput(source_url="https://x.com/a/status/1"),
            settings=settings,
            work_dir=tmp_path / "work",
        )


def test_gallery_command_redacted():
    from utils.logging_config import redact_command

    assert redact_command(["gallery-dl", "-C", "/tmp/c.txt"]) == [
        "gallery-dl",
        "-C",
        "***",
    ]
