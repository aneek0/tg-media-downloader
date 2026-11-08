import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

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
from services.sources import gallery_supports_url
from services.status import StatusSurface
from utils.logging_config import redact_command
from utils.models import ParsedInput


def make_status() -> StatusSurface:
    return StatusSurface(AsyncMock(), muted=True)

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
        "-o",
        "extractor.twitter.text-tweets=true",
        url,
    ]


def test_gallery_download_command_shape(tmp_path):
    cookies_path = make_cookies_file(tmp_path)
    settings = make_gallery_settings(tmp_path, str(cookies_path))
    work_dir = tmp_path / "work"
    url = "https://x.com/a/status/1"

    command = _gallery_download_command(ParsedInput(source_url=url), settings, work_dir)

    assert ("-D", str(work_dir)) in zip(command, command[1:], strict=False)
    assert ("-f", "{tweet_id}_{num}.{extension}") in zip(command, command[1:], strict=False)
    assert "--no-part" in command
    assert "--no-mtime" in command
    assert ("-C", str(cookies_path)) in zip(command, command[1:], strict=False)
    assert command[-1] == url

    bare_settings = make_gallery_settings(tmp_path, "", http_proxy="")
    bare_command = _gallery_download_command(
        ParsedInput(source_url=url), bare_settings, work_dir
    )
    assert "--proxy" not in bare_command
    assert "-C" not in bare_command
    assert "extractor.twitter.cookies-update=false" not in bare_command


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
                {
                    "content": "kitty cage",
                    "type": "photo",
                    "extension": "jpg",
                    "tweet_id": "2103014006747439474",
                    "num": 1,
                },
            ],
            [
                3,
                "https://pbs.twimg.com/media/b",
                {
                    "content": "kitty cage",
                    "type": "video",
                    "extension": "mp4",
                    "tweet_id": "2103014006747439474",
                    "num": 2,
                },
            ],
        ]
    )

    async def fake_download(*, work_dir, file_name, **_kwargs):
        path = Path(work_dir) / file_name
        path.write_bytes(b"media")
        return path

    monkeypatch.setattr("services.gallery.run_command", AsyncMock(return_value=(probe_json, "")))
    monkeypatch.setattr("services.gallery.download_http_file", fake_download)

    artifacts = await download_gallery_media(
        parsed_input=ParsedInput(source_url="https://x.com/AlterKyon/status/2103014006747439474"),
        settings=settings,
        work_dir=work_dir,
        status=make_status(),
        download_limiter=asyncio.Semaphore(6),
    )

    assert len(artifacts) == 2
    assert [artifact.send_type for artifact in artifacts] == ["photo", "video"]
    assert all(
        artifact.caption
        == '<b><a href="https://x.com/AlterKyon/status/2103014006747439474">kitty cage</a></b>'
        for artifact in artifacts
    )
    assert [artifact.file_name for artifact in artifacts] == [
        "2103014006747439474_1.jpg",
        "2103014006747439474_2.mp4",
    ]


@pytest.mark.asyncio
async def test_single_gallery_file_uses_http_before_gallery_dl(monkeypatch, tmp_path):
    """Single Pawchive CDN files must not use gallery-dl, which fails with 403."""
    settings = make_gallery_settings(tmp_path, "", http_proxy="")
    run_mock = AsyncMock()
    monkeypatch.setattr("services.gallery.run_command", run_mock)

    async def fake_download(*, work_dir, file_name, **_kwargs):
        path = Path(work_dir) / file_name
        path.write_bytes(b"video")
        return path

    monkeypatch.setattr("services.gallery.download_http_file", fake_download)
    source_url = "https://file.pawchive.pw/data/video.mp4"

    artifacts = await download_gallery_media(
        parsed_input=ParsedInput(source_url=source_url),
        settings=settings,
        work_dir=tmp_path / "work",
        status=make_status(),
        download_limiter=asyncio.Semaphore(6),
        file_dicts=[{"_url": source_url, "filename": "first recording", "extension": "mp4"}],
    )

    run_mock.assert_not_awaited()
    assert len(artifacts) == 1
    assert artifacts[0].send_type == "video"
    assert artifacts[0].file_name == "first recording.mp4"
    assert artifacts[0].caption == f'<b><a href="{source_url}">first recording</a></b>'

@pytest.mark.asyncio
async def test_download_gallery_media_downloads_three_files_concurrently(monkeypatch, tmp_path):
    """Album downloads overlap without losing order or duplicate-named files."""
    settings = make_gallery_settings(tmp_path, "", http_proxy="")
    file_dicts = [
        {
            "_url": f"https://file.pawchive.pw/{index}",
            "_http_headers": {"Referer": "https://pawchive.pw/post"},
            "filename": "same",
            "extension": "jpg",
        }
        for index in range(1, 5)
    ]
    active = 0
    max_active = 0

    async def fake_download(*, url, headers, work_dir, file_name, file_ext, **_kwargs):
        nonlocal active, max_active
        assert file_ext == "jpg"
        assert headers == {"Referer": "https://pawchive.pw/post"}
        active += 1
        max_active = max(max_active, active)
        try:
            await asyncio.sleep(0.02)
            path = Path(work_dir) / file_name
            path.write_bytes(url.rsplit("/", 1)[-1].encode())
            return path
        finally:
            active -= 1

    monkeypatch.setattr("services.gallery.download_http_file", fake_download)

    artifacts = await download_gallery_media(
        parsed_input=ParsedInput(source_url="https://pawchive.pw/fantia/user/1/post/2"),
        settings=settings,
        work_dir=tmp_path / "work",
        status=make_status(),
        download_limiter=asyncio.Semaphore(6),
        file_dicts=file_dicts,
        content="album",
    )

    assert max_active == 3
    assert [artifact.path.read_bytes() for artifact in artifacts] == [b"1", b"2", b"3", b"4"]
    assert len({artifact.file_name for artifact in artifacts}) == 4


@pytest.mark.asyncio
async def test_download_gallery_media_uses_shared_limit(monkeypatch, tmp_path):
    """The process-wide limiter applies on top of the per-album limit."""
    settings = make_gallery_settings(tmp_path, "", http_proxy="")
    file_dicts = [
        {"_url": f"https://cdn.example/{index}", "filename": str(index), "extension": "jpg"}
        for index in range(2)
    ]
    active = 0
    max_active = 0

    async def fake_download(*, work_dir, file_name, **_kwargs):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        try:
            await asyncio.sleep(0.02)
            path = Path(work_dir) / file_name
            path.write_bytes(b"media")
            return path
        finally:
            active -= 1

    monkeypatch.setattr("services.gallery.download_http_file", fake_download)

    await download_gallery_media(
        parsed_input=ParsedInput(source_url="https://pawchive.pw/fantia/user/1/post/2"),
        settings=settings,
        work_dir=tmp_path / "work",
        status=make_status(),
        download_limiter=asyncio.Semaphore(1),
        file_dicts=file_dicts,
    )

    assert max_active == 1


@pytest.mark.asyncio
async def test_download_gallery_media_fallback_discards_partial_files(monkeypatch, tmp_path):
    """A failed parallel attempt cannot leak partial files into gallery-dl results."""
    settings = make_gallery_settings(tmp_path, "", http_proxy="")
    file_dicts = [
        {"_url": f"https://cdn.example/{index}", "filename": str(index), "extension": "jpg"}
        for index in range(2)
    ]

    async def failed_download(*, work_dir, file_name, **_kwargs):
        path = Path(work_dir) / file_name
        path.write_bytes(b"partial")
        raise RuntimeError("expired CDN URL")

    async def fallback(command, cwd=None, timeout=None):  # noqa: ARG001
        (Path(cwd) / "one.jpg").write_bytes(b"one")
        (Path(cwd) / "two.jpg").write_bytes(b"two")
        return "", ""

    monkeypatch.setattr("services.gallery.download_http_file", failed_download)
    fallback_mock = AsyncMock(side_effect=fallback)
    monkeypatch.setattr("services.gallery.run_command", fallback_mock)

    artifacts = await download_gallery_media(
        parsed_input=ParsedInput(source_url="https://pawchive.pw/fantia/user/1/post/2"),
        settings=settings,
        work_dir=tmp_path / "work",
        status=make_status(),
        download_limiter=asyncio.Semaphore(6),
        file_dicts=file_dicts,
    )

    assert fallback_mock.await_count == 1
    assert [artifact.file_name for artifact in artifacts] == ["one.jpg", "two.jpg"]
    assert all(artifact.path.read_bytes() != b"partial" for artifact in artifacts)

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
            status=make_status(),
            download_limiter=asyncio.Semaphore(6),
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
            status=make_status(),
            download_limiter=asyncio.Semaphore(6),
        )


def test_gallery_command_redacted():
    assert redact_command(["gallery-dl", "-C", "/tmp/c.txt"]) == [
        "gallery-dl",
        "-C",
        "***",
    ]



@pytest.mark.parametrize(
    "url",
    [
        "https://www.tiktok.com/@myaffq7/video/7704044130346831105",
        "https://vt.tiktok.com/ZSpJRCB2v/",
        "https://vm.tiktok.com/ZGdh4WUhr/",
        "https://www.tiktok.com/@chillezy/photo/7240568259186019630",
    ],
)
def test_tiktok_gallery_disables_only_videos(url, tmp_path):
    """Photo posts stay in gallery-dl; video posts fall through to yt-dlp."""
    settings = make_gallery_settings(tmp_path, "")

    assert gallery_supports_url(url) is True
    for command in (
        _gallery_probe_command(ParsedInput(source_url=url), settings),
        _gallery_download_command(ParsedInput(source_url=url), settings, tmp_path),
    ):
        assert ("-o", "videos=false") in zip(command, command[1:], strict=False)


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


def test_plain_caption_keeps_line_breaks():
    """Regression: a numbered X post used to collapse into one paragraph."""
    assert plain_caption("a\n\n\n\nb") == "a\n\nb"
    assert plain_caption("1. one  \r\n2. two") == "1. one\n2. two"


def test_gallery_url_items_keeps_long_caption():
    """Regression: captions were cut at 1024 characters, dropping the tail
    links of long posts."""
    body = "\n".join(f"line {index} https://example.com/{index}" for index in range(80))
    items = gallery_url_items(
        [{"_url": "https://c/a.jpg", "extension": "jpg", "filename": "a"}],
        body,
        ParsedInput(source_url="https://s/1"),
    )
    assert items[0]["caption"] == f'<b><a href="https://s/1">{body}</a></b>'
    assert items[0]["caption"].endswith("https://example.com/79</a></b>")


def test_gallery_url_items_escapes_markup():
    items = gallery_url_items(
        [{"_url": "https://c/a.jpg", "extension": "jpg", "filename": "a"}],
        "AT&T <b>leak</b>",
        ParsedInput(source_url="https://s/1"),
    )
    assert items[0]["caption"] == '<b><a href="https://s/1">AT&amp;T leak</a></b>'


def test_gallery_url_items_caption_and_types():
    items = gallery_url_items(
        [
            {"_url": "https://c/a.jpg", "extension": "jpg", "filename": "a"},
            {"_url": "https://c/b.mp4", "extension": "mp4", "filename": "b"},
        ],
        "<p>hello</p>",
        ParsedInput(source_url="https://s/1"),
    )
    assert items[0]["caption"] == '<b><a href="https://s/1">hello</a></b>'
    assert items[1]["caption"] is None
    assert items[0]["send_type"] == "photo"
    assert items[1]["send_type"] == "video"
    assert items[0]["filename"] == "a.jpg"


def test_gallery_download_command_generic_filename_format(tmp_path):
    settings = make_gallery_settings(tmp_path, "", http_proxy="")
    command = _gallery_download_command(
        ParsedInput(source_url="https://pawchive.pw/post/1"), settings, tmp_path / "w"
    )
    idx = command.index("-f")
    assert command[idx + 1] == "{filename}.{extension}"


def test_gallery_url_items_uses_escaped_filename_link_when_post_has_no_text():
    items = gallery_url_items(
        [{"_url": "https://cdn/1.jpg", "filename": "post <1>", "extension": "jpg"}],
        None,
        ParsedInput(source_url="https://example.com/post?a=1&b=2"),
    )

    assert items[0]["caption"] == (
        '<b><a href="https://example.com/post?a=1&amp;b=2">post &lt;1&gt;</a></b>'
    )


def test_gallery_download_command_twitter_filename_format(tmp_path):
    settings = make_gallery_settings(tmp_path, "", http_proxy="")
    command = _gallery_download_command(
        ParsedInput(source_url="https://x.com/a/status/1"), settings, tmp_path / "w"
    )
    idx = command.index("-f")
    assert command[idx + 1] == "{tweet_id}_{num}.{extension}"
