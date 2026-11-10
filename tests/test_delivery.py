from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.types import (
    FSInputFile,
    InputMediaDocument,
    InputMediaPhoto,
    InputMediaVideo,
)

from services.delivery import (
    ChatDelivery,
    DirectDelivery,
    media_item,
    rich_blocks,
    send_url_media,
)
from services.status import StatusSurface
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
    *,
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


def make_status() -> tuple[SimpleNamespace, StatusSurface]:
    fake = SimpleNamespace(edit_text=AsyncMock())
    return fake, StatusSurface(fake.edit_text)


@asynccontextmanager
async def _no_action(**_: object):
    yield None


class _NoAction:
    upload_photo = staticmethod(_no_action)
    upload_document = staticmethod(_no_action)
    upload_video = staticmethod(_no_action)
    upload_video_note = staticmethod(_no_action)
    upload_audio = staticmethod(_no_action)


@pytest.mark.asyncio
async def test_media_item_uses_artifact_send_type_and_caption(tmp_path):
    photo = make_artifact(write_media(tmp_path / "a.jpg", "jpg"), "photo")
    video = make_artifact(write_media(tmp_path / "a.mp4", "mp4"), "video")
    document = make_artifact(write_media(tmp_path / "a.zip", "zip"), "document")

    def local(artifact):
        return FSInputFile(artifact.path, filename=artifact.file_name)

    document_item = (await media_item(document, None, media=local(document)))[0]
    photo_item = (await media_item(photo, "cap", media=local(photo)))[0]
    assert isinstance(photo_item, InputMediaPhoto)
    assert isinstance(
        (await media_item(video, None, media=local(video)))[0], InputMediaVideo
    )
    assert isinstance(document_item, InputMediaDocument)
    assert photo_item.caption == "cap"


@pytest.mark.asyncio
async def test_media_item_from_a_file_id_never_uploads_a_thumbnail(tmp_path):
    """An inline edit may only reference an existing file_id."""
    audio = make_artifact(write_media(tmp_path / "a.flac", "zip"), "audio")
    audio.audio_title = None

    item, cover = await media_item(audio, "cap", media="FILEID")

    assert cover is None
    assert item.thumbnail is None
    assert item.title == "a.flac"


@pytest.mark.asyncio
async def test_direct_delivery_uses_artifact_file_name(tmp_path):
    """Telegram receives the artifact name rather than its internal disk name."""
    artifact = make_artifact(write_media(tmp_path / "album.zip", "zip"), "document")
    artifact.file_name = "Artist — Album.zip"
    bot = SimpleNamespace(
        send_document=AsyncMock(
            return_value=SimpleNamespace(document=SimpleNamespace(file_id="D1"))
        )
    )

    await DirectDelivery(bot, 99).deliver([artifact], started_at=datetime.now())

    upload = bot.send_document.await_args.kwargs["document"]
    assert upload.filename == "Artist — Album.zip"


@pytest.mark.asyncio
async def test_photo_over_ten_megabytes_is_sent_as_a_file(tmp_path):
    """Telegram rejects photos over 10 MB, so such a photo goes out as a document."""
    path = write_media(tmp_path / "huge.jpg", "jpg")
    with path.open("ab") as handle:
        handle.truncate(10 * 1024 * 1024 + 1)
    artifact = make_artifact(path, "photo")
    bot = SimpleNamespace(
        send_document=AsyncMock(
            return_value=SimpleNamespace(document=SimpleNamespace(file_id="D1"))
        ),
        send_photo=AsyncMock(),
    )

    entries = await DirectDelivery(bot, 99).deliver(
        [artifact], started_at=datetime.now()
    )
    item, _ = await media_item(artifact, None, media="FILEID")

    assert bot.send_photo.await_count == 0
    assert bot.send_document.await_args.kwargs["document"].filename == "huge.jpg"
    assert isinstance(item, InputMediaDocument)
    assert entries[0].send_type == "document"


@pytest.mark.asyncio
async def test_deliver_single_delegates(monkeypatch, tmp_path):
    artifact = make_artifact(write_media(tmp_path / "a.jpg", "jpg"), "photo")
    single_mock = AsyncMock(return_value=None)
    monkeypatch.setattr("services.delivery._send_single", single_mock)

    message = make_message()
    _, status = make_status()

    await ChatDelivery(None, message).deliver(
        [artifact], status=status, started_at=datetime.now()
    )

    single_mock.assert_awaited_once()
    assert single_mock.await_args.args[2] is artifact


@pytest.mark.asyncio
async def test_deliver_sends_album_without_captions(tmp_path):
    artifacts = [
        make_artifact(write_media(tmp_path / "1.jpg", "jpg"), "photo"),
        make_artifact(write_media(tmp_path / "2.jpg", "jpg"), "photo"),
        make_artifact(write_media(tmp_path / "3.mp4", "mp4"), "video"),
    ]

    message = make_message()
    message.reply_media_group = AsyncMock(
        return_value=[
            SimpleNamespace(photo=[SimpleNamespace(file_id="1")]),
            SimpleNamespace(photo=[SimpleNamespace(file_id="2")]),
            SimpleNamespace(video=SimpleNamespace(file_id="3")),
        ]
    )
    _, status = make_status()

    await ChatDelivery(None, message).deliver(
        artifacts, status=status, started_at=datetime.now()
    )

    message.reply_media_group.assert_awaited_once()
    media = message.reply_media_group.await_args.kwargs["media"]
    assert len(media) == 3
    assert all(item.caption is None for item in media)
    assert not any(artifact.path.exists() for artifact in artifacts)


@pytest.mark.asyncio
async def test_deliver_chunks_over_ten(monkeypatch, tmp_path):
    monkeypatch.setattr("services.delivery.ChatActionSender", _NoAction)
    artifacts = [
        make_artifact(write_media(tmp_path / f"{i}.jpg", "jpg"), "photo")
        for i in range(11)
    ]

    message = make_message()
    message.reply_media_group = AsyncMock(
        side_effect=lambda media: [
            SimpleNamespace(photo=[SimpleNamespace(file_id=str(i))])
            for i in range(len(media))
        ]
    )
    message.reply_photo = AsyncMock(
        return_value=SimpleNamespace(photo=[SimpleNamespace(file_id="last")])
    )
    _, status = make_status()

    await ChatDelivery(None, message).deliver(
        artifacts, status=status, started_at=datetime.now()
    )

    assert message.reply_media_group.await_count == 1
    assert len(message.reply_media_group.await_args.kwargs["media"]) == 10
    assert message.reply_photo.await_count == 1
    assert not any(artifact.path.exists() for artifact in artifacts)


@pytest.mark.asyncio
async def test_deliver_returns_cached_media(monkeypatch, tmp_path):
    monkeypatch.setattr("services.delivery.ChatActionSender", _NoAction)

    artifact = make_artifact(write_media(tmp_path / "a.jpg", "jpg"), "photo")
    message = make_message()
    message.reply_photo = AsyncMock(
        return_value=SimpleNamespace(photo=[SimpleNamespace(file_id="AgAC7")])
    )
    _, status = make_status()

    result = await ChatDelivery(None, message).deliver(
        [artifact], status=status, started_at=datetime.now()
    )

    assert len(result) == 1
    assert result[0].file_id == "AgAC7"
    assert result[0].send_type == "photo"
    assert result[0].file_name == "a.jpg"
    assert result[0].caption == "c"




@pytest.mark.asyncio
async def test_deliver_album_returns_cached_media(tmp_path):
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
    _, status = make_status()

    result = await ChatDelivery(None, message).deliver(
        artifacts, status=status, started_at=datetime.now()
    )

    assert [entry.file_id for entry in result] == ["AgAC1", "AgAC2"]
    assert all(entry.send_type == "photo" for entry in result)


@pytest.mark.asyncio
async def test_media_item_prefers_artifact_metadata(monkeypatch, tmp_path):
    def fail_metadata(path):  # noqa: ARG001
        raise AssertionError("hachoir should not run when artifact fields are set")

    monkeypatch.setattr("services.delivery.video_metadata", fail_metadata)
    video = make_artifact(
        write_media(tmp_path / "a.mp4", "mp4"),
        "video",
        duration=61,
        width=320,
        height=240,
    )

    item = (
        await media_item(
            video,
            None,
            media=FSInputFile(video.path, filename=video.file_name),
        )
    )[0]

    assert isinstance(item, InputMediaVideo)
    assert (item.width, item.height, item.duration) == (320, 240, 61)


@pytest.mark.asyncio
async def test_media_item_falls_back_to_metadata(monkeypatch, tmp_path):
    video = make_artifact(write_media(tmp_path / "a.mp4", "mp4"), "video")
    monkeypatch.setattr(
        "services.delivery.video_metadata", lambda path: (1280, 720, 42)
    )

    item = (
        await media_item(
            video,
            None,
            media=FSInputFile(video.path, filename=video.file_name),
        )
    )[0]

    assert isinstance(item, InputMediaVideo)
    assert (item.width, item.height, item.duration) == (1280, 720, 42)


@pytest.mark.asyncio
async def test_single_send_prefers_artifact_metadata(monkeypatch, tmp_path):
    async def fail_duration(path):  # noqa: ARG001
        raise AssertionError("hachoir should not run when artifact fields are set")

    monkeypatch.setattr("services.delivery.ChatActionSender", _NoAction)
    monkeypatch.setattr("services.delivery.video_metadata", fail_duration)
    monkeypatch.setattr("services.delivery.audio_duration", fail_duration)
    monkeypatch.setattr("services.delivery.video_note_metadata", fail_duration)

    video = make_artifact(
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
    _, status = make_status()

    result = await ChatDelivery(None, message).deliver(
        [video], status=status, started_at=datetime.now()
    )

    assert result[0].file_id == "AgAV1"
    sent = message.reply_video.await_args.kwargs
    assert (sent["width"], sent["height"], sent["duration"]) == (320, 240, 61)


@pytest.mark.asyncio
async def test_single_send_falls_back_to_metadata(monkeypatch, tmp_path):
    monkeypatch.setattr("services.delivery.ChatActionSender", _NoAction)
    monkeypatch.setattr(
        "services.delivery.video_metadata", lambda path: (1280, 720, 42)
    )

    video = make_artifact(write_media(tmp_path / "a.mp4", "mp4"), "video")
    message = make_message()
    message.reply_video = AsyncMock(
        return_value=SimpleNamespace(video=SimpleNamespace(file_id="AgAV2"))
    )
    _, status = make_status()

    result = await ChatDelivery(None, message).deliver(
        [video], status=status, started_at=datetime.now()
    )

    assert result[0].file_id == "AgAV2"
    sent = message.reply_video.await_args.kwargs
    assert (sent["width"], sent["height"], sent["duration"]) == (1280, 720, 42)


@pytest.mark.asyncio
async def test_uploading_caption_escapes_file_name(monkeypatch, tmp_path):
    monkeypatch.setattr("services.delivery.ChatActionSender", _NoAction)

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
    fake, status = make_status()

    await ChatDelivery(None, message).deliver(
        [artifact], status=status, started_at=datetime.now()
    )

    first_edit = fake.edit_text.await_args_list[0]
    assert first_edit.args == ("Uploading <b>evil&lt;script&gt;.jpg</b>",)


@pytest.mark.asyncio
async def test_send_url_media_single_photo():
    bot = SimpleNamespace(
        send_photo=AsyncMock(
            return_value=SimpleNamespace(photo=[SimpleNamespace(file_id="P1")])
        )
    )
    result = await send_url_media(
        bot=bot,
        chat_id=5,
        items=[
            {
                "url": "https://c/a.jpg",
                "filename": "a.jpg",
                "send_type": "photo",
                "caption": "cap",
            }
        ],
    )
    assert bot.send_photo.await_args.kwargs["photo"] == "https://c/a.jpg"
    assert bot.send_photo.await_args.kwargs["caption"] == "cap"
    assert result[0].file_id == "P1"
    assert result[0].send_type == "photo"
    assert result[0].file_name == "a.jpg"


@pytest.mark.asyncio
async def test_send_url_media_group_uses_url_items():
    bot = SimpleNamespace(
        send_media_group=AsyncMock(
            return_value=[
                SimpleNamespace(photo=[SimpleNamespace(file_id="P1")]),
                SimpleNamespace(photo=[SimpleNamespace(file_id="P2")]),
            ]
        )
    )
    result = await send_url_media(
        bot=bot,
        chat_id=5,
        items=[
            {
                "url": "https://c/a.jpg",
                "filename": "a.jpg",
                "send_type": "photo",
                "caption": "cap",
            },
            {
                "url": "https://c/b.jpg",
                "filename": "b.jpg",
                "send_type": "photo",
                "caption": None,
            },
        ],
    )
    media = bot.send_media_group.await_args.kwargs["media"]
    assert len(media) == 2
    assert all(isinstance(item, InputMediaPhoto) for item in media)
    assert media[0].media == "https://c/a.jpg"
    assert media[0].caption == "cap"
    assert media[1].media == "https://c/b.jpg"
    assert media[1].caption is None
    assert [entry.file_id for entry in result] == ["P1", "P2"]


@pytest.mark.asyncio
async def test_send_url_media_error_propagates():
    bot = SimpleNamespace(
        send_photo=AsyncMock(
            side_effect=TelegramBadRequest(
                method="sendPhoto", message="WEBPAGE_CURL_FAILED"
            )
        )
    )
    with pytest.raises(TelegramBadRequest):
        await send_url_media(
            bot=bot,
            chat_id=5,
            items=[
                {
                    "url": "https://c/a.jpg",
                    "filename": "a.jpg",
                    "send_type": "photo",
                    "caption": None,
                }
            ],
        )


@pytest.mark.asyncio
async def test_deliver_retries_lost_media_group(tmp_path):
    """Regression: local Bot API dropping the request mid-body upload
    (aiohttp ``Can not write request body``) must be retried once."""
    artifacts = [
        make_artifact(write_media(tmp_path / "1.jpg", "jpg"), "photo"),
        make_artifact(write_media(tmp_path / "2.jpg", "jpg"), "photo"),
    ]

    message = make_message()
    message.reply_media_group = AsyncMock(
        side_effect=[
            TelegramNetworkError(method=None, message="Can not write request body"),
            [{"ok": True}],
        ]
    )
    _, status = make_status()

    await ChatDelivery(None, message).deliver(
        artifacts, status=status, started_at=datetime.now()
    )

    assert message.reply_media_group.await_count == 2
    assert not any(artifact.path.exists() for artifact in artifacts)


@pytest.mark.asyncio
async def test_send_url_media_long_post_uses_rich_message():
    """Regression: a post longer than Telegram's 1024-character caption limit
    lost its tail; it now goes out as one rich message (text + photo)."""
    bot = SimpleNamespace(
        send_photo=AsyncMock(),
        send_message=AsyncMock(),
        send_rich_message=AsyncMock(return_value=SimpleNamespace(message_id=7)),
    )
    caption = "\n".join(
        f"line {index} https://example.com/{index}" for index in range(80)
    )

    result = await send_url_media(
        bot=bot,
        chat_id=5,
        items=[
            {
                "url": "https://c/a.jpg?x=1&y=2",
                "filename": "a.jpg",
                "send_type": "photo",
                "caption": caption,
            }
        ],
    )

    assert result == []
    bot.send_photo.assert_not_awaited()
    bot.send_message.assert_not_awaited()
    rich = bot.send_rich_message.await_args.kwargs
    assert rich["chat_id"] == 5
    assert rich["rich_message"].html == (
        '<img src="https://c/a.jpg?x=1&amp;y=2"/>'
        "<p>" + "<br>".join(caption.split("\n")) + "</p>"
    )


def test_rich_blocks_keeps_headings_lists_and_paragraphs():
    """Rich HTML drops bare newlines, so the caption must become real blocks."""
    caption = (
        "STOP WASTING HOURS.\n"
        "\n"
        "Intro paragraph\n"
        "with a second line.\n"
        "\n"
        "START HERE:\n"
        "\n"
        "1. Repo https://github.com/a/b\n"
        "2. Site https://a.b\n"
        "\n"
        "- bullet one\n"
        "- bullet two\n"
        "\n"
        "WATCH &amp; FOLLOW ALONG:\n"
    )

    assert rich_blocks(caption) == (
        "<h3>STOP WASTING HOURS.</h3>"
        "<p>Intro paragraph<br>with a second line.</p>"
        "<h3>START HERE:</h3>"
        "<ol><li>Repo https://github.com/a/b</li><li>Site https://a.b</li></ol>"
        "<ul><li>bullet one</li><li>bullet two</li></ul>"
        "<h3>WATCH &amp; FOLLOW ALONG:</h3>"
    )


@pytest.mark.asyncio
async def test_send_url_media_rich_failure_falls_back():
    """A server or client without rich messages keeps the old path: photo
    with no caption plus the full text as a follow-up message."""
    bot = SimpleNamespace(
        send_photo=AsyncMock(
            return_value=SimpleNamespace(photo=[SimpleNamespace(file_id="P1")])
        ),
        send_message=AsyncMock(return_value=SimpleNamespace(message_id=8)),
        send_rich_message=AsyncMock(
            side_effect=TelegramBadRequest(method=None, message="method not found")
        ),
    )
    caption = "x" * 1500

    await send_url_media(
        bot=bot,
        chat_id=5,
        items=[
            {
                "url": "https://c/a.jpg",
                "filename": "a.jpg",
                "send_type": "photo",
                "caption": caption,
            }
        ],
    )

    assert bot.send_photo.await_args.kwargs["caption"] is None
    bot.send_message.assert_awaited_once_with(5, caption)


@pytest.mark.asyncio
async def test_single_send_posts_long_caption_as_reply(monkeypatch, tmp_path):
    """Regression: gallery downloads dropped everything past character 1000
    of a long post; the overflow now arrives as a reply."""
    monkeypatch.setattr("services.delivery.ChatActionSender", _NoAction)
    caption = "\n".join(f"step {index} https://example.com/{index}" for index in range(80))
    artifact = make_artifact(
        write_media(tmp_path / "a.jpg", "jpg"), "photo", caption=caption
    )
    message = make_message()
    message.reply_photo = AsyncMock(
        return_value=SimpleNamespace(photo=[SimpleNamespace(file_id="P2")])
    )
    message.reply = AsyncMock()
    _, status = make_status()

    await ChatDelivery(None, message).deliver(
        [artifact], status=status, started_at=datetime.now()
    )

    assert message.reply_photo.await_args.kwargs["caption"] is None
    message.reply.assert_awaited_once_with(caption)