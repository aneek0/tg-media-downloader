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
    gallery_url_items,
    plain_caption,
    probe_gallery,
    small_thumbnail_url,
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
        gallery_probe_timeout=15,
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

    async def fake_run(command, cwd=None, timeout=None):
        if "-j" in command:
            return (probe_json, "")
        (Path(cwd) / "2103014006747439474_1.jpg").write_bytes(b"\xff\xd8\xff\xe0jpg")
        (Path(cwd) / "2103014006747439474_2.mp4").write_bytes(b"\x00\x00\x00\x18mp4")
        return ("", "")

    monkeypatch.setattr("services.gallery.run_command", fake_run)

    artifacts = await download_gallery_media(
        parsed_input=ParsedInput(source_url="https://x.com/AlterKyon/status/2103014006747439474"),
        settings=settings,
        work_dir=work_dir,
    )

    assert len(artifacts) == 2
    assert [artifact.send_type for artifact in artifacts] == ["photo", "video"]
    assert all(
        artifact.caption
        == "https://x.com/AlterKyon/status/2103014006747439474\nkitty cage"
        for artifact in artifacts
    )
    assert [artifact.file_name for artifact in artifacts] == [
        "2103014006747439474_1.jpg",
        "2103014006747439474_2.mp4",
    ]


@pytest.mark.asyncio
async def test_download_gallery_media_auth_error(monkeypatch, tmp_path):
    settings = make_gallery_settings(tmp_path, "", http_proxy="")
    probe_json = json.dumps([[-1, {"message": "'Unavailable'"}]])
    monkeypatch.setattr(
        "services.gallery.run_command", AsyncMock(return_value=(probe_json, ""))
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
        "services.gallery.run_command", AsyncMock(return_value=("[]", ""))
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


@pytest.mark.asyncio
async def test_probe_gallery_runtime_error(monkeypatch, tmp_path):
    settings = make_gallery_settings(tmp_path, "", http_proxy="")
    monkeypatch.setattr(
        "services.gallery.gallery_supports_url", lambda url: True
    )
    monkeypatch.setattr(
        "services.gallery.run_command", AsyncMock(side_effect=RuntimeError("exit 64"))
    )
    probe = await probe_gallery(
        ParsedInput(source_url="https://s/1"), settings
    )
    assert probe.file_dicts == []
    assert probe.error == "exit 64"


@pytest.mark.asyncio
async def test_probe_gallery_invalid_json(monkeypatch, tmp_path):
    settings = make_gallery_settings(tmp_path, "", http_proxy="")
    monkeypatch.setattr(
        "services.gallery.gallery_supports_url", lambda url: True
    )
    monkeypatch.setattr(
        "services.gallery.run_command", AsyncMock(return_value=("not json", ""))
    )
    probe = await probe_gallery(
        ParsedInput(source_url="https://s/1"), settings
    )
    assert probe.file_dicts == []
    assert probe.error == "gallery-dl probe returned invalid JSON"


@pytest.mark.asyncio
async def test_probe_gallery_valid_stdout(monkeypatch, tmp_path):
    settings = make_gallery_settings(tmp_path, "", http_proxy="")
    monkeypatch.setattr(
        "services.gallery.gallery_supports_url", lambda url: True
    )
    stdout = json.dumps(
        [
            [2, {"content": "cap"}],
            [3, "https://c/x.jpg", {"extension": "jpg", "filename": "x"}],
        ]
    )
    monkeypatch.setattr(
        "services.gallery.run_command", AsyncMock(return_value=(stdout, ""))
    )
    probe = await probe_gallery(
        ParsedInput(source_url="https://s/1"), settings
    )
    assert probe.error is None
    assert probe.content == "cap"
    assert probe.file_dicts[0]["_url"] == "https://c/x.jpg"


def test_small_thumbnail_url_twitter_name_param():
    assert small_thumbnail_url(
        "https://pbs.twimg.com/media/a?format=jpg&name=large"
    ) == "https://pbs.twimg.com/media/a?format=jpg&name=small"


def test_small_thumbnail_url_twitter_adds_name():
    assert small_thumbnail_url(
        "https://pbs.twimg.com/media/a?format=jpg"
    ) == "https://pbs.twimg.com/media/a?format=jpg&name=small"


def test_small_thumbnail_url_pawchive():
    assert small_thumbnail_url(
        "https://file.pawchive.pw/data/x.jpeg"
    ) == "https://img.pawchive.pw/thumbnail/data/x.jpeg"


def test_small_thumbnail_url_unknown_host_returns_none():
    assert small_thumbnail_url("https://example.com/a.png") is None


def test_plain_caption_strips_html():
    assert plain_caption("<p>a</p><p>b</p>") == "a b"
    assert plain_caption(None) is None
    assert plain_caption("   ") is None


def test_gallery_url_items_caption_and_types():
    items = gallery_url_items(
        [
            {"_url": "https://c/a.jpg", "extension": "jpg", "filename": "a"},
            {"_url": "https://c/b.mp4", "extension": "mp4", "filename": "b"},
        ],
        "<p>hello</p>",
        ParsedInput(source_url="https://s/1"),
    )
    assert items[0]["caption"] == "hello"
    assert items[1]["caption"] is None
    assert items[0]["send_type"] == "photo"
    assert items[1]["send_type"] == "video"
    assert items[0]["filename"] == "a.jpg"


def test_gallery_download_command_generic_filename_format(tmp_path):
    from services.gallery import _gallery_download_command

    settings = make_gallery_settings(tmp_path, "", http_proxy="")
    command = _gallery_download_command(
        ParsedInput(source_url="https://pawchive.pw/post/1"), settings, tmp_path / "w"
    )
    idx = command.index("-f")
    assert command[idx + 1] == "{filename}.{extension}"


def test_gallery_download_command_twitter_filename_format(tmp_path):
    from services.gallery import _gallery_download_command

    settings = make_gallery_settings(tmp_path, "", http_proxy="")
    command = _gallery_download_command(
        ParsedInput(source_url="https://x.com/a/status/1"), settings, tmp_path / "w"
    )
    idx = command.index("-f")
    assert command[idx + 1] == "{tweet_id}_{num}.{extension}"
