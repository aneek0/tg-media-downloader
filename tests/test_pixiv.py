"""Pixiv: URL routing, Profile Page composition, credentials and surfaces."""

from __future__ import annotations

import dataclasses
import json
import os
import pickle
import stat
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from services import pixiv, pixiv_flow
from services.gallery import (
    _download_gallery_files,
    _gallery_flags,
    probe_gallery,
)
from tests.conftest import make_message, make_session, make_settings
from utils import text
from utils.models import ParsedInput, PixivProfilePayload, RequestType


@pytest.fixture(autouse=True)
def isolated_gallery_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Keep gallery-dl's OAuth cache away from the developer's real one, so a
    locally cached Pixiv token cannot change test outcomes."""
    cache_home = tmp_path / "xdg-cache"
    cache_home.mkdir()
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache_home))
    return cache_home


def make_pixiv_settings(tmp_path: Path, **overrides):
    settings = make_settings(tmp_path)
    values = {
        "pixiv_refresh_token": "refresh-token-value",
        "pixiv_cookies": "",
        **overrides,
    }
    return dataclasses.replace(settings, **values)


def make_work(work_id: str, pages: int = 1, ugoira: bool = False) -> pixiv.PixivWork:
    return pixiv.PixivWork(
        work_id=work_id,
        title=f"work {work_id}",
        artist="artist",
        pages=pages,
        cover_url=f"https://i.pximg.net/{work_id}.jpg",
        ugoira=ugoira,
    )


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://www.pixiv.net/artworks/123", "work"),
        ("https://www.pixiv.net/en/artworks/123", "work"),
        ("https://www.pixiv.net/i/123", "work"),
        ("https://www.pixiv.net/artworks/unlisted/AbC-1", "work"),
        ("https://www.pixiv.net/member_illust.php?mode=medium&illust_id=123", "work"),
        ("https://i.pximg.net/img-original/img/2020/01/01/00/00/00/1_p0.jpg", "work"),
        ("https://www.pixiv.net/users/456", "profile"),
        ("https://www.pixiv.net/en/users/456/artworks", "profile"),
        ("https://www.pixiv.net/u/456", "profile"),
        ("https://pixiv.me/someone", "profile"),
        ("https://www.pixiv.net/users/456/bookmarks", "unsupported"),
        ("https://www.pixiv.net/ranking.php?mode=daily", "unsupported"),
        ("https://www.pixiv.net/novel/show.php?id=7", "unsupported"),
        ("https://www.pixiv.net/tags/foo", "unsupported"),
        ("https://www.pixiv.net/users/456/bookmarks/artworks", "unsupported"),
    ],
)
def test_pixiv_url_kinds(url: str, expected: str | None) -> None:
    assert pixiv.pixiv_kind(url) == expected


def test_profile_user_id_reads_path_and_query() -> None:
    assert pixiv.profile_user_id("https://www.pixiv.net/users/456/artworks") == "456"
    assert pixiv.profile_user_id("https://www.pixiv.net/member.php?id=9") == "9"
    assert pixiv.profile_user_id("https://www.pixiv.net/artworks/123") is None


def test_page_never_exceeds_the_file_limit_or_splits_a_work() -> None:
    works = [make_work(str(index), pages=7) for index in range(12)]
    taken = pixiv.page_works(works)

    assert len(taken) == 7
    assert pixiv.page_file_count(taken) == 49
    # The next Work would push the page past 50 files: it opens the next page.
    assert taken[-1].work_id == "6"


def test_page_caps_work_count_even_when_files_are_small() -> None:
    taken = pixiv.page_works([make_work(str(index)) for index in range(25)])
    assert len(taken) == pixiv.PIXIV_PAGE_WORKS


def test_single_oversized_work_is_returned_alone_for_rejection() -> None:
    taken = pixiv.page_works([make_work("1", pages=80), make_work("2")])
    assert [work.work_id for work in taken] == ["1"]


def test_gallery_config_file_is_private_and_removed(tmp_path: Path) -> None:
    settings = make_pixiv_settings(tmp_path)

    with pixiv.gallery_config(settings) as flags:
        assert flags[:1] == ["--config"]
        config_path = Path(flags[1])
        assert stat.S_IMODE(config_path.stat().st_mode) == 0o600
        payload = json.loads(config_path.read_text(encoding="utf-8"))
        assert payload["extractor"]["pixiv"]["refresh-token"] == "refresh-token-value"
        # The secret never rides in argv, where it would be logged.
        assert "refresh-token-value" not in " ".join(flags)

    assert not config_path.exists()


def test_gallery_config_without_token_adds_no_flags(tmp_path: Path) -> None:
    settings = make_pixiv_settings(tmp_path, pixiv_refresh_token="")
    with pixiv.gallery_config(settings) as flags:
        assert flags == []


def test_pixiv_flags_use_pixiv_cookies_and_convert_ugoira_on_download(
    tmp_path: Path,
) -> None:
    cookies = tmp_path / "pixiv-cookies.txt"
    cookies.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
    settings = make_pixiv_settings(
        tmp_path, pixiv_cookies=str(cookies), twitter_cookies="never-used"
    )
    url = "https://www.pixiv.net/artworks/1"

    probe_flags = _gallery_flags(settings, url)
    download_flags = _gallery_flags(settings, url, download=True)

    assert "-C" in probe_flags
    assert str(cookies) in probe_flags
    assert "--ugoira" not in probe_flags
    assert download_flags[download_flags.index("--ugoira") + 1] == "mp4"


def test_twitter_flags_survive_the_pixiv_branch(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    flags = _gallery_flags(settings, "https://x.com/user/status/1")
    assert "extractor.twitter.text-tweets=true" in flags
    assert "--ugoira" not in flags


@pytest.mark.asyncio
async def test_probe_passes_the_token_by_file_and_never_by_argv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = make_pixiv_settings(tmp_path)
    seen: dict[str, list[str]] = {}

    async def fake_run(command, **_kwargs):
        seen["command"] = list(command)
        return "[]", ""

    monkeypatch.setattr("services.gallery.run_command", fake_run)

    await probe_gallery(
        ParsedInput(source_url="https://www.pixiv.net/artworks/1"), settings
    )

    command = seen["command"]
    assert command[:2] == ["gallery-dl", "-j"]
    assert "--config" in command
    assert "refresh-token-value" not in " ".join(command)


@pytest.mark.asyncio
async def test_pximg_downloads_carry_the_referer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = make_settings(tmp_path)
    captured: dict[str, dict | None] = {}

    async def fake_download(**kwargs):
        captured["headers"] = kwargs["headers"]
        path = kwargs["work_dir"] / kwargs["file_name"]
        path.write_bytes(b"data")
        return path

    monkeypatch.setattr("services.gallery.download_http_file", fake_download)

    await _download_gallery_files(
        parsed_input=ParsedInput(source_url="https://www.pixiv.net/artworks/1"),
        settings=settings,
        work_dir=tmp_path,
        file_dicts=[{"_url": "https://i.pximg.net/img/1_p0.jpg", "extension": "jpg"}],
        status=SimpleNamespace(),
        download_limiter=__import__("asyncio").Semaphore(1),
    )

    assert captured["headers"] == {"Referer": "https://www.pixiv.net/"}


def test_profile_payload_round_trips_through_the_store(tmp_path: Path) -> None:
    session = make_session(make_settings(tmp_path))
    stored = session.request_store.open(
        RequestType.PIXIV_PROFILE,
        ParsedInput(source_url="https://www.pixiv.net/users/456"),
        payload=PixivProfilePayload(
            user_id="456",
            user_name="artist",
            offsets=[0, 3],
            more=[False],
            preview_ids=[10, 11],
        ),
    )

    reloaded = session.request_store.load(stored.token)

    assert reloaded is not None
    assert reloaded.request_type is RequestType.PIXIV_PROFILE
    payload = reloaded.payload
    assert isinstance(payload, PixivProfilePayload)
    assert payload.offsets == [0, 3]
    assert payload.more == [False]
    assert payload.preview_ids == [10, 11]


def make_status():
    return SimpleNamespace(fail=AsyncMock(), edit=AsyncMock())


@pytest.mark.asyncio
async def test_intake_sends_a_profile_to_the_browser(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from routers.intake import _handle_pixiv

    session = make_session(make_pixiv_settings(tmp_path))
    opened = AsyncMock()
    monkeypatch.setattr("routers.intake.pixiv_flow.open_profile", opened)
    status = make_status()
    message = make_message()
    message.bot = object()

    handled = await _handle_pixiv(
        session,
        message,
        ParsedInput(source_url="https://www.pixiv.net/users/456"),
        status,
    )

    assert handled is True
    assert opened.await_count == 1


@pytest.mark.asyncio
async def test_intake_lets_artwork_links_use_the_gallery_path(
    tmp_path: Path,
) -> None:
    from routers.intake import _handle_pixiv

    session = make_session(make_pixiv_settings(tmp_path))
    status = make_status()

    handled = await _handle_pixiv(
        session,
        make_message(),
        ParsedInput(source_url="https://www.pixiv.net/artworks/1"),
        status,
    )

    assert handled is False
    status.fail.assert_not_awaited()


@pytest.mark.asyncio
async def test_intake_rejects_unsupported_pixiv_links(tmp_path: Path) -> None:
    from routers.intake import _handle_pixiv

    session = make_session(make_pixiv_settings(tmp_path))
    status = make_status()

    handled = await _handle_pixiv(
        session,
        make_message(),
        ParsedInput(source_url="https://www.pixiv.net/ranking.php?mode=daily"),
        status,
    )

    assert handled is True
    assert status.fail.await_args.args[0] == text.PIXIV_UNSUPPORTED


@pytest.mark.asyncio
async def test_intake_without_a_token_explains_the_setting(tmp_path: Path) -> None:
    from routers.intake import _handle_pixiv

    session = make_session(make_pixiv_settings(tmp_path, pixiv_refresh_token=""))
    status = make_status()

    handled = await _handle_pixiv(
        session,
        make_message(),
        ParsedInput(source_url="https://www.pixiv.net/artworks/1"),
        status,
    )

    assert handled is True
    assert status.fail.await_args.args[0] == text.PIXIV_NOT_CONFIGURED


@pytest.mark.asyncio
async def test_navigation_callback_renders_the_requested_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from routers.callbacks import pixiv_callback

    session = make_session(make_pixiv_settings(tmp_path))
    stored = session.request_store.open(
        RequestType.PIXIV_PROFILE,
        ParsedInput(source_url="https://www.pixiv.net/users/456"),
        payload=PixivProfilePayload(user_id="456", offsets=[0], more=[False]),
    )
    rendered = AsyncMock()
    monkeypatch.setattr("services.pixiv_flow.render_page", rendered)
    query = SimpleNamespace(
        bot=object(),
        answer=AsyncMock(),
        from_user=SimpleNamespace(id=99),
        inline_message_id=None,
        message=make_message(),
    )

    await pixiv_callback(
        query,
        __import__("utils.callbacks", fromlist=["PixivNavCallback"]).PixivNavCallback(
            token=stored.token, page=2, action="next"
        ),
        session,
    )

    assert rendered.await_args.kwargs["page"] == 2
    query.answer.assert_awaited()


@pytest.mark.asyncio
async def test_expired_navigation_token_is_reported(tmp_path: Path) -> None:
    from routers.callbacks import pixiv_callback
    from utils.callbacks import PixivNavCallback

    session = make_session(make_pixiv_settings(tmp_path))
    query = SimpleNamespace(
        bot=object(),
        answer=AsyncMock(),
        from_user=SimpleNamespace(id=99),
        inline_message_id=None,
        message=make_message(),
    )

    await pixiv_callback(
        query,
        PixivNavCallback(token="gone", page=0, action="next"),
        session,
    )

    assert query.answer.await_args.kwargs["show_alert"] is True


@pytest.mark.asyncio
async def test_inline_profile_offers_work_articles_and_a_pager(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from routers.inline import _answer_pixiv

    session = make_session(make_pixiv_settings(tmp_path))
    works = [make_work("3"), make_work("2"), make_work("1", pages=4)]
    monkeypatch.setattr(
        "services.pixiv.resolve_profile_user_id", AsyncMock(return_value="456")
    )
    monkeypatch.setattr(
        "services.pixiv.user_works", AsyncMock(return_value=(works, None))
    )
    query = SimpleNamespace(answer=AsyncMock(), from_user=SimpleNamespace(id=99))

    handled = await _answer_pixiv(
        query,
        ParsedInput(source_url="https://www.pixiv.net/users/456"),
        session,
    )

    assert handled is True
    results = query.answer.await_args.args[0]
    assert len(results) == len(works) + 1
    assert results[-1].id.startswith("pxv:")
    assert "works 1–3" in results[-1].input_message_content.message_text
    for result in results[:-1]:
        assert session.request_store.load(result.id) is not None


@pytest.mark.asyncio
async def test_inline_work_is_a_single_tap_to_download_article(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from routers.inline import _answer_pixiv

    session = make_session(make_pixiv_settings(tmp_path))
    monkeypatch.setattr(
        "services.pixiv.work", AsyncMock(return_value=make_work("7", pages=3))
    )
    query = SimpleNamespace(answer=AsyncMock(), from_user=SimpleNamespace(id=99))

    await _answer_pixiv(
        query,
        ParsedInput(source_url="https://www.pixiv.net/artworks/7"),
        session,
    )

    results = query.answer.await_args.args[0]
    assert len(results) == 1
    assert results[0].description == "artist · 3 p."
    stored = session.request_store.load(results[0].id)
    assert stored is not None
    assert stored.parsed_input.source_url == "https://www.pixiv.net/artworks/7"


@pytest.mark.asyncio
async def test_inline_rejects_unsupported_pixiv_links(tmp_path: Path) -> None:
    from routers.inline import _answer_pixiv

    session = make_session(make_pixiv_settings(tmp_path))
    query = SimpleNamespace(answer=AsyncMock(), from_user=SimpleNamespace(id=99))

    handled = await _answer_pixiv(
        query,
        ParsedInput(source_url="https://www.pixiv.net/novel/show.php?id=1"),
        session,
    )

    assert handled is True
    assert query.answer.await_args.args[0][0].id == "error"


@pytest.mark.asyncio
async def test_page_text_escapes_titles_for_html_surfaces() -> None:
    payload = PixivProfilePayload(user_name="a&b")
    rendered = pixiv_flow.page_text(payload, 0, [make_work("1")])
    assert "a&amp;b" in rendered
    plain = pixiv_flow.page_text(payload, 0, [make_work("1")], escape=False)
    assert "a&b" in plain


def test_work_metadata_parses_the_app_api_shape() -> None:
    work = pixiv._work_from_json(
        {
            "id": 42,
            "title": "t",
            "type": "ugoira",
            "page_count": 3,
            "user": {"id": 1, "name": "u"},
            "image_urls": {"square_medium": "https://i.pximg.net/thumb.jpg"},
        }
    )

    assert work is not None
    assert work.work_id == "42"
    assert work.cover_url == "https://i.pximg.net/thumb.jpg"
    assert work.artist == "u"
    assert work.pages == 3
    assert work.ugoira is True


def test_missing_cookie_file_is_reported_not_used(tmp_path: Path) -> None:
    settings = make_pixiv_settings(
        tmp_path, pixiv_cookies=str(tmp_path / "absent.txt")
    )
    assert pixiv.cookies_file(settings) is None
    assert _gallery_flags(settings, "https://www.pixiv.net/artworks/1") == []


def test_inline_cookie_file_content_is_written_out(tmp_path: Path) -> None:
    raw = "# Netscape HTTP Cookie File\n.pixiv.net\tTRUE\t/\tTRUE\t0\tPHPSESSID\tabc\n"
    settings = make_pixiv_settings(tmp_path, pixiv_cookies=raw)

    path = pixiv.cookies_file(settings)

    assert path is not None
    assert path.read_text(encoding="utf-8") == raw
    assert os.stat(path).st_size > 0


def make_rendering_bot(tmp_path: Path):
    covers = [tmp_path / f"cover{index}.jpg" for index in range(2)]

    async def fake_covers(_settings, _works, _directory):
        for path in covers:
            path.write_bytes(b"jpg")
        return covers

    bot = SimpleNamespace(
        send_media_group=AsyncMock(
            return_value=[SimpleNamespace(message_id=21), SimpleNamespace(message_id=22)]
        ),
        send_photo=AsyncMock(return_value=SimpleNamespace(message_id=21)),
        delete_message=AsyncMock(),
    )
    return bot, fake_covers


@pytest.mark.asyncio
async def test_render_page_persists_state_replaces_previews_and_shows_buttons(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = make_session(make_pixiv_settings(tmp_path))
    stored = session.request_store.open(
        RequestType.PIXIV_PROFILE,
        ParsedInput(source_url="https://www.pixiv.net/users/456"),
        payload=PixivProfilePayload(
            user_id="456",
            user_name="artist",
            offsets=[0],
            more=[],
            preview_ids=[7],
        ),
    )
    bot, fake_covers = make_rendering_bot(tmp_path)
    monkeypatch.setattr(
        "services.pixiv.user_works",
        AsyncMock(return_value=([make_work("5"), make_work("4")], None)),
    )
    monkeypatch.setattr("services.pixiv.download_covers", fake_covers)
    status = make_status()

    await pixiv_flow.render_page(
        session, bot=bot, stored=stored, page=0, chat_id=500, status=status
    )

    bot.delete_message.assert_awaited_once_with(chat_id=500, message_id=7)
    bot.send_media_group.assert_awaited_once()
    payload = session.request_store.load(stored.token).payload
    assert payload.preview_ids == [21, 22]
    # The next page starts where this one stopped, so works are never split.
    assert payload.offsets == [0, 2]
    assert payload.more == [False, True]
    keyboard = status.edit.await_args.kwargs["reply_markup"]
    labels = [button.text for row in keyboard.inline_keyboard for button in row]
    # One button per Work picks that Work; the artist has no further work, so
    # the next-page arrow is gone.
    assert labels == ["1. work 5", "2. work 4", "◀", "Download 1–2"]
    assert "Works 1–2" in status.edit.await_args.args[0]


@pytest.mark.asyncio
async def test_download_page_delivers_every_work_and_keeps_the_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = make_session(make_pixiv_settings(tmp_path))
    stored = session.request_store.open(
        RequestType.PIXIV_PROFILE,
        ParsedInput(source_url="https://www.pixiv.net/users/456"),
        payload=PixivProfilePayload(user_id="456", offsets=[0]),
    )
    delivered: list[list] = []

    class FakeDelivery:
        def __init__(self, _bot, reply):
            self.reply = reply

        async def deliver(self, artifacts, *, status, started_at):
            del status, started_at
            delivered.append(list(artifacts))
            return []

    async def fake_download(**_kwargs):
        return [SimpleNamespace(path=tmp_path / "a.jpg", file_name="a.jpg")]

    monkeypatch.setattr(
        "services.pixiv.user_works",
        AsyncMock(return_value=([make_work("2", pages=2), make_work("1", pages=3)], None)),
    )
    monkeypatch.setattr("services.pixiv_flow.download_gallery_media", fake_download)
    monkeypatch.setattr("services.pixiv_flow.ChatDelivery", FakeDelivery)
    status = make_status()

    await pixiv_flow.download_page(
        session,
        bot=SimpleNamespace(),
        stored=stored,
        page=0,
        status=status,
        reply=make_message(),
        chat_id=500,
    )

    assert len(delivered) == 2
    assert session.request_store.load(stored.token) is not None


@pytest.mark.asyncio
async def test_work_button_downloads_only_the_chosen_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A title button under the previews picks that one Work, not the page."""
    from routers.callbacks import pixiv_callback
    from utils.callbacks import PixivNavCallback

    session = make_session(make_pixiv_settings(tmp_path))
    stored = session.request_store.open(
        RequestType.PIXIV_PROFILE,
        ParsedInput(source_url="https://www.pixiv.net/users/456"),
        payload=PixivProfilePayload(
            user_id="456", user_name="artist", offsets=[0], more=[False]
        ),
    )
    monkeypatch.setattr(
        "services.pixiv.user_works",
        AsyncMock(return_value=([make_work("5"), make_work("4")], None)),
    )
    downloaded: list[str] = []

    async def fake_download(**kwargs):
        downloaded.append(kwargs["parsed_input"].source_url)
        return [SimpleNamespace(path=tmp_path / "a.jpg", file_name="a.jpg")]

    delivered: list[list] = []

    class FakeDelivery:
        def __init__(self, _bot, reply):
            self.reply = reply

        async def deliver(self, artifacts, *, status, started_at):
            del status, started_at
            delivered.append(list(artifacts))
            return []

    monkeypatch.setattr("services.pixiv_flow.download_gallery_media", fake_download)
    monkeypatch.setattr("services.pixiv_flow.ChatDelivery", FakeDelivery)
    message = SimpleNamespace(
        chat=SimpleNamespace(id=500), edit_text=AsyncMock()
    )
    query = SimpleNamespace(
        from_user=SimpleNamespace(id=99),
        message=message,
        inline_message_id=None,
        answer=AsyncMock(),
        bot=SimpleNamespace(),
    )

    await pixiv_callback(
        query,
        PixivNavCallback(token=stored.token, page=0, action="w1"),
        session,
    )

    assert downloaded == ["https://www.pixiv.net/artworks/4"]
    assert len(delivered) == 1
    keyboard = message.edit_text.await_args.kwargs["reply_markup"]
    labels = [button.text for row in keyboard.inline_keyboard for button in row]
    assert labels == ["1. work 5", "2. work 4", "◀", "Download 1–2"]


@pytest.mark.asyncio
async def test_download_page_rejects_a_work_over_the_file_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = make_session(make_pixiv_settings(tmp_path))
    stored = session.request_store.open(
        RequestType.PIXIV_PROFILE,
        ParsedInput(source_url="https://www.pixiv.net/users/456"),
        payload=PixivProfilePayload(user_id="456", offsets=[0]),
    )
    monkeypatch.setattr(
        "services.pixiv.user_works",
        AsyncMock(return_value=([make_work("9", pages=80)], None)),
    )
    status = make_status()

    await pixiv_flow.download_page(
        session,
        bot=SimpleNamespace(),
        stored=stored,
        page=0,
        status=status,
        reply=make_message(),
        chat_id=500,
    )

    assert "80 pages" in status.fail.await_args.args[0]


@pytest.mark.asyncio
async def test_navigating_past_the_last_work_reports_the_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = make_session(make_pixiv_settings(tmp_path))
    monkeypatch.setattr(
        "services.pixiv.user_works", AsyncMock(return_value=([], None))
    )
    stored = session.request_store.open(
        RequestType.PIXIV_PROFILE,
        ParsedInput(source_url="https://www.pixiv.net/users/456"),
        payload=PixivProfilePayload(user_id="456", offsets=[0, 1], more=[True, False]),
    )
    status = make_status()

    await pixiv_flow.render_page(
        session,
        bot=SimpleNamespace(),
        stored=stored,
        page=1,
        chat_id=500,
        status=status,
    )

    assert status.fail.await_args.args[0] == text.PIXIV_NO_MORE_PAGES


def write_gallery_cache(cache_home: Path, keys: list[str]) -> Path:
    """Create gallery-dl's cache.sqlite3 with the given key rows."""
    import sqlite3

    database = cache_home / "gallery-dl" / "cache.sqlite3"
    database.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database)
    with connection:
        connection.execute(
            "CREATE TABLE data (key TEXT PRIMARY KEY, value TEXT, expires INTEGER)"
        )
        for key in keys:
            connection.execute(
                "INSERT INTO data VALUES (?, ?, 0)", (key, pickle.dumps("refresh-token"))
            )
    connection.close()
    return database


def test_cached_oauth_token_counts_as_credentials(
    tmp_path: Path, isolated_gallery_cache: Path
) -> None:
    settings = make_pixiv_settings(tmp_path, pixiv_refresh_token="")

    assert pixiv.credentials_available(settings) is False

    write_gallery_cache(
        isolated_gallery_cache,
        ["gallery_dl.extractor.pixiv._refresh_token_cache-None"],
    )

    assert pixiv.credentials_available(settings) is True


def test_unrelated_cache_keys_do_not_count_as_credentials(
    tmp_path: Path, isolated_gallery_cache: Path
) -> None:
    write_gallery_cache(
        isolated_gallery_cache, ["gallery_dl.extractor.twitter._guest_token"]
    )
    settings = make_pixiv_settings(tmp_path, pixiv_refresh_token="")

    assert pixiv.has_cached_token() is False
    assert pixiv.credentials_available(settings) is False


def test_a_broken_cache_file_does_not_raise(tmp_path: Path) -> None:
    database = tmp_path / "cache.sqlite3"
    database.write_text("not a database", encoding="utf-8")

    assert pixiv.has_cached_token(database) is False


@pytest.mark.asyncio
async def test_cached_token_lets_an_artwork_link_through(
    tmp_path: Path, isolated_gallery_cache: Path
) -> None:
    from routers.intake import _handle_pixiv

    write_gallery_cache(
        isolated_gallery_cache,
        ["gallery_dl.extractor.pixiv._refresh_token_cache-None"],
    )
    session = make_session(make_pixiv_settings(tmp_path, pixiv_refresh_token=""))
    status = make_status()

    handled = await _handle_pixiv(
        session,
        make_message(),
        ParsedInput(source_url="https://www.pixiv.net/artworks/1"),
        status,
    )

    assert handled is False
    status.fail.assert_not_awaited()


def test_gallery_config_omits_ugoira_overrides(tmp_path: Path) -> None:
    """Ugoira conversion is a download flag; the config only carries auth."""
    settings = make_pixiv_settings(tmp_path)

    with pixiv.gallery_config(settings) as flags:
        payload = json.loads(Path(flags[1]).read_text(encoding="utf-8"))

    assert payload == {"extractor": {"pixiv": {"refresh-token": "refresh-token-value"}}}


def test_ugoira_archive_is_never_offered_as_a_url_send() -> None:
    """Pixiv ugoira arrives as a frames archive; only the local download
    converts it to MP4, so the CDN path must not send the ZIP."""
    from services.gallery import gallery_url_items

    archive = {
        "extension": "zip",
        "filename": "150571036_ugoira1920x1080",
        "_url": "https://i.pximg.net/img-zip-ugoira/1.zip",
    }

    assert gallery_url_items([archive], None, ParsedInput(source_url="https://www.pixiv.net/artworks/1")) == []
    kept = gallery_url_items([archive], None, ParsedInput(source_url="https://example.com/post/1"))
    assert len(kept) == 1
    assert kept[0]["send_type"] == "document"


def test_pixiv_images_still_use_the_url_send_path() -> None:
    from services.gallery import gallery_url_items

    page = {
        "extension": "jpg",
        "filename": "1_p0",
        "_url": "https://i.pximg.net/img-original/1_p0.jpg",
    }

    items = gallery_url_items([page], None, ParsedInput(source_url="https://www.pixiv.net/artworks/1"))

    assert len(items) == 1
    assert items[0]["send_type"] == "photo"


def test_run_command_falls_back_to_the_venv_tool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bot started as `.venv/bin/python bot.py` without an activated venv
    has no `gallery-dl` on PATH; the venv's own copy must be used instead."""
    from services import proc

    venv_bin = tmp_path / "venv" / "bin"
    venv_bin.mkdir(parents=True)
    tool = venv_bin / "gallery-dl"
    tool.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    monkeypatch.setattr(proc.sys, "executable", str(venv_bin / "python"))

    assert proc._resolve_program("gallery-dl") == str(tool)


def test_run_command_keeps_a_tool_it_cannot_find(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from services import proc

    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    monkeypatch.setattr(proc.sys, "executable", str(tmp_path / "venv" / "bin" / "python"))

    assert proc._resolve_program("gallery-dl") == "gallery-dl"
    assert proc._resolve_program("/usr/bin/gallery-dl") == "/usr/bin/gallery-dl"


@pytest.mark.asyncio
async def test_zip_only_pixiv_probe_skips_the_url_send_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pins the bug where an ugoira Work arrived as a ZIP document: a probe
    that only produced the frames archive must go through the local download,
    which converts it to MP4."""
    from services.executor import execute_request
    from services.gallery import gallery_url_items
    from services.media_cache import MediaCache
    from services.request_store import RequestStore
    from services.status import StatusSurface
    from utils.models import DownloadOption, StoredRequest

    settings = make_settings(tmp_path)
    settings.ensure_directories()
    store = RequestStore(settings.requests_dir, settings.work_dir)
    url = "https://www.pixiv.net/en/artworks/150571036"
    archive = {
        "extension": "zip",
        "filename": "150571036_ugoira1920x1080",
        "_url": "https://i.pximg.net/img-zip-ugoira/1.zip",
    }
    stored = StoredRequest(
        token="tok-pixiv",
        request_type="gallery_media",
        parsed_input=ParsedInput(source_url=url),
        options=[
            DownloadOption(option_id="gallery_all", label="Media", send_type="photo")
        ],
        info={"url_media": gallery_url_items([archive], None, ParsedInput(source_url=url))},
    )
    url_send = AsyncMock()
    monkeypatch.setattr("services.executor.send_url_media", url_send)
    artifact = SimpleNamespace(
        path=tmp_path / "video.mp4", file_name="video.mp4", send_type="video",
        caption="c", duration=None, width=None, height=None,
    )
    download = AsyncMock(return_value=[artifact])
    monkeypatch.setattr("services.pipeline.download_gallery_media", download)
    deliver = AsyncMock(return_value=[])
    monkeypatch.setattr("services.executor.ChatDelivery.deliver", deliver)

    await execute_request(
        make_session(settings, store, MediaCache(settings.media_cache_file)),
        stored,
        stored.options[0],
        bot=SimpleNamespace(),
        status=StatusSurface(AsyncMock()),
        source_message=SimpleNamespace(chat=SimpleNamespace(id=500)),
        user_id=99,
    )

    url_send.assert_not_awaited()
    download.assert_awaited_once()
    deliver.assert_awaited_once()
    assert deliver.await_args.kwargs["artifacts"][0].send_type == "video"


def app_work(work_id: int, pages: int = 1, kind: str = "illust") -> dict:
    return {
        "id": work_id,
        "title": f"work {work_id}",
        "type": kind,
        "page_count": pages,
        "user": {"id": 1, "name": "artist"},
        "image_urls": {"square_medium": f"https://i.pximg.net/{work_id}.jpg"},
    }


def fake_app(payloads: dict[str, dict]):
    """Stub ``_app_get`` per App API path, recording the params used."""

    async def _app_get(_settings, _client, path, params):
        params.setdefault("_calls", None)
        return payloads[path]

    return _app_get


@pytest.mark.asyncio
async def test_user_works_reads_the_offset_window_and_next_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = make_pixiv_settings(tmp_path)
    seen: dict[str, object] = {}

    async def _app_get(_settings, _client, path, params):
        seen["path"] = path
        seen["params"] = dict(params)
        return {
            "illusts": [app_work(30), app_work(29), app_work(28, kind="ugoira")],
            "next_url": "https://app-api.pixiv.net/v1/user/illusts?offset=13",
        }

    monkeypatch.setattr("services.pixiv._app_get", _app_get)

    works, next_offset = await pixiv.user_works(settings, "456", 10)

    assert seen["path"] == "/v1/user/illusts"
    assert seen["params"]["offset"] == 10
    assert [work.work_id for work in works] == ["30", "29", "28"]
    assert works[2].ugoira is True
    # Three works returned at offset 10 → the next page starts at 13.
    assert next_offset == 13


@pytest.mark.asyncio
async def test_last_page_reports_no_next_offset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = make_pixiv_settings(tmp_path)
    monkeypatch.setattr(
        "services.pixiv._app_get",
        fake_app({"/v1/user/illusts": {"illusts": [app_work(5)]}}),
    )

    works, next_offset = await pixiv.user_works(settings, "456", 0)

    assert len(works) == 1
    assert next_offset is None


@pytest.mark.asyncio
async def test_work_detail_reads_one_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = make_pixiv_settings(tmp_path)
    monkeypatch.setattr(
        "services.pixiv._app_get",
        fake_app({"/v1/illust/detail": {"illust": app_work(7, pages=4)}}),
    )

    work = await pixiv.work(settings, "7")

    assert work.work_id == "7"
    assert work.pages == 4


@pytest.mark.asyncio
async def test_access_token_is_exchanged_once_and_reused(tmp_path: Path) -> None:
    settings = make_pixiv_settings(tmp_path)
    pixiv._access.clear()
    posts: list[dict] = []

    class Response:
        def __init__(self, payload):
            self._payload = payload

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

        async def json(self, **_kwargs):
            return self._payload

    class Client:
        def post(self, url, data=None):
            posts.append({"url": url, "data": dict(data or {})})
            return Response({"response": {"access_token": "AT", "expires_in": 3600}})

    token = await pixiv._access_token(settings, Client())
    again = await pixiv._access_token(settings, Client())

    assert token == again == "AT"
    assert len(posts) == 1
    assert posts[0]["url"] == pixiv.TOKEN_URL
    assert posts[0]["data"]["grant_type"] == "refresh_token"
    assert posts[0]["data"]["refresh_token"] == "refresh-token-value"


@pytest.mark.asyncio
async def test_rejected_login_is_explained(tmp_path: Path) -> None:
    settings = make_pixiv_settings(tmp_path)
    pixiv._access.clear()

    class Response:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

        async def json(self, **_kwargs):
            return {"error": "invalid_grant"}

    class Client:
        def post(self, *_args, **_kwargs):
            return Response()

    with pytest.raises(pixiv.PixivError) as excinfo:
        await pixiv._access_token(settings, Client())

    assert "oauth:pixiv" in str(excinfo.value)


def test_cached_refresh_token_is_read_from_gallery_dl(tmp_path: Path) -> None:
    import sqlite3

    database = tmp_path / "cache.sqlite3"
    connection = sqlite3.connect(database)
    with connection:
        connection.execute(
            "CREATE TABLE data (key TEXT PRIMARY KEY, value TEXT, expires INTEGER)"
        )
        connection.execute(
            "INSERT INTO data VALUES (?, ?, 0)",
            ("gallery_dl.extractor.pixiv._refresh_token_cache-None", pickle.dumps("RT")),
        )
    connection.close()

    assert pixiv._cached_refresh_token(database) == "RT"


def test_profile_page_keyboard_hides_the_next_arrow_on_the_last_page() -> None:
    from utils.keyboards import pixiv_page_keyboard

    last = pixiv_page_keyboard(token="t", page=2, download_label="Download", has_next=False)
    more = pixiv_page_keyboard(token="t", page=0, download_label="Download", has_next=True)

    assert [button.text for row in last.inline_keyboard for button in row] == ["◀", "Download"]
    assert [button.text for row in more.inline_keyboard for button in row] == [
        "◀",
        "Download",
        "▶",
    ]


@pytest.mark.asyncio
async def test_profile_page_downloads_works_concurrently_in_page_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pins the parallel behaviour: every Work of a page starts before any
    of them finishes (like other gallery albums), while delivery keeps the
    page order so each Work keeps its own caption."""
    import asyncio

    session = make_session(make_pixiv_settings(tmp_path))
    stored = session.request_store.open(
        RequestType.PIXIV_PROFILE,
        ParsedInput(source_url="https://www.pixiv.net/users/456"),
        payload=PixivProfilePayload(user_id="456", offsets=[0]),
    )
    barrier = asyncio.Barrier(2)
    started: list[str] = []
    delivered: list[str] = []

    async def fake_download(**kwargs):
        url = kwargs["parsed_input"].source_url
        started.append(url)
        await barrier.wait()  # deadlocks unless both Works run at once
        artifact = SimpleNamespace(
            path=tmp_path / f"{url[-1]}.jpg", file_name=f"{url[-1]}.jpg",
            send_type="photo", caption=url, duration=None, width=None, height=None,
        )
        return [artifact]

    class FakeDelivery:
        def __init__(self, _bot, _reply):
            pass

        async def deliver(self, artifacts, *, status, started_at):
            del status, started_at
            delivered.append(artifacts[0].caption)
            return []

    monkeypatch.setattr(
        "services.pixiv.user_works",
        AsyncMock(return_value=([make_work("2"), make_work("1")], None)),
    )
    monkeypatch.setattr("services.pixiv_flow.download_gallery_media", fake_download)
    monkeypatch.setattr("services.pixiv_flow.ChatDelivery", FakeDelivery)

    await pixiv_flow.download_page(
        session,
        bot=SimpleNamespace(),
        stored=stored,
        page=0,
        status=make_status(),
        reply=make_message(),
        chat_id=500,
    )

    assert sorted(started) == [
        "https://www.pixiv.net/artworks/1",
        "https://www.pixiv.net/artworks/2",
    ]
    assert delivered == [
        "https://www.pixiv.net/artworks/2",
        "https://www.pixiv.net/artworks/1",
    ]
