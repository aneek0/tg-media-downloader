from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime

from aiogram import Bot
from aiogram.types import (
    FSInputFile,
    InputMediaDocument,
    InputMediaPhoto,
    InputMediaVideo,
    Message,
)
from aiogram.utils.chat_action import ChatActionSender

from services.media_cache import cached_media_from
from services.media import audio_duration, video_metadata, video_note_metadata
from utils import text
from utils.models import CachedMedia, DownloadArtifact


logger = logging.getLogger(__name__)


def _thumb_file(path: str | None) -> FSInputFile | None:
    if path and os.path.isfile(path):
        return FSInputFile(path)
    return None


async def upload_artifact(
    *,
    bot: Bot,
    status_message: Message,
    source_message: Message,
    artifact: DownloadArtifact,
    thumbnail_path: str | None,
    started_at: datetime,
) -> CachedMedia | None:
    await status_message.edit_text(text.upload_caption(text.esc(artifact.file_name)))
    thumb = _thumb_file(thumbnail_path)
    file_input = FSInputFile(artifact.path)
    download_seconds = int((datetime.now() - started_at).total_seconds())
    upload_started = datetime.now()
    logger.info(
        "Upload starting | chat=%s file=%s send_type=%s size=%s thumbnail=%s",
        source_message.chat.id,
        artifact.file_name,
        artifact.send_type,
        artifact.path.stat().st_size if artifact.path.exists() else 0,
        "yes" if thumb else "no",
    )

    sent_media: CachedMedia | None = None
    if artifact.send_type == "video":
        width = artifact.width
        height = artifact.height
        duration = artifact.duration
        if duration is None or width is None or height is None:
            fallback_width, fallback_height, fallback_duration = await asyncio.to_thread(
                video_metadata, artifact.path
            )
            width = fallback_width if width is None else width
            height = fallback_height if height is None else height
            duration = fallback_duration if duration is None else duration
        async with ChatActionSender.upload_video(bot=bot, chat_id=source_message.chat.id):
            sent = await source_message.reply_video(
                video=file_input,
                caption=artifact.caption,
                duration=duration,
                width=width,
                height=height,
                supports_streaming=True,
                thumbnail=thumb,
            )
        sent_media = cached_media_from(
            sent, send_type="video", file_name=artifact.file_name, caption=artifact.caption
        )
    elif artifact.send_type == "audio":
        duration = artifact.duration
        if duration is None:
            duration = await asyncio.to_thread(audio_duration, artifact.path)
        async with ChatActionSender.upload_document(bot=bot, chat_id=source_message.chat.id):
            sent = await source_message.reply_audio(
                audio=file_input,
                caption=artifact.caption,
                duration=duration,
                thumbnail=thumb,
                title=artifact.file_name,
            )
        sent_media = cached_media_from(
            sent, send_type="audio", file_name=artifact.file_name, caption=artifact.caption
        )
    elif artifact.send_type == "video_note":
        length = artifact.width
        duration = artifact.duration
        if duration is None or length is None:
            fallback_length, fallback_duration = await asyncio.to_thread(
                video_note_metadata, artifact.path
            )
            length = fallback_length if length is None else length
            duration = fallback_duration if duration is None else duration
        async with ChatActionSender.upload_video_note(
            bot=bot, chat_id=source_message.chat.id
        ):
            sent = await source_message.reply_video_note(
                video_note=file_input,
                duration=duration,
                length=length or 240,
                thumbnail=thumb,
            )
        sent_media = cached_media_from(
            sent, send_type="video_note", file_name=artifact.file_name, caption=artifact.caption
        )
    elif artifact.send_type == "photo":
        async with ChatActionSender.upload_photo(bot=bot, chat_id=source_message.chat.id):
            sent = await source_message.reply_photo(
                photo=file_input,
                caption=artifact.caption,
            )
        sent_media = cached_media_from(
            sent, send_type="photo", file_name=artifact.file_name, caption=artifact.caption
        )
    else:
        async with ChatActionSender.upload_document(bot=bot, chat_id=source_message.chat.id):
            sent = await source_message.reply_document(
                document=file_input,
                caption=artifact.caption,
                thumbnail=thumb,
            )
        sent_media = cached_media_from(
            sent, send_type="document", file_name=artifact.file_name, caption=artifact.caption
        )

    upload_seconds = int((datetime.now() - upload_started).total_seconds())
    logger.info(
        "Upload complete | chat=%s file=%s send_type=%s download_seconds=%s upload_seconds=%s",
        source_message.chat.id,
        artifact.file_name,
        artifact.send_type,
        download_seconds,
        upload_seconds,
    )
    await status_message.edit_text(
        text.DONE.format(
            download_seconds=download_seconds,
            upload_seconds=upload_seconds,
        )
    )
    artifact.path.unlink(missing_ok=True)
    return sent_media


def _media_item(
    artifact: DownloadArtifact, caption: str | None
) -> InputMediaPhoto | InputMediaVideo | InputMediaDocument:
    file_input = FSInputFile(artifact.path)
    ext = artifact.path.suffix.lstrip(".").lower()
    if ext in {"jpg", "jpeg", "png", "webp"}:
        return InputMediaPhoto(media=file_input, caption=caption)
    if ext in {"mp4", "mkv", "webm", "mov"}:
        if artifact.duration is not None and artifact.width is not None and artifact.height is not None:
            width, height, duration = artifact.width, artifact.height, artifact.duration
        else:
            width, height, duration = video_metadata(artifact.path)
        return InputMediaVideo(
            media=file_input,
            width=width,
            height=height,
            duration=duration,
            supports_streaming=True,
            caption=caption,
        )
    return InputMediaDocument(media=file_input, caption=caption)


async def upload_artifacts(
    *,
    bot: Bot,
    status_message: Message,
    source_message: Message,
    artifacts: list[DownloadArtifact],
    started_at: datetime,
    thumbnail_path: str | None = None,
) -> list[CachedMedia]:
    if len(artifacts) == 1:
        media = await upload_artifact(
            bot=bot,
            status_message=status_message,
            source_message=source_message,
            artifact=artifacts[0],
            thumbnail_path=thumbnail_path,
            started_at=started_at,
        )
        return [media] if media else []

    await status_message.edit_text(text.upload_caption(text.esc(artifacts[0].file_name)))
    upload_started = datetime.now()
    logger.info(
        "Album upload starting | chat=%s files=%s",
        source_message.chat.id,
        [a.file_name for a in artifacts],
    )
    cached: list[CachedMedia] = []
    for start in range(0, len(artifacts), 10):
        chunk = artifacts[start : start + 10]
        items = [
            _media_item(
                artifact,
                artifacts[0].caption if start == 0 and index == 0 else None,
            )
            for index, artifact in enumerate(chunk)
        ]
        sent_group = await source_message.reply_media_group(media=items)
        # send_media_group returns messages in input order; zip artifacts to
        # their sent messages so file_ids line up per element.
        for artifact, sent in zip(chunk, sent_group):
            media_entry = cached_media_from(
                sent,
                send_type=artifact.send_type,
                file_name=artifact.file_name,
                caption=artifact.caption,
            )
            if media_entry is not None:
                cached.append(media_entry)

    upload_seconds = int((datetime.now() - upload_started).total_seconds())
    download_seconds = int((datetime.now() - started_at).total_seconds())
    logger.info(
        "Album upload complete | chat=%s files=%s download_seconds=%s upload_seconds=%s",
        source_message.chat.id,
        len(artifacts),
        download_seconds,
        upload_seconds,
    )
    await status_message.edit_text(
        text.DONE.format(download_seconds=download_seconds, upload_seconds=upload_seconds)
    )
    for artifact in artifacts:
        artifact.path.unlink(missing_ok=True)
    return cached
