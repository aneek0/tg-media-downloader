from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from routers.callbacks import request_callback
from routers.commands import about_command, help_command, start_command
from routers.intake import intake_message
from routers.thumbnails import delete_thumbnail, show_thumbnail
from services.cooldown import CooldownManager
from services.request_store import RequestStore
from services.thumbnail_store import ThumbnailStore
from services.ytdlp import (
    _auto_video_selector,
    _is_audio_source,
    _pick_downloaded_file,
    download_best_quality,
)
from tests.conftest import make_message, make_settings
from utils.callbacks import RequestCallback
from utils.models import DownloadArtifact, DownloadOption, ParsedInput, StoredRequest

@pytest.mark.asyncio
async def test_start_help_about_handlers():
    message = make_message()

    await start_command(message)
    await help_command(message)
    await about_command(message)

    assert message.answer.await_count == 3


@pytest.mark.asyncio
async def test_thumbnail_handlers(tmp_path):
    store = ThumbnailStore(tmp_path / "thumbs")
    message = make_message()

    await show_thumbnail(message, store)
    assert message.answer.await_args_list[0].args[0].startswith("You do not have")

    thumbnail = store.path_for_user(message.from_user.id)
    thumbnail.write_bytes(b"jpg")

    await delete_thumbnail(message, store)
    assert message.answer.await_args_list[-1].args[0].startswith("Your thumbnail")


@pytest.mark.asyncio
async def test_intake_message_builds_quick_youtube_keyboard(tmp_path):
    settings = make_settings(tmp_path)
    settings.ensure_directories()
    store = RequestStore(settings.requests_dir, settings.work_dir)
    cooldown = CooldownManager(timeout_seconds=60)
    thumbnails = ThumbnailStore(settings.thumbnails_dir)

    message = make_message()
    message.text = "https://youtu.be/example"
    status_message = SimpleNamespace(edit_text=AsyncMock())
    message.reply.return_value = status_message

    await intake_message(message, settings, cooldown, store, thumbnails)

    message.reply.assert_awaited_once()
    status_message.edit_text.assert_awaited_once()


@pytest.mark.asyncio
async def test_request_callback_uses_stored_request(monkeypatch, tmp_path):
    settings = make_settings(tmp_path)
    settings.ensure_directories()
    store = RequestStore(settings.requests_dir, settings.work_dir)
    thumbnails = ThumbnailStore(settings.thumbnails_dir)
    stored = StoredRequest(
        token="token123",
        request_type="direct_download",
        parsed_input=ParsedInput(source_url="https://example.com/file.mp4"),
        options=[
            DownloadOption(
                option_id="direct_primary",
                label="Send as media",
                send_type="video",
                mode="direct",
                file_ext="mp4",
            )
        ],
        info={"ext": "mp4"},
    )
    store.save(stored)

    artifact = SimpleNamespace(
        path=tmp_path / "video.mp4",
        file_name="video.mp4",
        send_type="video",
        caption="video",
    )
    artifact.path.write_bytes(b"video")
    download_mock = AsyncMock(return_value=artifact)
    upload_mock = AsyncMock()
    monkeypatch.setattr(
        "services.executor.download_direct_file",
        download_mock,
    )
    monkeypatch.setattr(
        "services.executor.upload_artifact",
        upload_mock,
    )

    source_message = SimpleNamespace(chat=SimpleNamespace(id=500))
    status_message = SimpleNamespace(
        edit_text=AsyncMock(),
        reply_to_message=source_message,
    )
    query = SimpleNamespace(
        message=status_message,
        from_user=SimpleNamespace(id=99),
        bot=SimpleNamespace(),
        answer=AsyncMock(),
    )

    await request_callback(
        query,
        RequestCallback(token="token123", action="direct_primary"),
        settings,
        store,
        thumbnails,
    )

    download_mock.assert_awaited_once()
    upload_mock.assert_awaited_once()


def test_pick_downloaded_file_finds_nested_media(tmp_path):
    work_dir = tmp_path / "work"
    nested = work_dir / "nested"
    nested.mkdir(parents=True)
    media = nested / "clip.mp3"
    media.write_bytes(b"audio")
    (work_dir / "info.json").write_text("{}", encoding="utf-8")

    selected = _pick_downloaded_file(work_dir)

    assert selected == media


@pytest.mark.asyncio
async def test_intake_auto_best_downloads_without_keyboard(monkeypatch, tmp_path):
    settings = make_settings(tmp_path)
    settings.auto_best_quality = True
    settings.ensure_directories()
    store = RequestStore(settings.requests_dir, settings.work_dir)
    thumbnails = ThumbnailStore(settings.thumbnails_dir)
    cooldown = CooldownManager(timeout_seconds=60)

    message = make_message()
    message.text = "https://youtu.be/example"
    message.bot = SimpleNamespace()
    status_message = SimpleNamespace(edit_text=AsyncMock())
    message.reply.return_value = status_message

    monkeypatch.setattr(
        "routers.intake.probe_url",
        AsyncMock(return_value={"title": "T", "formats": [{"vcodec": "avc1.64001f"}]}),
    )
    artifact = SimpleNamespace(
        path=tmp_path / "v.mp4", file_name="v.mp4", send_type="video", caption="c"
    )
    download_mock = AsyncMock(return_value=artifact)
    upload_mock = AsyncMock()
    monkeypatch.setattr("services.executor.download_best_quality", download_mock)
    monkeypatch.setattr("services.executor.upload_artifact", upload_mock)

    await intake_message(message, settings, cooldown, store, thumbnails)

    download_mock.assert_awaited_once()
    upload_mock.assert_awaited_once()
    for call in status_message.edit_text.await_args_list:
        assert "reply_markup" not in call.kwargs


@pytest.mark.asyncio
async def test_intake_auto_best_falls_back_to_direct_download(monkeypatch, tmp_path):
    settings = make_settings(tmp_path)
    settings.auto_best_quality = True
    settings.ensure_directories()
    store = RequestStore(settings.requests_dir, settings.work_dir)
    thumbnails = ThumbnailStore(settings.thumbnails_dir)
    cooldown = CooldownManager(timeout_seconds=60)

    message = make_message()
    message.text = "https://example.com/file.mp4"
    message.bot = SimpleNamespace()
    status_message = SimpleNamespace(edit_text=AsyncMock())
    message.reply.return_value = status_message

    monkeypatch.setattr(
        "routers.intake.probe_url",
        AsyncMock(side_effect=RuntimeError("boom")),
    )
    artifact = SimpleNamespace(
        path=tmp_path / "f.mp4", file_name="f.mp4", send_type="document", caption="c"
    )
    direct_mock = AsyncMock(return_value=artifact)
    best_mock = AsyncMock(return_value=artifact)
    upload_mock = AsyncMock()
    monkeypatch.setattr("services.executor.download_direct_file", direct_mock)
    monkeypatch.setattr("services.executor.download_best_quality", best_mock)
    monkeypatch.setattr("services.executor.upload_artifact", upload_mock)

    await intake_message(message, settings, cooldown, store, thumbnails)

    upload_mock.assert_awaited_once()


def test_auto_video_selector_exact_string():
    assert (
        _auto_video_selector(1080)
        == "bv*[height<=1080][ext=mp4]+ba/b[height<=1080]/bv*[height<=1080]+ba/b"
    )
    assert (
        _auto_video_selector(480)
        == "bv*[height<=480][ext=mp4]+ba/b[height<=480]/bv*[height<=480]+ba/b"
    )


def test_is_audio_source_detection():
    assert _is_audio_source({"formats": [{"vcodec": "none", "acodec": "mp4a"}]}) is True
    assert _is_audio_source({"formats": [{"vcodec": "avc1.64001f"}]}) is False
    assert _is_audio_source({"formats": [{"vcodec": "none"}, {"vcodec": "avc1"}]}) is False
    assert _is_audio_source({"vcodec": "none"}) is True


@pytest.mark.asyncio
async def test_download_best_quality_video_command(monkeypatch, tmp_path):
    settings = make_settings(tmp_path)
    parsed = ParsedInput(source_url="https://youtu.be/example")
    media = tmp_path / "work" / "Title [id].mp4"
    media.parent.mkdir(parents=True)
    media.write_bytes(b"x")
    run_mock = AsyncMock(return_value=("", ""))
    monkeypatch.setattr("services.ytdlp._run_command", run_mock)

    artifact = await download_best_quality(
        parsed_input=parsed,
        settings=settings,
        work_dir=tmp_path / "work",
        info={"formats": [{"vcodec": "avc1.64001f"}]},
    )

    command = run_mock.await_args.args[0]
    selector_index = command.index("-f")
    assert (
        command[selector_index + 1]
        == "bv*[height<=1080][ext=mp4]+ba/b[height<=1080]/bv*[height<=1080]+ba/b"
    )
    assert "--embed-subs" in command
    assert artifact.send_type == "video"


@pytest.mark.asyncio
async def test_download_best_quality_audio_command(monkeypatch, tmp_path):
    settings = make_settings(tmp_path)
    parsed = ParsedInput(source_url="https://soundcloud.com/example/track")
    media = tmp_path / "work" / "Track [id].mp3"
    media.parent.mkdir(parents=True)
    media.write_bytes(b"x")
    run_mock = AsyncMock(return_value=("", ""))
    monkeypatch.setattr("services.ytdlp._run_command", run_mock)

    artifact = await download_best_quality(
        parsed_input=parsed,
        settings=settings,
        work_dir=tmp_path / "work",
        info={"formats": [{"vcodec": "none", "acodec": "mp4a"}]},
    )

    command = run_mock.await_args.args[0]
    assert command[command.index("-f") + 1] == "bestaudio"
    assert "--extract-audio" in command
    assert "mp3" in command
    assert "192k" in command
    assert artifact.send_type == "audio"


@pytest.mark.asyncio
async def test_execute_request_gallery_media(monkeypatch, tmp_path):
    from services.executor import execute_request

    settings = make_settings(tmp_path)
    settings.ensure_directories()
    store = RequestStore(settings.requests_dir, settings.work_dir)
    thumbnails = ThumbnailStore(settings.thumbnails_dir)
    stored = StoredRequest(
        token="tok-g",
        request_type="gallery_media",
        parsed_input=ParsedInput(source_url="https://x.com/a/status/1"),
        options=[
            DownloadOption(
                option_id="gallery_all",
                label="Media",
                send_type="photo",
                mode="gallery",
            )
        ],
    )
    f1 = tmp_path / "a_1.jpg"
    f1.write_bytes(b"j" * 4)
    f2 = tmp_path / "a_2.jpg"
    f2.write_bytes(b"j" * 4)
    artifacts = [
        DownloadArtifact(path=f1, file_name="a_1.jpg", send_type="photo", caption="c"),
        DownloadArtifact(path=f2, file_name="a_2.jpg", send_type="photo", caption="c"),
    ]
    download_mock = AsyncMock(return_value=artifacts)
    upload_mock = AsyncMock()
    monkeypatch.setattr("services.executor.download_gallery_media", download_mock)
    monkeypatch.setattr("services.executor.upload_artifacts", upload_mock)

    status_message = SimpleNamespace(edit_text=AsyncMock())
    source_message = SimpleNamespace(chat=SimpleNamespace(id=500))

    await execute_request(
        stored=stored,
        option=stored.options[0],
        bot=None,
        status_message=status_message,
        source_message=source_message,
        user_id=99,
        settings=settings,
        request_store=store,
        thumbnail_store=thumbnails,
    )

    download_mock.assert_awaited_once()
    upload_mock.assert_awaited_once()
    assert upload_mock.await_args.kwargs["artifacts"] == artifacts


@pytest.mark.asyncio
async def test_intake_twitter_routes_to_gallery(monkeypatch, tmp_path):
    settings = make_settings(tmp_path)
    settings.ensure_directories()
    store = RequestStore(settings.requests_dir, settings.work_dir)
    thumbnails = ThumbnailStore(settings.thumbnails_dir)
    cooldown = CooldownManager(timeout_seconds=60)

    message = make_message()
    message.text = "https://x.com/AlterKyon/status/2103014006747439474"
    message.bot = SimpleNamespace()
    status = SimpleNamespace(edit_text=AsyncMock())
    message.reply.return_value = status
    execute_mock = AsyncMock()
    monkeypatch.setattr("routers.intake.execute_request", execute_mock)

    await intake_message(message, settings, cooldown, store, thumbnails)

    execute_mock.assert_awaited_once()
    stored = execute_mock.await_args.kwargs["stored"]
    assert stored.request_type == "gallery_media"
