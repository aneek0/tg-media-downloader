from types import SimpleNamespace
from unittest.mock import AsyncMock

import json
import re

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import (
    InlineQueryResultCachedVideo,
    InlineQueryResultPhoto,
    InputMediaPhoto,
)

from routers.inline import (
    gallery_nav_callback,
    inline_chosen_handler,
    inline_query_handler,
)
from services.inline_flow import run_inline_download
from services.progress import humanbytes
from services.request_store import RequestStore
from tests.conftest import make_media_cache, make_settings
from utils import text
from utils.callbacks import GalleryNavCallback
from utils.models import (
    CachedMedia,
    DownloadArtifact,
    FileTooLargeError,
    ParsedInput,
    StoredRequest,
)


@pytest.mark.asyncio
async def test_inline_query_cache_hit_answers_cached_results(monkeypatch, tmp_path):
    settings = make_settings(tmp_path)
    settings.ensure_directories()
    store = RequestStore(settings.requests_dir, settings.work_dir)
    cache = make_media_cache(tmp_path)
    url = "https://example.com/file.mp4"
    await cache.record(
        url,
        [CachedMedia(file_id="BAAC9", send_type="video", file_name="f.mp4", caption="c")],
    )

    query = SimpleNamespace(
        query=url,
        from_user=SimpleNamespace(id=99),
        answer=AsyncMock(),
    )

    await inline_query_handler(query, settings, store, cache)

    query.answer.assert_awaited_once()
    result = query.answer.await_args.args[0][0]
    assert isinstance(result, InlineQueryResultCachedVideo)
    assert result.video_file_id == "BAAC9"
    assert result.id == "cached0"
    # no request token was created
    assert not list((settings.requests_dir).iterdir())


@pytest.mark.asyncio
async def test_inline_chosen_skips_cached_ids(tmp_path):
    settings = make_settings(tmp_path)
    settings.ensure_directories()
    store = RequestStore(settings.requests_dir, settings.work_dir)
    cache = make_media_cache(tmp_path)

    chosen = SimpleNamespace(
        result_id="cached0",
        inline_message_id="im1",
        from_user=SimpleNamespace(id=99),
        bot=SimpleNamespace(edit_message_text=AsyncMock()),
    )

    await inline_chosen_handler(chosen, settings, store, cache)

    chosen.bot.edit_message_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_inline_download_records_cache(monkeypatch, tmp_path):
    settings = make_settings(tmp_path)
    settings.ensure_directories()
    store = RequestStore(settings.requests_dir, settings.work_dir)
    cache = make_media_cache(tmp_path)
    url = "https://x.com/a/status/1"
    stored = StoredRequest(
        token="tok-c",
        request_type="inline_media",
        parsed_input=ParsedInput(source_url=url),
        options=[],
    )
    store.save(stored)

    media = tmp_path / "v.mp4"
    media.write_bytes(b"\x00\x00\x00\x18" + b"m" * 10)
    artifact = DownloadArtifact(
        path=media, file_name="v.mp4", send_type="video", caption=None
    )
    monkeypatch.setattr(
        "services.inline_flow.download_gallery_media",
        AsyncMock(return_value=[artifact]),
    )

    bot = SimpleNamespace(
        edit_message_text=AsyncMock(),
        edit_message_media=AsyncMock(
            return_value=SimpleNamespace(video=SimpleNamespace(file_id="BAAC10"))
        ),
    )

    await run_inline_download(
        bot=bot,
        user_id=99,
        inline_message_id="im1",
        stored=stored,
        settings=settings,
        request_store=store,
        media_cache=cache,
    )

    entries = cache.get(url)
    assert entries is not None
    assert entries[0].file_id == "BAAC10"
    assert entries[0].send_type == "video"
    assert store.load("tok-c") is None


@pytest.mark.asyncio
async def test_run_inline_download_too_large_shows_limit_and_deletes_token(
    monkeypatch, tmp_path
):
    settings = make_settings(tmp_path)
    settings.ensure_directories()
    store = RequestStore(settings.requests_dir, settings.work_dir)
    cache = make_media_cache(tmp_path)
    url = "https://example.com/huge.bin"
    stored = StoredRequest(
        token="tok-large",
        request_type="inline_media",
        parsed_input=ParsedInput(source_url=url),
        options=[],
    )
    store.save(stored)

    monkeypatch.setattr(
        "services.inline_flow.download_direct_file",
        AsyncMock(side_effect=FileTooLargeError("file exceeds upload limit")),
    )

    bot = SimpleNamespace(
        edit_message_text=AsyncMock(),
        edit_message_media=AsyncMock(),
    )

    await run_inline_download(
        bot=bot,
        user_id=99,
        inline_message_id="im1",
        stored=stored,
        settings=settings,
        request_store=store,
        media_cache=cache,
    )

    assert bot.edit_message_text.await_count == 2
    edited_text = bot.edit_message_text.await_args_list[-1].kwargs["text"]
    assert "larger than Telegram allows" in edited_text
    assert humanbytes(settings.max_upload_bytes) in edited_text
    assert store.load("tok-large") is None
    bot.edit_message_media.assert_not_awaited()
    assert cache.get(url) is None


@pytest.mark.asyncio
async def test_run_inline_download_generic_failure_escapes_error_text(
    monkeypatch, tmp_path
):
    settings = make_settings(tmp_path)
    settings.ensure_directories()
    store = RequestStore(settings.requests_dir, settings.work_dir)
    cache = make_media_cache(tmp_path)
    url = "https://example.com/boom.mp4"
    stored = StoredRequest(
        token="tok-fail",
        request_type="inline_media",
        parsed_input=ParsedInput(source_url=url),
        options=[],
    )
    store.save(stored)

    monkeypatch.setattr(
        "services.inline_flow.download_direct_file",
        AsyncMock(side_effect=RuntimeError("<b>&boom</b>")),
    )

    bot = SimpleNamespace(
        edit_message_text=AsyncMock(),
        edit_message_media=AsyncMock(),
    )

    await run_inline_download(
        bot=bot,
        user_id=99,
        inline_message_id="im1",
        stored=stored,
        settings=settings,
        request_store=store,
        media_cache=cache,
    )

    failure_edit = bot.edit_message_text.await_args_list[-1]
    assert "&lt;b&gt;&amp;boom&lt;/b&gt;" in failure_edit.kwargs["text"]
    assert "<b>&boom</b>" not in failure_edit.kwargs["text"]
    assert store.load("tok-fail") is None
    assert cache.get(url) is None


@pytest.mark.asyncio
async def test_run_inline_download_escapes_url_in_download_start(
    monkeypatch, tmp_path
):
    settings = make_settings(tmp_path)
    settings.ensure_directories()
    store = RequestStore(settings.requests_dir, settings.work_dir)
    cache = make_media_cache(tmp_path)
    url = 'https://example.com/a?q="x"&amp=1'
    stored = StoredRequest(
        token="tok-esc",
        request_type="inline_media",
        parsed_input=ParsedInput(source_url=url),
        options=[],
    )
    store.save(stored)

    monkeypatch.setattr(
        "services.inline_flow.download_direct_file",
        AsyncMock(side_effect=RuntimeError("stop after start")),
    )

    bot = SimpleNamespace(
        edit_message_text=AsyncMock(),
        edit_message_media=AsyncMock(),
    )

    await run_inline_download(
        bot=bot,
        user_id=99,
        inline_message_id="im1",
        stored=stored,
        settings=settings,
        request_store=store,
        media_cache=cache,
    )

    start_edit = bot.edit_message_text.await_args_list[0]
    assert text.esc(url) in start_edit.kwargs["text"]
    assert url not in start_edit.kwargs["text"]


@pytest.mark.asyncio
async def test_run_inline_download_start_edit_failure_falls_into_error_branch(
    monkeypatch, tmp_path
):
    settings = make_settings(tmp_path)
    settings.ensure_directories()
    store = RequestStore(settings.requests_dir, settings.work_dir)
    cache = make_media_cache(tmp_path)
    url = "https://example.com/file.mp4"
    stored = StoredRequest(
        token="tok-start",
        request_type="inline_media",
        parsed_input=ParsedInput(source_url=url),
        options=[],
    )
    store.save(stored)

    download_mock = AsyncMock()
    monkeypatch.setattr("services.inline_flow.download_direct_file", download_mock)

    class _FakeMethod:
        __name__ = "editMessageText"

    bot = SimpleNamespace(
        edit_message_text=AsyncMock(
            side_effect=[
                TelegramBadRequest(method=_FakeMethod(), message="not modified"),
                None,
            ]
        ),
        edit_message_media=AsyncMock(),
    )

    await run_inline_download(
        bot=bot,
        user_id=99,
        inline_message_id="im1",
        stored=stored,
        settings=settings,
        request_store=store,
        media_cache=cache,
    )

    download_mock.assert_not_awaited()
    bot.edit_message_media.assert_not_awaited()
    failure_edit = bot.edit_message_text.await_args_list[-1]
    assert "could not download that link" in failure_edit.kwargs["text"]
    assert store.load("tok-start") is None
    assert cache.get(url) is None


@pytest.mark.asyncio
async def test_inline_twitter_query_uses_run_command_with_timeout(
    monkeypatch, tmp_path
):
    settings = make_settings(tmp_path)
    settings.ensure_directories()
    store = RequestStore(settings.requests_dir, settings.work_dir)
    cache = make_media_cache(tmp_path)

    probe = '[[3, "https://video.example/1.mp4", {"ext": "mp4"}]]'
    probe_command = ["gallery-dl", "-j", "https://x.com/a/status/1"]
    monkeypatch.setattr(
        "routers.inline._gallery_probe_command", lambda parsed, s: probe_command
    )
    run_command_mock = AsyncMock(return_value=(probe, ""))
    monkeypatch.setattr("routers.inline.run_command", run_command_mock)

    query = SimpleNamespace(
        query="https://x.com/a/status/1",
        from_user=SimpleNamespace(id=99),
        answer=AsyncMock(),
    )

    await inline_query_handler(query, settings, store, cache)

    run_command_mock.assert_awaited_once_with(
        probe_command, timeout=settings.process_max_timeout
    )
    assert not list((settings.requests_dir).iterdir())


@pytest.mark.asyncio
async def test_inline_twitter_gallery_first_result_and_token_stored(
    monkeypatch, tmp_path
):
    settings = make_settings(tmp_path)
    settings.ensure_directories()
    store = RequestStore(settings.requests_dir, settings.work_dir)
    cache = make_media_cache(tmp_path)

    probe = json.dumps(
        [
            [3, "https://video.example/1.jpg", {"ext": "jpg"}],
            [3, "https://video.example/2.jpg", {"ext": "jpg"}],
            [3, "https://video.example/3.jpg", {"ext": "jpg"}],
            [2, {"content": "tweet text"}],
        ]
    )
    probe_command = ["gallery-dl", "-j", "https://x.com/a/status/1"]
    monkeypatch.setattr(
        "routers.inline._gallery_probe_command", lambda parsed, s: probe_command
    )
    run_command_mock = AsyncMock(return_value=(probe, ""))
    monkeypatch.setattr("routers.inline.run_command", run_command_mock)

    query = SimpleNamespace(
        query="https://x.com/a/status/1",
        from_user=SimpleNamespace(id=99),
        answer=AsyncMock(),
    )

    await inline_query_handler(query, settings, store, cache)

    run_command_mock.assert_awaited_once_with(
        probe_command, timeout=settings.process_max_timeout
    )
    query.answer.assert_awaited_once()
    results = query.answer.await_args.args[0]
    first = results[0]
    assert first.id.startswith("gallery:")
    token = first.id.split("gallery:", 1)[1]
    assert re.fullmatch(r"[0-9a-f]{10}", token)
    assert isinstance(first, InlineQueryResultPhoto)
    assert first.photo_url == "https://video.example/1.jpg"
    row = first.reply_markup.inline_keyboard[0]
    assert len(first.reply_markup.inline_keyboard) == 1
    assert len(row) == 2
    assert all(button.callback_data.startswith("gnav:") for button in row)
    assert [result.id for result in results[1:]] == ["photo1", "photo2", "photo3"]

    stored = store.load(token)
    assert stored is not None
    assert stored.request_type == "inline_gallery"
    assert stored.info["photos"] == [
        "https://video.example/1.jpg",
        "https://video.example/2.jpg",
        "https://video.example/3.jpg",
    ]
    assert "https://x.com/a/status/1" in stored.info["caption"]
    assert "tweet text" in stored.info["caption"]


@pytest.mark.asyncio
async def test_inline_twitter_gallery_single_photo_not_created(
    monkeypatch, tmp_path
):
    settings = make_settings(tmp_path)
    settings.ensure_directories()
    store = RequestStore(settings.requests_dir, settings.work_dir)
    cache = make_media_cache(tmp_path)

    probe = json.dumps(
        [
            [3, "https://video.example/1.jpg", {"ext": "jpg"}],
            [2, {"content": "tweet text"}],
        ]
    )
    probe_command = ["gallery-dl", "-j", "https://x.com/a/status/1"]
    monkeypatch.setattr(
        "routers.inline._gallery_probe_command", lambda parsed, s: probe_command
    )
    monkeypatch.setattr(
        "routers.inline.run_command", AsyncMock(return_value=(probe, ""))
    )

    query = SimpleNamespace(
        query="https://x.com/a/status/1",
        from_user=SimpleNamespace(id=99),
        answer=AsyncMock(),
    )

    await inline_query_handler(query, settings, store, cache)

    results = query.answer.await_args.args[0]
    assert not any(result.id.startswith("gallery:") for result in results)
    assert [result.id for result in results] == ["photo1"]
    assert not list((settings.requests_dir).iterdir())


@pytest.mark.asyncio
async def test_gallery_nav_callback_edits_media(tmp_path):
    settings = make_settings(tmp_path)
    settings.ensure_directories()
    store = RequestStore(settings.requests_dir, settings.work_dir)

    stored = StoredRequest(
        token="tok-g",
        request_type="inline_gallery",
        parsed_input=ParsedInput(source_url="https://x.com/a/status/1"),
        options=[],
        info={
            "photos": [
                "https://video.example/1.jpg",
                "https://video.example/2.jpg",
                "https://video.example/3.jpg",
            ],
            "caption": "cap",
        },
    )
    store.save(stored)

    query = SimpleNamespace(
        inline_message_id="im1",
        from_user=SimpleNamespace(id=99),
        bot=SimpleNamespace(edit_message_media=AsyncMock()),
        answer=AsyncMock(),
    )

    await gallery_nav_callback(
        query, GalleryNavCallback(token="tok-g", index=1), store
    )

    edit = query.bot.edit_message_media
    edit.assert_awaited_once()
    kwargs = edit.await_args.kwargs
    assert kwargs["inline_message_id"] == "im1"
    media = kwargs["media"]
    assert isinstance(media, InputMediaPhoto)
    assert media.media == "https://video.example/2.jpg"
    assert media.caption == "cap"
    assert media.parse_mode is None
    row = kwargs["reply_markup"].inline_keyboard[0]
    assert [button.callback_data for button in row] == ["gnav:tok-g:0", "gnav:tok-g:2"]
    query.answer.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_gallery_nav_callback_expired_token_answers_toast(tmp_path):
    settings = make_settings(tmp_path)
    settings.ensure_directories()
    store = RequestStore(settings.requests_dir, settings.work_dir)

    query = SimpleNamespace(
        inline_message_id="im1",
        from_user=SimpleNamespace(id=99),
        bot=SimpleNamespace(edit_message_media=AsyncMock()),
        answer=AsyncMock(),
    )

    await gallery_nav_callback(
        query, GalleryNavCallback(token="unknown", index=1), store
    )

    query.bot.edit_message_media.assert_not_awaited()
    query.answer.assert_awaited_once()
    assert "expired" in query.answer.await_args.args[0].lower()
