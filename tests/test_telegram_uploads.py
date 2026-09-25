from __future__ import annotations

from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.types import InputMediaDocument, InputMediaPhoto, InputMediaVideo

from services.telegram_uploads import _media_item, upload_artifact, upload_artifacts
from tests.conftest import make_message
from utils.models import DownloadArtifact


def write_media(path: Path, kind: str) -> Path:
    if kind == "jpg":
        payload = b"\xff\xd8\xff\xe0" + b"j" * 10
    elif kind == "mp4":
        payload = b"\x00\x00\x00\x18" + b"m" * 10
    else:
        payload = b"PK" + b"z" * 10
    path.write_bytes(payload)
    return path


def make_artifact(
    path: Path,
    send_type: str,
    caption: str = "c",
    duration: int | None = None,
    width: int | None = None,
    height: int | None = None,
) -> DownloadArtifact:
    return DownloadArtifact(
        path=path,
        file_name=path.name,
        send_type=send_type,
        caption=caption,
        duration=duration,
        width=width,
        height=height,
    )


def test_media_item_classifies_by_extension(tmp_path):
    photo = make_artifact(write_media(tmp_path / "a.jpg", "jpg"), "photo")
    video = make_artifact(write_media(tmp_path / "a.mp4", "mp4"), "video")
    document = make_artifact(write_media(tmp_path / "a.zip", "zip"), "document")

    assert isinstance(_media_item(photo, None), InputMediaPhoto)
    assert isinstance(_media_item(video, None), InputMediaVideo)
    assert isinstance(_media_item(document, None), InputMediaDocument)

    assert _media_item(photo, None).caption is None
    assert _media_item(photo, "cap").caption == "cap"


@pytest.mark.asyncio
async def test_upload_artifacts_single_delegates(monkeypatch, tmp_path):
    artifact = make_artifact(write_media(tmp_path / "a.jpg", "jpg"), "photo")
    upload_mock = AsyncMock()
    monkeypatch.setattr("services.telegram_uploads.upload_artifact", upload_mock)

    message = make_message()
    status = SimpleNamespace(edit_text=AsyncMock())

    await upload_artifacts(
        bot=None,
        status_message=status,
        source_message=message,
        artifacts=[artifact],
        started_at=datetime.now(),
    )

    upload_mock.assert_awaited_once()
    assert upload_mock.await_args.kwargs["artifact"] is artifact


@pytest.mark.asyncio
async def test_upload_artifacts_sends_album(tmp_path):
    artifacts = [
        make_artifact(write_media(tmp_path / "1.jpg", "jpg"), "photo"),
        make_artifact(write_media(tmp_path / "2.jpg", "jpg"), "photo"),
        make_artifact(write_media(tmp_path / "3.mp4", "mp4"), "video"),
    ]

    message = make_message()
    status = SimpleNamespace(edit_text=AsyncMock())

    await upload_artifacts(
        bot=None,
        status_message=status,
        source_message=message,
        artifacts=artifacts,
        started_at=datetime.now(),
    )

    message.reply_media_group.assert_awaited_once()
    items = message.reply_media_group.await_args.kwargs["media"]
    assert len(items) == 3
    assert items[0].caption == artifacts[0].caption
    assert items[1].caption is None
    assert items[2].caption is None
    assert isinstance(items[0], InputMediaPhoto)
    assert isinstance(items[1], InputMediaPhoto)
    assert isinstance(items[2], InputMediaVideo)
    assert not any(artifact.path.exists() for artifact in artifacts)


@pytest.mark.asyncio
async def test_upload_artifacts_chunks_over_ten(tmp_path):
    artifacts = [
        make_artifact(write_media(tmp_path / f"{i:02d}.jpg", "jpg"), "photo")
        for i in range(1, 12)
    ]
    artifacts.append(
        make_artifact(write_media(tmp_path / "12.mp4", "mp4"), "video")
    )

    message = make_message()
    status = SimpleNamespace(edit_text=AsyncMock())

    await upload_artifacts(
        bot=None,
        status_message=status,
        source_message=message,
        artifacts=artifacts,
        started_at=datetime.now(),
    )

    assert message.reply_media_group.await_count == 2
    first_call, second_call = message.reply_media_group.await_args_list
    first_items = first_call.kwargs["media"]
    second_items = second_call.kwargs["media"]
    assert len(first_items) == 10
    assert len(second_items) == 2
    assert first_items[0].caption == artifacts[0].caption
    assert all(item.caption is None for item in first_items[1:])
    assert all(item.caption is None for item in second_items)
    assert not any(artifact.path.exists() for artifact in artifacts)


@pytest.mark.asyncio
async def test_upload_artifact_returns_cached_media(monkeypatch, tmp_path):
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _ctx(**_: object):
        yield None

    class _NoAction:
        upload_photo = staticmethod(lambda **kw: _ctx(**kw))
        upload_document = staticmethod(lambda **kw: _ctx(**kw))
        upload_video = staticmethod(lambda **kw: _ctx(**kw))
        upload_video_note = staticmethod(lambda **kw: _ctx(**kw))
        upload_audio = staticmethod(lambda **kw: _ctx(**kw))

    monkeypatch.setattr("services.telegram_uploads.ChatActionSender", _NoAction)

    artifact = make_artifact(write_media(tmp_path / "a.jpg", "jpg"), "photo")
    message = make_message()
    message.reply_photo = AsyncMock(
        return_value=SimpleNamespace(photo=[SimpleNamespace(file_id="AgAC7")])
    )
    status = SimpleNamespace(edit_text=AsyncMock())

    result = await upload_artifact(
        bot=None,
        status_message=status,
        source_message=message,
        artifact=artifact,
        thumbnail_path=None,
        started_at=datetime.now(),
    )

    assert result is not None
    assert result.file_id == "AgAC7"
    assert result.send_type == "photo"
    assert result.file_name == "a.jpg"
    assert result.caption == "c"


@pytest.mark.asyncio
async def test_upload_artifacts_album_returns_cached_media(tmp_path):
    artifacts = [
        make_artifact(write_media(tmp_path / "1.jpg", "jpg"), "photo"),
        make_artifact(write_media(tmp_path / "2.jpg", "jpg"), "photo"),
    ]
    message = make_message()
    message.reply_media_group = AsyncMock(
        return_value=[
            SimpleNamespace(photo=[SimpleNamespace(file_id=f"AgAC{i}")])
            for i in (1, 2)
        ]
    )
    status = SimpleNamespace(edit_text=AsyncMock())

    result = await upload_artifacts(
        bot=None,
        status_message=status,
        source_message=message,
        artifacts=artifacts,
        started_at=datetime.now(),
    )

    assert [entry.file_id for entry in result] == ["AgAC1", "AgAC2"]
    assert all(entry.send_type == "photo" for entry in result)


def test_media_item_prefers_artifact_metadata(monkeypatch, tmp_path):
    def fail_metadata(path):
        raise AssertionError("hachoir should not run when artifact fields are set")

    monkeypatch.setattr("services.telegram_uploads.video_metadata", fail_metadata)
    video = make_artifact(
        write_media(tmp_path / "a.mp4", "mp4"),
        "video",
        duration=61,
        width=320,
        height=240,
    )

    item = _media_item(video, None)

    assert isinstance(item, InputMediaVideo)
    assert (item.width, item.height, item.duration) == (320, 240, 61)


def test_media_item_falls_back_to_metadata(monkeypatch, tmp_path):
    video = make_artifact(write_media(tmp_path / "a.mp4", "mp4"), "video")
    monkeypatch.setattr(
        "services.telegram_uploads.video_metadata", lambda path: (1280, 720, 42)
    )

    item = _media_item(video, None)

    assert isinstance(item, InputMediaVideo)
    assert (item.width, item.height, item.duration) == (1280, 720, 42)


@pytest.mark.asyncio
async def test_upload_artifact_prefers_artifact_metadata(monkeypatch, tmp_path):
    from contextlib import asynccontextmanager

    calls = []

    async def fail_duration(path):
        raise AssertionError("hachoir should not run when artifact fields are set")

    @asynccontextmanager
    async def _ctx(**_: object):
        yield None

    class _NoAction:
        upload_video = staticmethod(lambda **kw: _ctx(**kw))
        upload_document = staticmethod(lambda **kw: _ctx(**kw))
        upload_video_note = staticmethod(lambda **kw: _ctx(**kw))
        upload_audio = staticmethod(lambda **kw: _ctx(**kw))
        upload_photo = staticmethod(lambda **kw: _ctx(**kw))

    monkeypatch.setattr("services.telegram_uploads.ChatActionSender", _NoAction)
    monkeypatch.setattr("services.telegram_uploads.video_metadata", fail_duration)
    monkeypatch.setattr("services.telegram_uploads.audio_duration", fail_duration)
    monkeypatch.setattr("services.telegram_uploads.video_note_metadata", fail_duration)

    artifact = make_artifact(
        write_media(tmp_path / "a.mp4", "mp4"),
        "video",
        duration=61,
        width=320,
        height=240,
    )
    message = make_message()
    message.reply_video = AsyncMock(
        return_value=SimpleNamespace(video=SimpleNamespace(file_id="AgAV1"))
    )
    status = SimpleNamespace(edit_text=AsyncMock())

    result = await upload_artifact(
        bot=None,
        status_message=status,
        source_message=message,
        artifact=artifact,
        thumbnail_path=None,
        started_at=datetime.now(),
    )

    kwargs = message.reply_video.await_args.kwargs
    assert (kwargs["width"], kwargs["height"], kwargs["duration"]) == (320, 240, 61)
    assert result is not None
    assert result.file_id == "AgAV1"


@pytest.mark.asyncio
async def test_upload_artifact_falls_back_to_metadata(monkeypatch, tmp_path):
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _ctx(**_: object):
        yield None

    class _NoAction:
        upload_video = staticmethod(lambda **kw: _ctx(**kw))
        upload_document = staticmethod(lambda **kw: _ctx(**kw))
        upload_video_note = staticmethod(lambda **kw: _ctx(**kw))
        upload_audio = staticmethod(lambda **kw: _ctx(**kw))
        upload_photo = staticmethod(lambda **kw: _ctx(**kw))

    monkeypatch.setattr("services.telegram_uploads.ChatActionSender", _NoAction)
    monkeypatch.setattr(
        "services.telegram_uploads.video_metadata", lambda path: (1280, 720, 42)
    )

    artifact = make_artifact(write_media(tmp_path / "a.mp4", "mp4"), "video")
    message = make_message()
    message.reply_video = AsyncMock(
        return_value=SimpleNamespace(video=SimpleNamespace(file_id="AgAV2"))
    )
    status = SimpleNamespace(edit_text=AsyncMock())

    result = await upload_artifact(
        bot=None,
        status_message=status,
        source_message=message,
        artifact=artifact,
        thumbnail_path=None,
        started_at=datetime.now(),
    )

    kwargs = message.reply_video.await_args.kwargs
    assert (kwargs["width"], kwargs["height"], kwargs["duration"]) == (1280, 720, 42)
    assert result is not None
    assert result.file_id == "AgAV2"


@pytest.mark.asyncio
async def test_upload_artifact_escapes_file_name_in_caption(monkeypatch, tmp_path):
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _ctx(**_: object):
        yield None

    class _NoAction:
        upload_photo = staticmethod(lambda **kw: _ctx(**kw))
        upload_document = staticmethod(lambda **kw: _ctx(**kw))
        upload_video = staticmethod(lambda **kw: _ctx(**kw))
        upload_video_note = staticmethod(lambda **kw: _ctx(**kw))
        upload_audio = staticmethod(lambda **kw: _ctx(**kw))

    monkeypatch.setattr("services.telegram_uploads.ChatActionSender", _NoAction)

    path = write_media(tmp_path / "evil<script>.jpg", "jpg")
    artifact = DownloadArtifact(
        path=path,
        file_name="evil<script>.jpg",
        send_type="photo",
        caption="c",
    )
    message = make_message()
    message.reply_photo = AsyncMock(
        return_value=SimpleNamespace(photo=[SimpleNamespace(file_id="AgAC9")])
    )
    status = SimpleNamespace(edit_text=AsyncMock())

    await upload_artifact(
        bot=None,
        status_message=status,
        source_message=message,
        artifact=artifact,
        thumbnail_path=None,
        started_at=datetime.now(),
    )

    first_edit = status.edit_text.await_args_list[0]
    assert first_edit.args == ("Uploading <b>evil&lt;script&gt;.jpg</b>",)
