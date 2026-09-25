from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

from aiogram import Bot
from aiogram.types import (
    InlineQueryResultCachedAudio,
    InlineQueryResultCachedDocument,
    InlineQueryResultCachedMpeg4Gif,
    InlineQueryResultCachedPhoto,
    InlineQueryResultCachedVideo,
    InputMediaPhoto,
    InputMediaVideo,
    Message,
)

from utils.models import CachedMedia

logger = logging.getLogger(__name__)

_ALBUM_CHUNK = 10
_MAX_CAPTION = 1024


class MediaCache:
    """Persistent source-URL -> sent file_id cache.

    Telegram keeps uploaded media on its servers and lets bots resend them
    by file_id; caching them makes repeat requests instant (no re-download).
    """

    def __init__(self, cache_file: Path, max_entries: int = 200) -> None:
        self._cache_file = cache_file.resolve()
        self._lock = asyncio.Lock()
        self._max_entries = max_entries
        self._entries: dict[str, dict[str, Any]] = {}
        if self._cache_file.exists():
            try:
                payload = json.loads(self._cache_file.read_text(encoding="utf-8"))
                self._entries = dict(payload["entries"])
            except (OSError, ValueError, TypeError, KeyError) as exc:
                logger.warning(
                    "Media cache unreadable, starting empty | file=%s error=%s",
                    self._cache_file,
                    exc,
                )
                self._entries = {}

    def get(self, source_url: str) -> list[CachedMedia] | None:
        entry = self._entries.get(source_url)
        if entry is None:
            return None
        media = entry.get("media")
        if not isinstance(media, list):
            return None
        try:
            return [CachedMedia.from_dict(item) for item in media]
        except (TypeError, KeyError, ValueError) as exc:
            logger.warning(
                "Media cache entry corrupt | source=%s error=%s", source_url, exc
            )
            return None

    async def record(self, source_url: str, media: list[CachedMedia]) -> None:
        async with self._lock:
            self._entries[source_url] = {
                "updated_at": time.time(),
                "media": [item.to_dict() for item in media],
            }
            if len(self._entries) > self._max_entries:
                evict = sorted(
                    self._entries.items(), key=lambda item: item[1]["updated_at"]
                )[: len(self._entries) - self._max_entries]
                for url, _ in evict:
                    del self._entries[url]
                logger.info(
                    "Media cache evicted oldest entries | count=%s", len(evict)
                )
            await self._persist()

    async def remove(self, source_url: str) -> None:
        async with self._lock:
            if self._entries.pop(source_url, None) is not None:
                await self._persist()

    async def _persist(self) -> None:
        payload = json.dumps({"version": 1, "entries": self._entries})
        try:
            await asyncio.to_thread(self._write_atomic, payload)
        except OSError as exc:
            logger.warning(
                "Media cache persist failed | file=%s error=%s",
                self._cache_file,
                exc,
            )

    def _write_atomic(self, payload: str) -> None:
        temp_path = self._cache_file.with_name(f"{self._cache_file.name}.tmp")
        temp_path.write_text(payload, encoding="utf-8")
        os.replace(temp_path, self._cache_file)


def cached_media_from(
    sent: Any, *, send_type: str, file_name: str, caption: str | None
) -> CachedMedia | None:
    """Extract a CachedMedia from a just-sent Message (or a bool/None from
    edge-case edit methods)."""
    if sent is None or isinstance(sent, bool):
        return None
    if send_type == "photo":
        content = getattr(sent, "photo", None)
        file_id = content[-1].file_id if content else None
    else:
        file_id = getattr(
            getattr(sent, send_type if send_type != "document" else "document", None),
            "file_id",
            None,
        )
    if not file_id:
        logger.warning(
            "Cannot capture file_id from sent message | send_type=%s file=%s",
            send_type,
            file_name,
        )
        return None
    return CachedMedia(
        file_id=file_id, send_type=send_type, file_name=file_name, caption=caption
    )


def _cap(entry: CachedMedia) -> str | None:
    return entry.caption[:_MAX_CAPTION] if entry.caption else None


async def send_cached_media(
    bot: Bot, chat_id: int, entries: list[CachedMedia]
) -> list[Message]:
    """Resend cached file_ids to a chat; raises TelegramAPIError to the
    caller (used as the invalid-file_id fallback trigger)."""
    sent_messages: list[Message] = []
    if (
        len(entries) > 1
        and entries
        and all(entry.send_type in {"photo", "video"} for entry in entries)
    ):
        for start in range(0, len(entries), _ALBUM_CHUNK):
            chunk = entries[start : start + _ALBUM_CHUNK]
            items = [
                (
                    InputMediaPhoto(
                        media=entry.file_id,
                        caption=_cap(entry) if index == 0 else None,
                    )
                    if entry.send_type == "photo"
                    else InputMediaVideo(
                        media=entry.file_id,
                        supports_streaming=True,
                        caption=_cap(entry) if index == 0 else None,
                    )
                )
                for index, entry in enumerate(chunk)
            ]
            sent_messages.extend(await bot.send_media_group(chat_id, media=items))
        return sent_messages

    for entry in entries:
        if entry.send_type == "photo":
            sent_messages.append(
                await bot.send_photo(chat_id, photo=entry.file_id, caption=_cap(entry))
            )
        elif entry.send_type == "video":
            sent_messages.append(
                await bot.send_video(
                    chat_id,
                    video=entry.file_id,
                    caption=_cap(entry),
                    supports_streaming=True,
                )
            )
        elif entry.send_type == "audio":
            sent_messages.append(
                await bot.send_audio(
                    chat_id,
                    audio=entry.file_id,
                    caption=_cap(entry),
                    title=entry.file_name,
                )
            )
        elif entry.send_type == "animation":
            sent_messages.append(
                await bot.send_animation(
                    chat_id, animation=entry.file_id, caption=_cap(entry)
                )
            )
        elif entry.send_type == "video_note":
            sent_messages.append(
                await bot.send_video_note(chat_id, video_note=entry.file_id)
            )
        else:
            sent_messages.append(
                await bot.send_document(
                    chat_id, document=entry.file_id, caption=_cap(entry)
                )
            )
    return sent_messages


def cached_inline_results(entries: list[CachedMedia]) -> list:
    """Cached inline answers; result ids use the 'cached' prefix so the
    chosen-result handler can skip them (they post instantly client-side)."""
    results: list[Any] = []
    for index, entry in enumerate(entries):
        result_id = f"cached{index}"
        caption = (entry.caption or "")[:_MAX_CAPTION] or None
        if entry.send_type == "photo":
            results.append(
                InlineQueryResultCachedPhoto(
                    id=result_id,
                    photo_file_id=entry.file_id,
                    caption=caption if index == 0 else None,
                )
            )
        elif entry.send_type == "video":
            results.append(
                InlineQueryResultCachedVideo(
                    id=result_id,
                    video_file_id=entry.file_id,
                    title=entry.file_name or f"Video {index + 1}",
                    caption=caption if index == 0 else None,
                )
            )
        elif entry.send_type == "audio":
            results.append(
                InlineQueryResultCachedAudio(
                    id=result_id,
                    audio_file_id=entry.file_id,
                    caption=caption if index == 0 else None,
                )
            )
        elif entry.send_type == "animation":
            results.append(
                InlineQueryResultCachedMpeg4Gif(
                    id=result_id,
                    mpeg4_file_id=entry.file_id,
                    caption=caption if index == 0 else None,
                )
            )
        elif entry.send_type == "video_note":
            continue  # no cached inline type for video notes
        else:
            results.append(
                InlineQueryResultCachedDocument(
                    id=result_id,
                    document_file_id=entry.file_id,
                    title=entry.file_name or f"File {index + 1}",
                    caption=caption if index == 0 else None,
                )
            )
    return results
