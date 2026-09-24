from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.types import InputMediaDocument, InputMediaPhoto, InputMediaVideo

from services.telegram_uploads import _media_item, upload_artifacts
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


def make_artifact(path: Path, send_type: str, caption: str = "c") -> DownloadArtifact:
    return DownloadArtifact(
        path=path,
        file_name=path.name,
        send_type=send_type,
        caption=caption,
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
