from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from aiogram.exceptions import TelegramForbiddenError
from aiogram.types import InputMediaAudio

from services.inline_flow import run_inline_download
from services.mornye import audio_cache_key
from services.pipeline import Downloaded
from services.request_store import RequestStore
from tests.conftest import make_media_cache, make_session, make_settings
from utils.models import CachedMedia, DownloadArtifact, ParsedInput, StoredRequest


@pytest.mark.asyncio
async def test_inline_audio_edits_with_dm_uploaded_file_id(monkeypatch, tmp_path):
    """Inline audio cannot upload twice: DM upload supplies the edit file_id."""
    settings = make_settings(tmp_path)
    settings.ensure_directories()
    store = RequestStore(settings.requests_dir, settings.work_dir)
    stored = StoredRequest(
        token="audio-token",
        request_type="inline_media",
        parsed_input=ParsedInput(source_url="https://www.deezer.com/track/1"),
    )
    store.save(stored)
    audio = tmp_path / "song.flac"
    audio.write_bytes(b"fLaC" + b"\0" * 32)
    artifact = DownloadArtifact(
        path=audio,
        file_name="song.flac",
        send_type="audio",
        caption="Artist — Song",
        duration=181,
        audio_title="Song",
        audio_performer="Artist",
    )
    monkeypatch.setattr(
        "services.inline_flow.download_artifacts",
        AsyncMock(return_value=Downloaded([artifact])),
    )
    bot = SimpleNamespace(
        edit_message_text=AsyncMock(),
        send_audio=AsyncMock(
            return_value=SimpleNamespace(audio=SimpleNamespace(file_id="AUDIO_DM"))
        ),
        edit_message_media=AsyncMock(
            return_value=SimpleNamespace(audio=SimpleNamespace(file_id="AUDIO_INLINE"))
        ),
    )
    await run_inline_download(
        make_session(settings, store, make_media_cache(tmp_path)),
        bot=bot,
        user_id=99,
        inline_message_id="inline-1",
        stored=stored,
    )
    bot.send_audio.assert_awaited_once()
    media = bot.edit_message_media.await_args.kwargs["media"]
    assert isinstance(media, InputMediaAudio)
    assert media.media == "AUDIO_DM"
    assert media.title == "Song"
    assert store.load("audio-token") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("request_type", ["inline_media", "mornye"])
@pytest.mark.parametrize("caption_status", [False, True])
async def test_inline_cached_audio_skips_download(
    monkeypatch, tmp_path, request_type, caption_status
):
    """Regression: a repeated Artist Card/catalogue track must reuse its Telegram file_id."""
    settings = make_settings(tmp_path)
    settings.ensure_directories()
    store = RequestStore(settings.requests_dir, settings.work_dir)
    source_url = "https://www.deezer.com/track/3406923721"
    token = f"cached-{request_type}-{caption_status}"
    stored = StoredRequest(
        token=token,
        request_type=request_type,
        parsed_input=ParsedInput(source_url=source_url),
    )
    store.save(stored)
    cache = make_media_cache(tmp_path)
    await cache.record(
        source_url,
        [CachedMedia(file_id="CACHED_AUDIO", send_type="audio", file_name="song.flac")],
    )
    download = AsyncMock()
    monkeypatch.setattr("services.inline_flow.download_artifacts", download)
    bot = SimpleNamespace(
        edit_message_text=AsyncMock(),
        edit_message_caption=AsyncMock(),
        send_audio=AsyncMock(),
        edit_message_media=AsyncMock(return_value=True),
    )

    await run_inline_download(
        make_session(settings, store, cache),
        bot=bot,
        user_id=123,
        inline_message_id="inline-1",
        stored=stored,
        caption_status=caption_status,
    )

    download.assert_not_awaited()
    bot.send_audio.assert_not_awaited()
    media = bot.edit_message_media.await_args.kwargs["media"]
    assert isinstance(media, InputMediaAudio)
    assert media.media == "CACHED_AUDIO"
    assert store.load(token) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("spillover_fails", [False, True])
async def test_album_get_all_edits_first_track_and_spills_remainder(
    monkeypatch, tmp_path, spillover_fails
):
    """GET ALL keeps the album's first track inline and the remaining tracks in order."""
    settings = make_settings(tmp_path)
    settings.ensure_directories()
    store = RequestStore(settings.requests_dir, settings.work_dir)
    stored = StoredRequest(
        token="album-order",
        request_type="mornye",
        parsed_input=ParsedInput(source_url="https://www.deezer.com/album/1"),
    )
    store.save(stored)
    artifacts = []
    for title in ("Вы", "Мы", "Я"):
        path = tmp_path / f"{title}.flac"
        path.write_bytes(b"fLaC")
        artifacts.append(
            DownloadArtifact(
                path=path,
                file_name=path.name,
                send_type="audio",
                caption=title,
                audio_title=title,
            )
        )
    monkeypatch.setattr(
        "services.inline_flow.download_artifacts",
        AsyncMock(return_value=Downloaded(artifacts)),
    )

    class FakeDelivery:
        calls = []

        def __init__(self, _bot, _user_id):
            pass

        async def deliver(self, items, *, started_at):
            del started_at
            names = [item.file_name for item in items]
            self.calls.append(names)
            if spillover_fails and len(self.calls) == 2:
                raise RuntimeError("DM unavailable")
            return [
                CachedMedia(
                    file_id=f"FILE-{item.file_name}",
                    send_type="audio",
                    file_name=item.file_name,
                )
                for item in items
            ]

    monkeypatch.setattr("services.inline_flow.DirectDelivery", FakeDelivery)
    bot = SimpleNamespace(
        edit_message_text=AsyncMock(),
        edit_message_caption=AsyncMock(),
        edit_message_media=AsyncMock(
            return_value=SimpleNamespace(audio=SimpleNamespace(file_id="INLINE"))
        ),
    )

    await run_inline_download(
        make_session(settings, store, make_media_cache(tmp_path)),
        bot=bot,
        user_id=123,
        inline_message_id="inline-1",
        stored=stored,
    )

    assert FakeDelivery.calls == [["Вы.flac"], ["Мы.flac", "Я.flac"]]
    assert bot.edit_message_media.await_args.kwargs["media"].media == "FILE-Вы.flac"
    if spillover_fails:
        caption = bot.edit_message_caption.await_args.kwargs["caption"]
        assert caption == "Вы\n(+2 more files — open the bot and send the link)"
    else:
        bot.edit_message_caption.assert_not_awaited()


@pytest.mark.asyncio
async def test_inline_audio_without_bot_chat_falls_back_to_open_bot(
    monkeypatch, tmp_path
):
    """Pins the unreachable-DM hang: a user who never opened the bot cannot
    receive the upload an inline edit needs, so the message says where to
    get the file and no further DM send is attempted."""
    settings = make_settings(tmp_path)
    settings.ensure_directories()
    store = RequestStore(settings.requests_dir, settings.work_dir)
    stored = StoredRequest(
        token="no-dm",
        request_type="mornye",
        parsed_input=ParsedInput(source_url="https://www.deezer.com/album/1"),
    )
    store.save(stored)
    artifacts = []
    for title in ("Вы", "Мы"):
        path = tmp_path / f"{title}.flac"
        path.write_bytes(b"fLaC")
        artifacts.append(
            DownloadArtifact(
                path=path,
                file_name=path.name,
                send_type="audio",
                caption=title,
                audio_title=title,
            )
        )
    monkeypatch.setattr(
        "services.inline_flow.download_artifacts",
        AsyncMock(return_value=Downloaded(artifacts)),
    )

    class ForbiddenDelivery:
        """A DM to a user who never started the bot: Telegram refuses it."""

        calls: list[list[str]] = []

        def __init__(self, _bot, _user_id):
            pass

        async def deliver(self, items, *, started_at):
            del started_at
            self.calls.append([item.file_name for item in items])
            raise TelegramForbiddenError(
                method=Mock(),
                message="Forbidden: bot can't initiate conversation with a user",
            )

    monkeypatch.setattr("services.inline_flow.DirectDelivery", ForbiddenDelivery)
    bot = SimpleNamespace(
        edit_message_text=AsyncMock(),
        edit_message_caption=AsyncMock(),
        edit_message_media=AsyncMock(),
    )

    await run_inline_download(
        make_session(settings, store, make_media_cache(tmp_path)),
        bot=bot,
        user_id=123,
        inline_message_id="inline-1",
        stored=stored,
    )

    assert ForbiddenDelivery.calls == [["Вы.flac"]]
    bot.edit_message_media.assert_not_awaited()
    bot.edit_message_caption.assert_not_awaited()
    failed = bot.edit_message_text.await_args.kwargs["text"]
    assert "open the bot and send the link" in failed
    # The remaining tracks are never claimed as sent.
    assert "more files" not in failed
    assert store.load("no-dm") is None


@pytest.mark.asyncio
async def test_inline_audio_cache_keeps_flac_and_mp3_apart(monkeypatch, tmp_path):
    """Pins the mixed-format cache bug: a cached FLAC must not answer an MP3
    preference, and the new MP3 is cached under its own key."""
    settings = make_settings(tmp_path)
    settings.ensure_directories()
    store = RequestStore(settings.requests_dir, settings.work_dir)
    source_url = "https://www.deezer.com/track/3406923721"
    stored = StoredRequest(
        token="format-token",
        request_type="mornye",
        parsed_input=ParsedInput(source_url=source_url),
    )
    store.save(stored)
    cache = make_media_cache(tmp_path)
    await cache.record(
        audio_cache_key(source_url, "lossless", "flac"),
        [
            CachedMedia(
                file_id="FLAC_FILE", send_type="audio", file_name="song.flac"
            )
        ],
    )
    session = make_session(settings, store, cache)
    session.update_preferences(99, audio_format="mp3")
    media = tmp_path / "song.mp3"
    media.write_bytes(b"ID3" + b"\0" * 32)
    artifact = DownloadArtifact(
        path=media,
        file_name="song.mp3",
        send_type="audio",
        caption="Artist — Song",
        duration=181,
        audio_title="Song",
        audio_performer="Artist",
    )
    download = AsyncMock(return_value=Downloaded([artifact]))
    monkeypatch.setattr("services.inline_flow.download_artifacts", download)
    bot = SimpleNamespace(
        edit_message_text=AsyncMock(),
        send_audio=AsyncMock(
            return_value=SimpleNamespace(audio=SimpleNamespace(file_id="MP3_FILE"))
        ),
        edit_message_media=AsyncMock(return_value=True),
    )

    await run_inline_download(
        session,
        bot=bot,
        user_id=99,
        inline_message_id="inline-1",
        stored=stored,
    )

    download.assert_awaited_once()
    assert download.await_args.args[2].audio_format == "mp3"
    flac_entries = cache.get(audio_cache_key(source_url, "lossless", "flac"))
    assert flac_entries is not None
    assert flac_entries[0].file_id == "FLAC_FILE"
    mp3_entries = cache.get(audio_cache_key(source_url, "lossless", "mp3"))
    assert mp3_entries is not None
    assert mp3_entries[0].file_id == "MP3_FILE"
