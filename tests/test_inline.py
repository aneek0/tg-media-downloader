from types import SimpleNamespace
from unittest.mock import AsyncMock

import re

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import (
    InlineQueryResultArticle,
    InlineQueryResultCachedPhoto,
    InlineQueryResultCachedVideo,
    InlineQueryResultPhoto,
    InlineQueryResultVideo,
    InputMediaPhoto,
)

from routers.inline import (
    gallery_nav_callback,
    inline_chosen_handler,
    inline_query_handler,
)
from services.gallery import GalleryProbe
from services.inline_flow import run_inline_download
from services.progress import humanbytes
from services.request_store import RequestStore
from tests.conftest import make_media_cache, make_settings
from utils import text
from utils.callbacks import GalleryNavCallback, InlineCancelCallback
from utils.models import (
    CachedMedia,
    DownloadArtifact,
    FileTooLargeError,
    ParsedInput,
    StoredRequest,
)


@pytest.mark.asyncio
async def test_inline_query_cache_hit_answers_cached_results(tmp_path):
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
async def test_inline_query_cache_hit_with_photos_gets_pager(tmp_path):
    settings = make_settings(tmp_path)
    settings.ensure_directories()
    store = RequestStore(settings.requests_dir, settings.work_dir)
    cache = make_media_cache(tmp_path)
    url = "https://pawchive.pw/fanbox/user/1/post/2"
    await cache.record(
        url,
        [
            CachedMedia(file_id=f"PH{i}", send_type="photo", file_name=f"{i}.jpg", caption="c")
            for i in range(3)
        ],
    )

    query = SimpleNamespace(
        query=url,
        from_user=SimpleNamespace(id=99),
        answer=AsyncMock(),
    )

    await inline_query_handler(query, settings, store, cache)

    results = query.answer.await_args.args[0]
    assert len(results) == 4  # pager + 3 cached photos
    pager = results[0]
    assert isinstance(pager, InlineQueryResultCachedPhoto)
    assert pager.id.startswith("gallery:")
    assert pager.photo_file_id == "PH0"
    row = pager.reply_markup.inline_keyboard[0]
    assert len(row) == 2
    assert all(button.callback_data.startswith("gnav:") for button in row)

    token = pager.id.split("gallery:", 1)[1]
    stored = store.load(token)
    assert stored is not None
    assert stored.request_type == "inline_gallery"
    assert stored.info["photos"] == ["PH0", "PH1", "PH2"]

    assert [result.id for result in results[1:]] == ["cached0", "cached1", "cached2"]


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
        "services.inline_flow.probe_gallery",
        AsyncMock(
            return_value=GalleryProbe(
                file_dicts=[{"_url": "https://cdn/x.mp4", "extension": "mp4"}],
                content=None,
                error=None,
            )
        ),
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
        "services.inline_flow.probe_gallery",
        AsyncMock(return_value=GalleryProbe([], None, "Unsupported URL: " + url)),
    )
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
async def test_inline_twitter_query_uses_gallery_probe_with_timeout(
    monkeypatch, tmp_path
):
    settings = make_settings(tmp_path)
    settings.ensure_directories()
    store = RequestStore(settings.requests_dir, settings.work_dir)
    cache = make_media_cache(tmp_path)

    probe = '[[3, "https://video.example/1.mp4", {"ext": "mp4"}]]'
    monkeypatch.setattr(
        "services.gallery.gallery_supports_url", lambda url: True
    )
    run_command_mock = AsyncMock(return_value=(probe, ""))
    monkeypatch.setattr("services.gallery.run_command", run_command_mock)

    query = SimpleNamespace(
        query="https://x.com/a/status/1",
        from_user=SimpleNamespace(id=99),
        answer=AsyncMock(),
    )

    await inline_query_handler(query, settings, store, cache)

    run_command_mock.assert_awaited_once()
    assert run_command_mock.await_args.kwargs["timeout"] == settings.gallery_probe_timeout
    assert not list((settings.requests_dir).iterdir())


@pytest.mark.asyncio
async def test_inline_twitter_gallery_first_result_and_token_stored(
    monkeypatch, tmp_path
):
    settings = make_settings(tmp_path)
    settings.ensure_directories()
    store = RequestStore(settings.requests_dir, settings.work_dir)
    cache = make_media_cache(tmp_path)

    file_dicts = [
        {"_url": "https://video.example/1.jpg", "ext": "jpg"},
        {"_url": "https://video.example/2.jpg", "ext": "jpg"},
        {"_url": "https://video.example/3.jpg", "ext": "jpg"},
    ]
    monkeypatch.setattr(
        "routers.inline.probe_gallery",
        AsyncMock(return_value=GalleryProbe(file_dicts=file_dicts, content="tweet text", error=None)),
    )

    query = SimpleNamespace(
        query="https://x.com/a/status/1",
        from_user=SimpleNamespace(id=99),
        answer=AsyncMock(),
    )

    await inline_query_handler(query, settings, store, cache)

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

    file_dicts = [
        {"_url": "https://video.example/1.jpg", "ext": "jpg"},
    ]
    monkeypatch.setattr(
        "routers.inline.probe_gallery",
        AsyncMock(return_value=GalleryProbe(file_dicts=file_dicts, content="tweet text", error=None)),
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
async def test_inline_twitter_small_video_is_instant_video_result(
    monkeypatch, tmp_path
):
    """Telegram's URL fetcher pulls video.twimg.com again (verified live) but
    caps around 20MB, so a small video becomes a real InlineQueryResultVideo
    that posts instantly - no download token, no cancel keyboard."""
    settings = make_settings(tmp_path)
    settings.ensure_directories()
    store = RequestStore(settings.requests_dir, settings.work_dir)
    cache = make_media_cache(tmp_path)

    file_dicts = [
        {
            "_url": "https://video.twimg.com/amplify_video/1/vid/x.mp4",
            "extension": "mp4",
            "tweet_id": 999,
            "num": 1,
        }
    ]
    monkeypatch.setattr(
        "routers.inline.probe_gallery",
        AsyncMock(return_value=GalleryProbe(file_dicts=file_dicts, content="tweet text", error=None)),
    )
    # 2MB -> within the instant-video size cap
    monkeypatch.setattr(
        "routers.inline._content_length", AsyncMock(return_value=2 * 1024 * 1024)
    )

    query = SimpleNamespace(
        query="https://x.com/a/status/1",
        from_user=SimpleNamespace(id=99),
        answer=AsyncMock(),
    )

    await inline_query_handler(query, settings, store, cache)

    results = query.answer.await_args.args[0]
    assert len(results) == 1
    result = results[0]
    assert isinstance(result, InlineQueryResultVideo)
    assert result.video_url == "https://video.twimg.com/amplify_video/1/vid/x.mp4"
    assert result.id.startswith("video")
    assert result.caption and "https://x.com/a/status/1" in result.caption
    # instant result: no request token was created
    assert not list((settings.requests_dir).iterdir())


@pytest.mark.asyncio
async def test_inline_twitter_large_video_is_download_article(
    monkeypatch, tmp_path
):
    """Above the URL-fetcher size cap the video falls back to a
    download-on-tap article bound to a request token."""
    settings = make_settings(tmp_path)
    settings.ensure_directories()
    store = RequestStore(settings.requests_dir, settings.work_dir)
    cache = make_media_cache(tmp_path)

    file_dicts = [
        {
            "_url": "https://video.twimg.com/amplify_video/1/vid/x.mp4",
            "extension": "mp4",
            "tweet_id": 999,
            "num": 1,
        }
    ]
    monkeypatch.setattr(
        "routers.inline.probe_gallery",
        AsyncMock(return_value=GalleryProbe(file_dicts=file_dicts, content="tweet text", error=None)),
    )
    # 100MB -> above the cap
    monkeypatch.setattr(
        "routers.inline._content_length", AsyncMock(return_value=100 * 1024 * 1024)
    )

    query = SimpleNamespace(
        query="https://x.com/a/status/1",
        from_user=SimpleNamespace(id=99),
        answer=AsyncMock(),
    )

    await inline_query_handler(query, settings, store, cache)

    results = query.answer.await_args.args[0]
    assert len(results) == 1
    result = results[0]
    assert isinstance(result, InlineQueryResultArticle)
    assert not isinstance(result, InlineQueryResultVideo)
    assert result.title == "Video 1"
    assert result.description == "tweet text"
    assert result.input_message_content.message_text == text.PROCESSING
    assert re.fullmatch(r"[0-9a-f]{10}", result.id)

    row = result.reply_markup.inline_keyboard[0]
    assert len(row) == 1
    assert row[0].text == "Cancel"
    assert InlineCancelCallback.unpack(row[0].callback_data).token == result.id

    stored = store.load(result.id)
    assert stored is not None
    assert stored.request_type == "inline_media"
    assert stored.info["preferred_name"] == "999_1.mp4"


@pytest.mark.asyncio
async def test_inline_twitter_album_keeps_photo_cdn_and_video_article(
    monkeypatch, tmp_path
):
    """Photos stay instant CDN results; videos in the same tweet carry the
    expected download name so the tapped one is picked out after
    download."""
    settings = make_settings(tmp_path)
    settings.ensure_directories()
    store = RequestStore(settings.requests_dir, settings.work_dir)
    cache = make_media_cache(tmp_path)

    file_dicts = [
        {"_url": "https://pbs.twimg.com/media/1.jpg", "extension": "jpg", "tweet_id": 999, "num": 1},
        {"_url": "https://video.twimg.com/vid/1.mp4", "extension": "mp4", "tweet_id": 999, "num": 2},
        {"_url": "https://pbs.twimg.com/media/2.jpg", "extension": "jpg", "tweet_id": 999, "num": 3},
        {"_url": "https://video.twimg.com/vid/2.mp4", "extension": "mp4", "tweet_id": 999, "num": 4},
    ]
    monkeypatch.setattr(
        "routers.inline.probe_gallery",
        AsyncMock(return_value=GalleryProbe(file_dicts=file_dicts, content="tweet text", error=None)),
    )
    # large videos -> articles (the album keeps its download-on-tap path)
    monkeypatch.setattr(
        "routers.inline._content_length", AsyncMock(return_value=100 * 1024 * 1024)
    )

    query = SimpleNamespace(
        query="https://x.com/a/status/1",
        from_user=SimpleNamespace(id=99),
        answer=AsyncMock(),
    )

    await inline_query_handler(query, settings, store, cache)

    results = query.answer.await_args.args[0]
    pager = results[0]
    assert pager.id.startswith("gallery:")
    assert pager.photo_url == "https://pbs.twimg.com/media/1.jpg"
    cdn_photos = [
        result.id
        for result in results
        if isinstance(result, InlineQueryResultPhoto) and not result.id.startswith("gallery:")
    ]
    assert cdn_photos == ["photo1", "photo2"]
    videos = [result for result in results if isinstance(result, InlineQueryResultArticle)]
    assert [result.title for result in videos] == ["Video 1", "Video 2"]

    first = store.load(videos[0].id)
    second = store.load(videos[1].id)
    # gallery-dl names twitter downloads "{tweet_id}_{num}.{extension}":
    # num is probe order, so the tapped video is found after download even
    # though photos and videos interleave.
    assert first.info["preferred_name"] == "999_2.mp4"
    assert second.info["preferred_name"] == "999_4.mp4"

@pytest.mark.asyncio
async def test_run_inline_download_preferred_name_picks_matching_artifact(
    monkeypatch, tmp_path
):
    """Video articles identify the tapped item by expected download name,
    because probe order does not match artifact order in mixed albums."""
    settings = make_settings(tmp_path)
    settings.ensure_directories()
    store = RequestStore(settings.requests_dir, settings.work_dir)
    cache = make_media_cache(tmp_path)
    url = "https://x.com/a/status/1"
    stored = StoredRequest(
        token="tok-name",
        request_type="inline_media",
        parsed_input=ParsedInput(source_url=url),
        options=[],
        info={"preferred_name": "999_2.mp4"},
    )
    store.save(stored)

    def _artifact(name):
        path = tmp_path / name
        path.write_bytes(b"\x00\x00\x00\x18" + b"m" * 10)
        return DownloadArtifact(path=path, file_name=name, send_type="video", caption=None)

    artifacts = [_artifact("999_1.mp4"), _artifact("999_2.mp4")]
    monkeypatch.setattr(
        "services.inline_flow.probe_gallery",
        AsyncMock(return_value=GalleryProbe(file_dicts=[{"_url": "u"}], content=None, error=None)),
    )
    monkeypatch.setattr(
        "services.inline_flow.download_gallery_media",
        AsyncMock(return_value=artifacts),
    )

    bot = SimpleNamespace(
        edit_message_text=AsyncMock(),
        edit_message_media=AsyncMock(
            return_value=SimpleNamespace(video=SimpleNamespace(file_id="BAAC40"))
        ),
        send_media_group=AsyncMock(return_value=[]),
        send_video=AsyncMock(
            return_value=SimpleNamespace(video=SimpleNamespace(file_id="BAAC41"))
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

    media = bot.edit_message_media.await_args.kwargs["media"]
    assert media.media.filename == "999_2.mp4"
    assert cache.get(url)[0].file_id == "BAAC40"
    assert store.load("tok-name") is None


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


@pytest.mark.asyncio
async def test_inline_page_url_downloads_via_ytdlp_not_direct(
    monkeypatch, tmp_path
):
    """Page URLs (TikTok, Reddit, …) must go through yt-dlp. The old
    behaviour plain-GET-ed the HTML page and shipped it as a document."""
    settings = make_settings(tmp_path)
    settings.ensure_directories()
    store = RequestStore(settings.requests_dir, settings.work_dir)
    cache = make_media_cache(tmp_path)
    url = "https://vt.tiktok.com/ZSbj8LgVN/"
    stored = StoredRequest(
        token="tok-tt",
        request_type="inline_media",
        parsed_input=ParsedInput(source_url=url),
        options=[],
    )
    store.save(stored)

    probe_mock = AsyncMock(return_value={"title": "T", "id": "1", "duration": 9})
    monkeypatch.setattr("services.inline_flow.probe_url", probe_mock)

    media = tmp_path / "v.mp4"
    media.parent.mkdir(parents=True, exist_ok=True)
    media.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"v" * 10)
    artifact = DownloadArtifact(
        path=media, file_name="v.mp4", send_type="video", caption=None
    )
    best_mock = AsyncMock(return_value=artifact)
    monkeypatch.setattr("services.inline_flow.download_best_quality", best_mock)
    direct_mock = AsyncMock()
    monkeypatch.setattr("services.inline_flow.download_direct_file", direct_mock)

    bot = SimpleNamespace(
        edit_message_text=AsyncMock(),
        edit_message_media=AsyncMock(
            return_value=SimpleNamespace(video=SimpleNamespace(file_id="BAAC20"))
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

    probe_mock.assert_awaited_once()
    best_mock.assert_awaited_once()
    direct_mock.assert_not_awaited()
    entries = cache.get(url)
    assert entries is not None
    assert entries[0].file_id == "BAAC20"
    assert store.load("tok-tt") is None


@pytest.mark.asyncio
async def test_inline_probe_failure_falls_back_to_direct(monkeypatch, tmp_path):
    """yt-dlp being unable to resolve the URL must not kill the request:
    the direct downloader still gets a chance (direct file links)."""
    settings = make_settings(tmp_path)
    settings.ensure_directories()
    store = RequestStore(settings.requests_dir, settings.work_dir)
    cache = make_media_cache(tmp_path)
    url = "https://example.com/file.bin"
    stored = StoredRequest(
        token="tok-direct",
        request_type="inline_media",
        parsed_input=ParsedInput(source_url=url),
        options=[],
    )
    store.save(stored)

    probe_mock = AsyncMock(side_effect=RuntimeError("Unsupported URL"))
    monkeypatch.setattr("services.inline_flow.probe_url", probe_mock)

    media = tmp_path / "f.bin"
    media.parent.mkdir(parents=True, exist_ok=True)
    media.write_bytes(b"data")
    artifact = DownloadArtifact(
        path=media, file_name="f.bin", send_type="document", caption=None
    )
    direct_mock = AsyncMock(return_value=artifact)
    monkeypatch.setattr("services.inline_flow.download_direct_file", direct_mock)
    best_mock = AsyncMock()
    monkeypatch.setattr("services.inline_flow.download_best_quality", best_mock)

    bot = SimpleNamespace(
        edit_message_text=AsyncMock(),
        edit_message_media=AsyncMock(
            return_value=SimpleNamespace(document=SimpleNamespace(file_id="BAAC30"))
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

    probe_mock.assert_awaited_once()
    direct_mock.assert_awaited_once()
    best_mock.assert_not_awaited()
    assert cache.get(url) is not None
    assert store.load("tok-direct") is None


@pytest.mark.asyncio
async def test_inline_non_twitter_gallery_returns_photo_preview(monkeypatch, tmp_path):
    settings = make_settings(tmp_path)
    settings.ensure_directories()
    store = RequestStore(settings.requests_dir, settings.work_dir)
    cache = make_media_cache(tmp_path)

    file_dicts = [
        {"_url": "https://file.pawchive.pw/data/x.jpeg", "extension": "jpg", "filename": "x"},
    ]
    monkeypatch.setattr(
        "routers.inline.probe_gallery",
        AsyncMock(return_value=GalleryProbe(file_dicts=file_dicts, content="<p>hello</p>", error=None)),
    )

    query = SimpleNamespace(
        query="https://pawchive.pw/fanbox/user/1/post/2",
        from_user=SimpleNamespace(id=99),
        answer=AsyncMock(),
    )

    await inline_query_handler(query, settings, store, cache)

    query.answer.assert_awaited_once()
    results = query.answer.await_args.args[0]
    assert len(results) == 1
    result = results[0]
    assert isinstance(result, InlineQueryResultPhoto)
    # id = request token (10 hex) -> chosen_inline_result starts download
    assert re.fullmatch(r"[0-9a-f]{10}", result.id)
    assert result.thumbnail_url == "https://img.pawchive.pw/thumbnail/data/x.jpeg"
    assert result.photo_url == "https://img.pawchive.pw/thumbnail/data/x.jpeg"
    assert result.caption == "https://pawchive.pw/fanbox/user/1/post/2\nhello"
    assert result.reply_markup is not None
    row = result.reply_markup.inline_keyboard[0]
    assert len(row) == 1
    assert row[0].text == "Cancel"

    stored = store.load(result.id)
    assert stored is not None
    assert stored.request_type == "inline_media"
    assert stored.info["preferred_index"] == 0


@pytest.mark.asyncio
async def test_inline_non_twitter_gallery_multi_photo_pager_and_tokens(monkeypatch, tmp_path):
    settings = make_settings(tmp_path)
    settings.ensure_directories()
    store = RequestStore(settings.requests_dir, settings.work_dir)
    cache = make_media_cache(tmp_path)

    file_dicts = [
        {"_url": f"https://file.pawchive.pw/data/{i}.jpeg", "extension": "jpg", "filename": str(i)}
        for i in range(3)
    ]
    monkeypatch.setattr(
        "routers.inline.probe_gallery",
        AsyncMock(return_value=GalleryProbe(file_dicts=file_dicts, content="cap", error=None)),
    )

    query = SimpleNamespace(
        query="https://pawchive.pw/fanbox/user/1/post/2",
        from_user=SimpleNamespace(id=99),
        answer=AsyncMock(),
    )

    await inline_query_handler(query, settings, store, cache)

    results = query.answer.await_args.args[0]
    assert len(results) == 4  # pager + 3 photo results
    pager = results[0]
    assert pager.id.startswith("gallery:")
    assert pager.photo_url == "https://img.pawchive.pw/thumbnail/data/0.jpeg"
    assert pager.thumbnail_url == "https://img.pawchive.pw/thumbnail/data/0.jpeg"
    pager_token = pager.id.split("gallery:", 1)[1]
    stored_pager = store.load(pager_token)
    assert stored_pager.request_type == "inline_gallery"
    assert stored_pager.info["photos"] == [
        f"https://img.pawchive.pw/thumbnail/data/{i}.jpeg" for i in range(3)
    ]
    for index, result in enumerate(results[1:]):
        assert re.fullmatch(r"[0-9a-f]{10}", result.id)
        assert result.photo_url == f"https://img.pawchive.pw/thumbnail/data/{index}.jpeg"
        stored = store.load(result.id)
        assert stored.request_type == "inline_media"
        assert stored.info["preferred_index"] == index


@pytest.mark.asyncio
async def test_run_inline_download_preferred_index_puts_tapped_photo_first(
    monkeypatch, tmp_path
):
    settings = make_settings(tmp_path)
    settings.ensure_directories()
    store = RequestStore(settings.requests_dir, settings.work_dir)
    cache = make_media_cache(tmp_path)
    url = "https://pawchive.pw/fanbox/user/1/post/2"
    stored = StoredRequest(
        token="tok-idx",
        request_type="inline_media",
        parsed_input=ParsedInput(source_url=url),
        options=[],
        info={"preferred_index": 1},
    )
    store.save(stored)

    def _artifact(name):
        path = tmp_path / name
        path.write_bytes(b"\xff\xd8\xff\xe0" + b"j" * 10)
        return DownloadArtifact(path=path, file_name=name, send_type="photo", caption=None)

    artifacts = [_artifact("a.jpg"), _artifact("b.jpg"), _artifact("c.jpg")]
    monkeypatch.setattr(
        "services.inline_flow.probe_gallery",
        AsyncMock(return_value=GalleryProbe(file_dicts=[{"_url": "u"}], content=None, error=None)),
    )
    monkeypatch.setattr(
        "services.inline_flow.download_gallery_media",
        AsyncMock(return_value=artifacts),
    )

    bot = SimpleNamespace(
        edit_message_text=AsyncMock(),
        edit_message_media=AsyncMock(
            return_value=SimpleNamespace(photo=[SimpleNamespace(file_id="PH_B")])
        ),
        send_media_group=AsyncMock(
            return_value=[
                SimpleNamespace(photo=[SimpleNamespace(file_id="PH_A")]),
                SimpleNamespace(photo=[SimpleNamespace(file_id="PH_C")]),
            ]
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

    # The message media must be the tapped photo (b.jpg), not artifacts[0].
    media = bot.edit_message_media.await_args.kwargs["media"]
    assert media.media.filename == "b.jpg"
    # Remaining photos spill to DM as an album in original order: a.jpg, c.jpg.
    bot.send_media_group.assert_awaited_once()
    dm_media = bot.send_media_group.await_args.kwargs["media"]
    assert [m.media.filename for m in dm_media] == ["a.jpg", "c.jpg"]
    entries = cache.get(url)
    assert entries is not None
    assert entries[0].file_id == "PH_B"
    assert [e.file_id for e in entries[1:]] == ["PH_A", "PH_C"]
    assert store.load("tok-idx") is None
