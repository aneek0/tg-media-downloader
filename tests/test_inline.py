import re
from types import SimpleNamespace
from unittest.mock import AsyncMock

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
from services.inline_flow import InlineDmBlocked, run_inline_download
from services.pipeline import Downloaded
from services.progress import humanbytes
from services.request_store import RequestStore
from tests.conftest import (
    make_media_cache,
    make_session,
    make_settings,
    record_cached_video,
)
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
    await record_cached_video(cache, url)

    query = SimpleNamespace(
        query=url,
        from_user=SimpleNamespace(id=99),
        answer=AsyncMock(),
    )

    await inline_query_handler(query, make_session(settings, store, cache))

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

    await inline_query_handler(query, make_session(settings, store, cache))

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

    await inline_chosen_handler(chosen, make_session(settings, store, cache))

    chosen.bot.edit_message_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_inline_download_records_cache(monkeypatch, tmp_path):
    settings = make_settings(tmp_path)
    settings.ensure_directories()
    store = RequestStore(settings.requests_dir, settings.work_dir)
    cache = make_media_cache(tmp_path)
    url = "https://x.com/a/status/1"
    stored = StoredRequest(
        token="7f737c1839",
        request_type="inline_media",
        parsed_input=ParsedInput(source_url=url),
        options=[],
        info={
            "plan": "gallery",
            "file_dicts": [
                {
                    "_url": "https://cdn/x.mp4",
                    "extension": "mp4",
                    "tweet_id": "1",
                    "num": 1,
                }
            ],
        },
    )
    store.save(stored)

    media = tmp_path / "v.mp4"
    media.write_bytes(b"\x00\x00\x00\x18" + b"m" * 10)
    artifact = DownloadArtifact(
        path=media, file_name="v.mp4", send_type="video", caption=None
    )
    probe_mock = AsyncMock()
    monkeypatch.setattr("services.pipeline.probe_gallery", probe_mock)
    gallery_mock = AsyncMock(return_value=[artifact])
    monkeypatch.setattr("services.pipeline.download_gallery_media", gallery_mock)

    bot = SimpleNamespace(
        edit_message_text=AsyncMock(),
        send_video=AsyncMock(
            return_value=SimpleNamespace(video=SimpleNamespace(file_id="BAAC11"))
        ),
        edit_message_media=AsyncMock(return_value=True),
    )

    await run_inline_download(
        make_session(settings, store, cache),
        bot=bot,
        user_id=99,
        inline_message_id="im1",
        stored=stored,
    )

    gallery_mock.assert_awaited_once()
    assert gallery_mock.await_args.kwargs["file_dicts"]
    probe_mock.assert_not_awaited()
    bot.send_video.assert_awaited_once()
    media = bot.edit_message_media.await_args.kwargs["media"]
    assert media.media == "BAAC11"
    entries = cache.get(url)
    assert entries is not None
    assert entries[0].file_id == "BAAC11"
    assert entries[0].send_type == "video"
    assert store.load("7f737c1839") is None


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
        token="4cd92ea127",
        request_type="inline_media",
        parsed_input=ParsedInput(source_url=url),
        options=[],
    )
    store.save(stored)

    monkeypatch.setattr(
        "services.pipeline.download_direct_file",
        AsyncMock(side_effect=FileTooLargeError("file exceeds upload limit")),
    )

    bot = SimpleNamespace(
        edit_message_text=AsyncMock(),
        edit_message_media=AsyncMock(),
    )

    await run_inline_download(
        make_session(settings, store, cache),
        bot=bot,
        user_id=99,
        inline_message_id="im1",
        stored=stored,
    )

    assert bot.edit_message_text.await_count == 2
    edited_text = bot.edit_message_text.await_args_list[-1].kwargs["text"]
    assert "larger than Telegram allows" in edited_text
    assert humanbytes(settings.max_upload_bytes) in edited_text
    assert store.load("4cd92ea127") is None
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
        token="4cb185ccbd",
        request_type="inline_media",
        parsed_input=ParsedInput(source_url=url),
        options=[],
    )
    store.save(stored)

    monkeypatch.setattr(
        "services.pipeline.probe_gallery",
        AsyncMock(return_value=GalleryProbe([], None, "Unsupported URL: " + url)),
    )
    monkeypatch.setattr(
        "services.pipeline.download_direct_file",
        AsyncMock(side_effect=RuntimeError("<b>&boom</b>")),
    )

    bot = SimpleNamespace(
        edit_message_text=AsyncMock(),
        edit_message_media=AsyncMock(),
    )

    await run_inline_download(
        make_session(settings, store, cache),
        bot=bot,
        user_id=99,
        inline_message_id="im1",
        stored=stored,
    )

    failure_edit = bot.edit_message_text.await_args_list[-1]
    assert "&lt;b&gt;&amp;boom&lt;/b&gt;" in failure_edit.kwargs["text"]
    assert "<b>&boom</b>" not in failure_edit.kwargs["text"]
    assert store.load("4cb185ccbd") is None
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
        token="3aaa0749e9",
        request_type="inline_media",
        parsed_input=ParsedInput(source_url=url),
        options=[],
    )
    store.save(stored)

    monkeypatch.setattr(
        "services.pipeline.download_direct_file",
        AsyncMock(side_effect=RuntimeError("stop after start")),
    )

    bot = SimpleNamespace(
        edit_message_text=AsyncMock(),
        edit_message_media=AsyncMock(),
    )

    await run_inline_download(
        make_session(settings, store, cache),
        bot=bot,
        user_id=99,
        inline_message_id="im1",
        stored=stored,
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
        token="383a874b48",
        request_type="inline_media",
        parsed_input=ParsedInput(source_url=url),
        options=[],
    )
    store.save(stored)

    download_mock = AsyncMock()
    monkeypatch.setattr("services.pipeline.download_direct_file", download_mock)

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
        make_session(settings, store, cache),
        bot=bot,
        user_id=99,
        inline_message_id="im1",
        stored=stored,
    )

    download_mock.assert_not_awaited()
    bot.edit_message_media.assert_not_awaited()
    failure_edit = bot.edit_message_text.await_args_list[-1]
    assert "could not download that link" in failure_edit.kwargs["text"]
    assert store.load("383a874b48") is None
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

    await inline_query_handler(query, make_session(settings, store, cache))

    run_command_mock.assert_awaited_once()
    assert run_command_mock.await_args.kwargs["timeout"] == settings.gallery_probe_timeout
    assert not list((settings.requests_dir).iterdir())



@pytest.mark.asyncio
async def test_inline_twitter_text_post_is_ready_result(monkeypatch, tmp_path):
    """A text-only tweet must never fall through to HTML direct download."""
    settings = make_settings(tmp_path)
    settings.ensure_directories()
    session = make_session(settings)
    monkeypatch.setattr(
        "routers.inline.probe_gallery",
        AsyncMock(return_value=GalleryProbe([], "text-only post", None)),
    )
    query = SimpleNamespace(
        query="https://x.com/a/status/1",
        from_user=SimpleNamespace(id=99),
        answer=AsyncMock(),
    )

    await inline_query_handler(query, session)

    results = query.answer.await_args.args[0]
    assert len(results) == 1
    result = results[0]
    assert isinstance(result, InlineQueryResultArticle)
    assert result.id == "text:0"
    assert result.input_message_content.message_text == "text-only post"
    assert not list(settings.requests_dir.iterdir())

    chosen = SimpleNamespace(
        result_id=result.id,
        inline_message_id="inline-message",
        from_user=SimpleNamespace(id=99),
        bot=SimpleNamespace(edit_message_text=AsyncMock()),
    )
    await inline_chosen_handler(chosen, session)
    chosen.bot.edit_message_text.assert_not_awaited()

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

    await inline_query_handler(query, make_session(settings, store, cache))

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

    await inline_query_handler(query, make_session(settings, store, cache))

    results = query.answer.await_args.args[0]
    assert not any(result.id.startswith("gallery:") for result in results)
    assert [result.id for result in results] == ["photo1"]
    assert not list((settings.requests_dir).iterdir())


@pytest.mark.asyncio
async def test_inline_twitter_long_post_gets_rich_result(monkeypatch, tmp_path):
    """Regression: a tweet longer than the 1024-character photo caption limit
    now also offers one rich result with the full text and its photo."""
    settings = make_settings(tmp_path)
    settings.ensure_directories()
    store = RequestStore(settings.requests_dir, settings.work_dir)
    cache = make_media_cache(tmp_path)

    content = "\n".join(
        f"step {index} https://example.com/{index}" for index in range(80)
    )
    monkeypatch.setattr(
        "routers.inline.probe_gallery",
        AsyncMock(
            return_value=GalleryProbe(
                file_dicts=[{"_url": "https://c/1.jpg", "extension": "jpg"}],
                content=content,
                error=None,
            )
        ),
    )
    query = SimpleNamespace(
        query="https://x.com/a/status/1",
        from_user=SimpleNamespace(id=99),
        answer=AsyncMock(),
    )

    await inline_query_handler(query, make_session(settings, store, cache))

    results = query.answer.await_args.args[0]
    rich = [result for result in results if result.id == "rich"]
    assert len(rich) == 1
    html = rich[0].input_message_content.rich_message.html
    assert html.startswith('<img src="https://c/1.jpg"/>')
    assert "https://example.com/79" in html
    photo = [result for result in results if result.id == "photo1"][0]
    assert len(photo.caption) == 1024

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

    await inline_query_handler(query, make_session(settings, store, cache))

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

    await inline_query_handler(query, make_session(settings, store, cache))

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

    await inline_query_handler(query, make_session(settings, store, cache))

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
        token="ef74bd6050",
        request_type="inline_media",
        parsed_input=ParsedInput(source_url=url),
        options=[],
        info={
            "plan": "gallery",
            "preferred_name": "999_2.mp4",
            "file_dicts": [{"_url": "https://cdn/a.mp4", "extension": "mp4"}],
        },
    )
    store.save(stored)

    def _artifact(name):
        path = tmp_path / name
        path.write_bytes(b"\x00\x00\x00\x18" + b"m" * 10)
        return DownloadArtifact(path=path, file_name=name, send_type="video", caption=None)

    artifacts = [_artifact("999_1.mp4"), _artifact("999_2.mp4")]
    monkeypatch.setattr(
        "services.pipeline.probe_gallery",
        AsyncMock(return_value=GalleryProbe(file_dicts=[{"_url": "u"}], content=None, error=None)),
    )
    monkeypatch.setattr(
        "services.pipeline.download_gallery_media",
        AsyncMock(return_value=artifacts),
    )

    bot = SimpleNamespace(
        edit_message_text=AsyncMock(),
        edit_message_media=AsyncMock(return_value=True),
        send_media_group=AsyncMock(return_value=[]),
        send_video=AsyncMock(
            side_effect=[
                SimpleNamespace(video=SimpleNamespace(file_id="BAAC41")),
                SimpleNamespace(video=SimpleNamespace(file_id="BAAC42")),
            ]
        ),
    )

    await run_inline_download(
        make_session(settings, store, cache),
        bot=bot,
        user_id=99,
        inline_message_id="im1",
        stored=stored,
    )

    media = bot.edit_message_media.await_args.kwargs["media"]
    assert media.media == "BAAC41"
    assert cache.get(url)[0].file_id == "BAAC41"
    assert store.load("ef74bd6050") is None
    assert bot.send_video.await_args_list[0].kwargs["video"].path.name == "999_2.mp4"


@pytest.mark.asyncio
async def test_gallery_nav_callback_edits_media(tmp_path):
    settings = make_settings(tmp_path)
    settings.ensure_directories()
    store = RequestStore(settings.requests_dir, settings.work_dir)

    stored = StoredRequest(
        token="bf06ecb9cc",
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
        query, GalleryNavCallback(token="bf06ecb9cc", index=1),
        make_session(settings, store),
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
    assert [button.callback_data for button in row] == ["gnav:bf06ecb9cc:0", "gnav:bf06ecb9cc:2"]
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
        query, GalleryNavCallback(token="0123456789", index=1),
        make_session(settings, store),
    )

    query.bot.edit_message_media.assert_not_awaited()
    query.answer.assert_awaited_once()
    assert "expired" in query.answer.await_args.args[0].lower()


@pytest.mark.asyncio
async def test_inline_page_url_downloads_via_ytdlp_not_direct(
    monkeypatch, tmp_path
):
    """Page URLs (TikTok, Reddit, …) must go through yt-dlp. The old
    behaviour plain-GET-ed the HTML page and shipped it as a document.
    Telegram rejects new-file uploads on inline edits, so the downloaded
    video must reach the DM first and the edit must reuse that file_id."""
    settings = make_settings(tmp_path)
    settings.ensure_directories()
    store = RequestStore(settings.requests_dir, settings.work_dir)
    cache = make_media_cache(tmp_path)
    url = "https://vt.tiktok.com/ZSbj8LgVN/"
    stored = StoredRequest(
        token="52d3dc50b5",
        request_type="inline_media",
        parsed_input=ParsedInput(source_url=url),
        options=[],
    )
    store.save(stored)

    probe_mock = AsyncMock(return_value={"title": "T", "id": "1", "duration": 9})
    monkeypatch.setattr("services.pipeline.probe_url", probe_mock)

    media = tmp_path / "v.mp4"
    media.parent.mkdir(parents=True, exist_ok=True)
    media.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"v" * 10)
    artifact = DownloadArtifact(
        path=media, file_name="v.mp4", send_type="video", caption=None
    )
    best_mock = AsyncMock(return_value=artifact)
    monkeypatch.setattr("services.pipeline.download_best_quality", best_mock)
    direct_mock = AsyncMock()
    monkeypatch.setattr("services.pipeline.download_direct_file", direct_mock)

    bot = SimpleNamespace(
        edit_message_text=AsyncMock(),
        send_video=AsyncMock(
            return_value=SimpleNamespace(video=SimpleNamespace(file_id="BAAC21"))
        ),
        edit_message_media=AsyncMock(return_value=True),
    )

    await run_inline_download(
        make_session(settings, store, cache),
        bot=bot,
        user_id=99,
        inline_message_id="im1",
        stored=stored,
    )

    probe_mock.assert_awaited_once()
    best_mock.assert_awaited_once()
    direct_mock.assert_not_awaited()
    bot.send_video.assert_awaited_once()
    media = bot.edit_message_media.await_args.kwargs["media"]
    assert media.media == "BAAC21"
    entries = cache.get(url)
    assert entries is not None
    assert entries[0].file_id == "BAAC21"
    assert store.load("52d3dc50b5") is None


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
        token="31c9647d45",
        request_type="inline_media",
        parsed_input=ParsedInput(source_url=url),
        options=[],
    )
    store.save(stored)

    probe_mock = AsyncMock(side_effect=RuntimeError("Unsupported URL"))
    monkeypatch.setattr("services.pipeline.probe_url", probe_mock)

    media = tmp_path / "f.bin"
    media.parent.mkdir(parents=True, exist_ok=True)
    media.write_bytes(b"data")
    artifact = DownloadArtifact(
        path=media, file_name="f.bin", send_type="document", caption=None
    )
    direct_mock = AsyncMock(return_value=artifact)
    monkeypatch.setattr("services.pipeline.download_direct_file", direct_mock)
    best_mock = AsyncMock()
    monkeypatch.setattr("services.pipeline.download_best_quality", best_mock)

    bot = SimpleNamespace(
        edit_message_text=AsyncMock(),
        send_document=AsyncMock(
            return_value=SimpleNamespace(document=SimpleNamespace(file_id="BAAC31"))
        ),
        edit_message_media=AsyncMock(return_value=True),
    )

    await run_inline_download(
        make_session(settings, store, cache),
        bot=bot,
        user_id=99,
        inline_message_id="im1",
        stored=stored,
    )

    probe_mock.assert_awaited_once()
    direct_mock.assert_awaited_once()
    best_mock.assert_not_awaited()
    media = bot.edit_message_media.await_args.kwargs["media"]
    assert media.media == "BAAC31"
    assert cache.get(url)[0].file_id == "BAAC31"
    assert store.load("31c9647d45") is None


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

    await inline_query_handler(query, make_session(settings, store, cache))

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

    await inline_query_handler(query, make_session(settings, store, cache))

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
        token="2fbbcc615a",
        request_type="inline_media",
        parsed_input=ParsedInput(source_url=url),
        options=[],
        info={
            "plan": "gallery",
            "preferred_index": 1,
            "file_dicts": [{"_url": "https://cdn/a.jpg", "extension": "jpg"}],
        },
    )
    store.save(stored)

    def _artifact(name):
        path = tmp_path / name
        path.write_bytes(b"\xff\xd8\xff\xe0" + b"j" * 10)
        return DownloadArtifact(path=path, file_name=name, send_type="photo", caption=None)

    artifacts = [_artifact("a.jpg"), _artifact("b.jpg"), _artifact("c.jpg")]
    monkeypatch.setattr(
        "services.pipeline.probe_gallery",
        AsyncMock(return_value=GalleryProbe(file_dicts=[{"_url": "u"}], content=None, error=None)),
    )
    monkeypatch.setattr(
        "services.pipeline.download_gallery_media",
        AsyncMock(return_value=artifacts),
    )

    bot = SimpleNamespace(
        edit_message_text=AsyncMock(),
        send_photo=AsyncMock(
            return_value=SimpleNamespace(photo=[SimpleNamespace(file_id="PH_B_DM")])
        ),
        edit_message_media=AsyncMock(return_value=True),
        send_media_group=AsyncMock(
            return_value=[
                SimpleNamespace(photo=[SimpleNamespace(file_id="PH_A")]),
                SimpleNamespace(photo=[SimpleNamespace(file_id="PH_C")]),
            ]
        ),
    )

    await run_inline_download(
        make_session(settings, store, cache),
        bot=bot,
        user_id=99,
        inline_message_id="im1",
        stored=stored,
    )

    # The message media must be the DM-uploaded copy of the tapped photo
    # (b.jpg), not artifacts[0] and not a local-file upload.
    media = bot.edit_message_media.await_args.kwargs["media"]
    assert media.media == "PH_B_DM"
    assert bot.send_photo.await_args.kwargs["photo"].path.name == "b.jpg"
    # Remaining photos spill to DM as an album in original order: a.jpg, c.jpg.
    bot.send_media_group.assert_awaited_once()
    dm_media = bot.send_media_group.await_args.kwargs["media"]
    assert [m.media.filename for m in dm_media] == ["a.jpg", "c.jpg"]
    entries = cache.get(url)
    assert entries is not None
    assert entries[0].file_id == "PH_B_DM"
    assert [e.file_id for e in entries[1:]] == ["PH_A", "PH_C"]
    assert store.load("2fbbcc615a") is None

@pytest.mark.asyncio
async def test_run_inline_download_poster_flow_keeps_media_and_uploads_by_file_id(
    monkeypatch, tmp_path
):
    """Pins the big-video poster flow: while the message still holds the
    poster photo no status text may be edited onto it, and the finished
    video is uploaded to the DM first so the inline edit can use its
    file_id (Bot API forbids new-file uploads on inline message edits)."""
    settings = make_settings(tmp_path)
    settings.ensure_directories()
    store = RequestStore(settings.requests_dir, settings.work_dir)
    cache = make_media_cache(tmp_path)
    url = "https://x.com/a/status/1"
    poster = "https://pbs.twimg.com/ext_tw_video/1/pu/ths/1.jpg"
    video_cdn = "https://video.twimg.com/ext_tw_video/1/vid/720p.mp4"
    stored = StoredRequest(
        token="6c69c93a6c",
        request_type="inline_media",
        parsed_input=ParsedInput(source_url=url),
        options=[],
        info={
            "plan": "gallery",
            "file_dicts": [{"_url": video_cdn, "extension": "mp4"}],
            "preferred_name": "999_1.mp4",
            "poster_url": poster,
        },
    )
    store.save(stored)

    path = tmp_path / "999_1.mp4"
    path.write_bytes(b"\x00\x00\x00\x18" + b"m" * 10)
    artifact = DownloadArtifact(
        path=path,
        file_name="999_1.mp4",
        send_type="video",
        caption=None,
        width=720,
        height=1280,
        duration=12,
    )
    monkeypatch.setattr(
        "services.pipeline.probe_gallery",
        AsyncMock(return_value=GalleryProbe(file_dicts=[{"_url": "u"}], content=None, error=None)),
    )
    monkeypatch.setattr(
        "services.pipeline.download_gallery_media",
        AsyncMock(return_value=[artifact]),
    )

    bot = SimpleNamespace(
        edit_message_text=AsyncMock(),
        edit_message_media=AsyncMock(
            return_value=SimpleNamespace(video=SimpleNamespace(file_id="BAAC50"))
        ),
        send_media_group=AsyncMock(return_value=[]),
        send_video=AsyncMock(
            return_value=SimpleNamespace(video=SimpleNamespace(file_id="BAAC51"))
        ),
    )

    await run_inline_download(
        make_session(settings, store, cache),
        bot=bot,
        user_id=99,
        inline_message_id="im1",
        stored=stored,
    )

    # Poster stays on screen: no download/upload status text was edited in.
    bot.edit_message_text.assert_not_awaited()
    # The video reached the DM and the inline edit reused that file_id.
    bot.send_video.assert_awaited_once()
    media = bot.edit_message_media.await_args.kwargs["media"]
    assert media.media == "BAAC51"
    assert (media.width, media.height, media.duration) == (720, 1280, 12)
    assert media.supports_streaming is True
    # Both copies are cached and the local file plus token are gone.
    entries = cache.get(url)
    assert entries is not None
    assert [e.file_id for e in entries] == ["BAAC50", "BAAC51"]
    assert not path.exists()
    assert store.load("6c69c93a6c") is None


@pytest.mark.asyncio
async def test_run_inline_download_poster_flow_still_reports_failure(
    monkeypatch, tmp_path
):
    """A muted (poster) surface must not swallow errors: the poster is
    edited into an explicit failure notice instead of a silent hang."""
    settings = make_settings(tmp_path)
    settings.ensure_directories()
    store = RequestStore(settings.requests_dir, settings.work_dir)
    cache = make_media_cache(tmp_path)
    stored = StoredRequest(
        token="6556af121c",
        request_type="inline_media",
        parsed_input=ParsedInput(source_url="https://x.com/a/status/2"),
        options=[],
        info={
            "plan": "gallery",
            "file_dicts": [{"_url": "https://cdn/v.mp4", "extension": "mp4"}],
            "poster_url": "https://pbs.twimg.com/p/1.jpg",
        },
    )
    store.save(stored)

    path = tmp_path / "999_1.mp4"
    path.write_bytes(b"\x00\x00\x00\x18" + b"m" * 10)
    artifact = DownloadArtifact(
        path=path, file_name="999_1.mp4", send_type="video", caption=None
    )
    monkeypatch.setattr(
        "services.pipeline.probe_gallery",
        AsyncMock(return_value=GalleryProbe(file_dicts=[{"_url": "u"}], content=None, error=None)),
    )
    monkeypatch.setattr(
        "services.pipeline.download_gallery_media",
        AsyncMock(return_value=[artifact]),
    )

    bot = SimpleNamespace(
        edit_message_text=AsyncMock(),
        edit_message_media=AsyncMock(
            side_effect=TelegramBadRequest(method="EditMessageMedia", message="bad webhook response")
        ),
        send_media_group=AsyncMock(return_value=[]),
        send_video=AsyncMock(
            return_value=SimpleNamespace(video=SimpleNamespace(file_id="BAAC52"))
        ),
    )

    await run_inline_download(
        make_session(settings, store, cache),
        bot=bot,
        user_id=99,
        inline_message_id="im1",
        stored=stored,
    )

    notice = bot.edit_message_text.await_args.kwargs["text"]
    assert text.INLINE_FAILED in notice
    assert "bad webhook response" in notice
    assert store.load("6556af121c") is None


def test_inline_dm_blocked_stays_a_runtime_error_for_the_zip_flow():
    """The album ZIP flow only catches RuntimeError, so the refused-DM
    signal must surface there with the open-bot hint."""
    exc = InlineDmBlocked()

    assert isinstance(exc, RuntimeError)
    assert "open the bot and send the link" in str(exc)


@pytest.mark.asyncio
async def test_inline_spillover_note_names_the_bot(monkeypatch, tmp_path):
    """Album spillover in someone else's chat must name the bot: users cannot
    guess which bot the caption means."""
    settings = make_settings(tmp_path)
    settings.ensure_directories()
    store = RequestStore(settings.requests_dir, settings.work_dir)
    cache = make_media_cache(tmp_path)
    stored = StoredRequest(
        token="73f8ad82f4",
        request_type="inline_media",
        parsed_input=ParsedInput(source_url="https://example.com/album.zip"),
        options=[],
    )
    store.save(stored)
    files = []
    for index in range(2):
        path = tmp_path / f"track{index}.flac"
        path.write_bytes(b"fLaC" + b"\0" * 32)
        files.append(
            DownloadArtifact(
                path=path,
                file_name=f"track{index}.flac",
                send_type="audio",
                caption=f"Track {index}",
            )
        )
    monkeypatch.setattr(
        "services.inline_flow.download_artifacts",
        AsyncMock(return_value=Downloaded(files)),
    )
    audio_calls = 0

    async def _send_audio(**_kwargs):
        nonlocal audio_calls
        audio_calls += 1
        if audio_calls == 1:
            return SimpleNamespace(audio=SimpleNamespace(file_id="A1"))
        raise RuntimeError("bot can't initiate conversation")

    bot = SimpleNamespace(
        edit_message_text=AsyncMock(),
        edit_message_media=AsyncMock(return_value=True),
        edit_message_caption=AsyncMock(),
        send_audio=AsyncMock(side_effect=_send_audio),
    )
    session = make_session(settings, store, cache)
    session.identity.mention = "@my_media_bot"

    await run_inline_download(
        session,
        bot=bot,
        user_id=99,
        inline_message_id="im1",
        stored=stored,
    )

    caption = bot.edit_message_caption.await_args.kwargs["caption"]
    assert "@my_media_bot" in caption
    assert "Track 0" in caption


def test_inline_dm_blocked_names_the_bot():
    """The refused-DM fallback carries the bot handle, not just 'the bot'."""
    exc = InlineDmBlocked("@my_media_bot")

    assert "@my_media_bot" in str(exc)

@pytest.mark.asyncio
async def test_inline_tiktok_photo_post_mp3_not_a_photo_result(monkeypatch, tmp_path):
    """Regression: a TikTok photo post probes as image + soundtrack mp3; the
    mp3 must not become an inline photo result or pager page. Telegram
    fetches every URL result at answer time and rejected the whole answer
    with EXTERNAL_MEDIA_NOT_SUPPORTED (client spun forever), and the pager's
    second page failed edit_message_media on the mp3."""
    settings = make_settings(tmp_path)
    settings.ensure_directories()
    store = RequestStore(settings.requests_dir, settings.work_dir)
    cache = make_media_cache(tmp_path)

    file_dicts = [
        {
            "_url": "https://p16-va.tiktokcdn.com/img.jpeg",
            "extension": "jpeg",
            "filename": "717484b7e8ea",
        },
        {
            "_url": "https://v16-webapp.tiktok.com/sound.mp3",
            "extension": "mp3",
            "filename": "оригинальный звук",
        },
    ]
    monkeypatch.setattr(
        "routers.inline.probe_gallery",
        AsyncMock(return_value=GalleryProbe(file_dicts=file_dicts, content=None, error=None)),
    )

    query = SimpleNamespace(
        query="https://vt.tiktok.com/ZSpCSjrDA/",
        from_user=SimpleNamespace(id=99),
        answer=AsyncMock(),
    )

    await inline_query_handler(query, make_session(settings, store, cache))

    results = query.answer.await_args.args[0]
    # One photo only: the mp3 never becomes a photo result nor a pager page.
    assert all(
        getattr(result, "photo_url", None) != "https://v16-webapp.tiktok.com/sound.mp3"
        for result in results
    )
    photo_results = [
        result
        for result in results
        if isinstance(result, InlineQueryResultPhoto)
    ]
    assert len(photo_results) == 1
    assert photo_results[0].photo_url == "https://p16-va.tiktokcdn.com/img.jpeg"
    # A single photo means no pager at all.
    assert not any(result.id.startswith("gallery:") for result in results)
    # The download-on-tap token still carries the whole post (photo + mp3).
    stored = store.load(photo_results[0].id)
    assert stored.request_type == "inline_media"
    assert stored.info["plan"] == "gallery"
    assert len(stored.info["file_dicts"]) == 2
    assert stored.info["preferred_index"] == 0


@pytest.mark.asyncio
async def test_inline_answer_falls_back_when_telegram_rejects(monkeypatch, tmp_path):
    """Regression: EXTERNAL_MEDIA_NOT_SUPPORTED on answerInlineQuery killed
    the handler, so the client spun forever. A tap-to-download article must
    answer the query instead."""
    settings = make_settings(tmp_path)
    settings.ensure_directories()
    store = RequestStore(settings.requests_dir, settings.work_dir)
    cache = make_media_cache(tmp_path)

    file_dicts = [
        {"_url": "https://cdn.example/1.jpeg", "extension": "jpeg", "filename": "1"},
        {"_url": "https://cdn.example/2.jpeg", "extension": "jpeg", "filename": "2"},
    ]
    monkeypatch.setattr(
        "routers.inline.probe_gallery",
        AsyncMock(return_value=GalleryProbe(file_dicts=file_dicts, content="cap", error=None)),
    )

    answer_mock = AsyncMock(
        side_effect=[
            TelegramBadRequest(
                method="answerInlineQuery",
                message="Bad Request: EXTERNAL_MEDIA_NOT_SUPPORTED",
            ),
            None,
        ]
    )
    query = SimpleNamespace(
        query="https://pawchive.pw/fanbox/user/1/post/2",
        from_user=SimpleNamespace(id=99),
        answer=answer_mock,
    )

    await inline_query_handler(query, make_session(settings, store, cache))

    assert answer_mock.await_count == 2
    fallback = answer_mock.await_args_list[1].args[0][0]
    assert isinstance(fallback, InlineQueryResultArticle)
    stored = store.load(fallback.id)
    assert stored is not None
    assert stored.request_type == "inline_media"
    assert stored.info["plan"] == "url"
