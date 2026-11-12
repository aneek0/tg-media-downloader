from __future__ import annotations

import asyncio
import html
import itertools
import logging
import re
import tempfile
from collections.abc import Iterator, Sequence
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramNetworkError
from aiogram.types import (
    FSInputFile,
    InlineQueryResultCachedAudio,
    InlineQueryResultCachedDocument,
    InlineQueryResultCachedMpeg4Gif,
    InlineQueryResultCachedPhoto,
    InlineQueryResultCachedVideo,
    InputMediaAudio,
    InputMediaDocument,
    InputMediaPhoto,
    InputMediaVideo,
    InputRichMessage,
    MediaUnion,
    Message,
)
from aiogram.utils.chat_action import ChatActionSender
from aiohttp import ClientOSError
from mutagen.flac import FLAC
from mutagen.mp3 import MP3

from services.media import audio_duration, video_metadata, video_note_metadata
from services.status import StatusSurface, elapsed_seconds
from utils import text
from utils.logging_config import safe_url_label
from utils.models import CachedMedia, DownloadArtifact

logger = logging.getLogger(__name__)

_ALBUM_CHUNK = 10
RICH_TEXT_LIMIT = 32768  # Bot API rich message ceiling, UTF-8 characters


_NUMBERED_LINE = re.compile(r"^(\d{1,3})\.\s+(.*)$")
_BULLET_LINE = re.compile(r"^[-•*]\s+(.*)$")


def _is_heading(line: str) -> bool:
    """Short all-caps line: 'START HERE', 'GITHUB REPOS:'. Rich messages need
    real block tags, so section markers become <h3> instead of plain lines."""
    plain = html.unescape(line)
    letters = [char for char in plain if char.isalpha()]
    return (
        len(letters) >= 3
        and len(plain) <= 80
        and all(char.isupper() for char in letters)
    )


def _list_item(line: str) -> tuple[bool, int, str] | None:
    """(is_ordered, first_number, text) for a list line, else None."""
    numbered = _NUMBERED_LINE.match(line)
    if numbered:
        return True, int(numbered.group(1)), numbered.group(2)
    bullet = _BULLET_LINE.match(line)
    if bullet:
        return False, 1, bullet.group(1)
    return None


def _line_kind(line: str) -> str:
    """Block kind of one stripped caption line: blank, heading, text, ol, ul."""
    if not line:
        return "blank"
    if _is_heading(line):
        return "heading"
    entry = _list_item(line)
    if entry is None:
        return "text"
    return "ol" if entry[0] else "ul"


def _render_group(kind: str, lines: list[str]) -> str:
    """One rich block for a run of consecutive lines of the same kind."""
    if kind == "heading":
        return "".join(f"<h3>{line}</h3>" for line in lines)
    if kind in {"ol", "ul"}:
        items = [entry for entry in (_list_item(line) for line in lines) if entry]
        bodies = "".join(f"<li>{body}</li>" for _, _, body in items)
        start = items[0][1] if kind == "ol" and items else 1
        opener = f'<ol start="{start}">' if start != 1 else f"<{kind}>"
        return f"{opener}{bodies}</{kind}>"
    if kind == "blank":
        return ""
    return "<p>" + "<br>".join(lines) + "</p>"


def rich_blocks(caption: str) -> str:
    """Turn a plain, HTML-escaped caption into rich message blocks.

    Rich HTML drops bare newlines (everything collapses into one paragraph), so
    paragraphs, ordered/bulleted lists and all-caps section headings must be
    emitted as tags to survive."""
    stripped = [line.strip() for line in caption.split("\n")]
    return "".join(
        _render_group(kind, list(group))
        for kind, group in itertools.groupby(stripped, key=_line_kind)
    )


def rich_post_html(photo_urls: Sequence[str], caption: str) -> str:
    """One Bot API rich message holding the post text and its photos, so long
    posts keep their full text (32 768 characters) instead of a 1024-character
    caption plus a detached follow-up message."""
    media = "".join(
        f'<img src="{html.escape(url, quote=True)}"/>' for url in photo_urls
    )
    container = f"<tg-collage>{media}</tg-collage>" if len(photo_urls) > 1 else media
    return container + rich_blocks(caption[:RICH_TEXT_LIMIT])


def _url_media_item(
    item: dict, caption: str | None
) -> InputMediaPhoto | InputMediaVideo | InputMediaAudio | InputMediaDocument:
    """InputMedia wrapper for a raw URL (Telegram fetches it itself)."""
    media_class = {
        "photo": InputMediaPhoto,
        "video": InputMediaVideo,
        "audio": InputMediaAudio,
        "document": InputMediaDocument,
    }[item["send_type"]]
    if item["send_type"] == "video":
        return InputMediaVideo(
            media=item["url"], caption=caption, supports_streaming=True
        )
    return media_class(media=item["url"], caption=caption)


async def _retry_media_group(send_group: Any, items: list) -> list:
    """Send a media group retrying once on connection errors. Retry helps
    when the local Bot API server drops the request mid-body upload
    (aiohttp ``Can not write request body``)."""
    attempts = 2
    for attempt in range(1, attempts + 1):
        try:
            return await send_group(items)
        except (TelegramNetworkError, ClientOSError):
            if attempt == attempts:
                raise
            logger.warning(
                "Media group upload lost connection | attempt=%s retrying in 5s",
                attempt,
            )
            await asyncio.sleep(5)
    raise RuntimeError("unreachable")


async def send_url_media(
    *, bot: Bot, chat_id: int, items: list[dict]
) -> list[CachedMedia] | None:
    """Send media by raw URL: Telegram's servers download the CDN files
    (the Telegram-CDN outsource path). item: {url, filename, send_type,
    caption}. TelegramAPIError propagates to the caller for the local
    fallback. Returns None when the post went out as a rich message and has
    no file_id to cache."""
    if len(items) == 1:
        return await _send_url_single(bot=bot, chat_id=chat_id, item=items[0])

    sent_media: list[CachedMedia] = []
    # Telegram media groups reject mixing audio/documents with photos and
    # videos, so those kinds travel as their own single sends after the
    # rest keeps its album.
    album = [
        item for item in items if item["send_type"] in {"photo", "video"}
    ]
    singles = [
        item for item in items if item["send_type"] not in {"photo", "video"}
    ]
    for start in range(0, len(album), _ALBUM_CHUNK):
        chunk = album[start : start + _ALBUM_CHUNK]
        caption, overflow = text.split_caption(chunk[0].get("caption"))
        sent_group = await _retry_media_group(
            lambda media: bot.send_media_group(chat_id=chat_id, media=media),
            [
                _url_media_item(item, caption if index == 0 else None)
                for index, item in enumerate(chunk)
            ],
        )
        for item, sent in zip(chunk, sent_group, strict=False):
            entry = cached_media_from(
                sent,
                send_type=item["send_type"],
                file_name=item["filename"],
                caption=item.get("caption"),
            )
            if entry is not None:
                sent_media.append(entry)
        await _send_overflow(bot, chat_id, None, overflow)
    for item in singles:
        sent_media.extend(
            await _send_url_single(bot=bot, chat_id=chat_id, item=item)
        )
    return sent_media


async def _send_url_single(
    *, bot: Bot, chat_id: int, item: dict
) -> list[CachedMedia]:
    """Send one raw-URL item and return its cache entries (empty when the
    post went out as a rich message and has no file_id to cache)."""
    send_type = item["send_type"]
    caption, overflow = text.split_caption(item.get("caption"))
    if overflow and send_type == "photo":
        try:
            await bot.send_rich_message(
                chat_id=chat_id,
                rich_message=InputRichMessage(
                    html=rich_post_html([item["url"]], item["caption"])
                ),
            )
        except TelegramAPIError:
            logger.warning(
                "Rich message send failed, falling back to caption and "
                "follow-up text | chat=%s source=%s",
                chat_id,
                safe_url_label(item["url"]),
            )
        else:
            logger.info(
                "Rich message sent | chat=%s source=%s chars=%s",
                chat_id,
                safe_url_label(item["url"]),
                len(item["caption"]),
            )
            return []
    if send_type == "photo":
        sent = await bot.send_photo(
            chat_id=chat_id, photo=item["url"], caption=caption
        )
    elif send_type == "video":
        sent = await bot.send_video(
            chat_id=chat_id,
            video=item["url"],
            caption=caption,
            supports_streaming=True,
        )
    elif send_type == "audio":
        sent = await bot.send_audio(
            chat_id=chat_id,
            audio=item["url"],
            caption=caption,
            # An explicit empty title is rejected (AUDIO_TITLE_EMPTY).
            title=item.get("audio_title") or item["filename"],
            performer=item.get("audio_performer"),
        )
    else:
        sent = await bot.send_document(
            chat_id=chat_id,
            document=item["url"],
            caption=caption,
        )
    entry = cached_media_from(
        sent,
        send_type=send_type,
        file_name=item["filename"],
        caption=item.get("caption"),
    )
    await _send_overflow(bot, chat_id, None, overflow)
    return [entry] if entry is not None else []



def _extract_cover(path: Path) -> Path | None:
    """Extract the embedded cover art (FLAC/MP3) to a temp JPEG for the
    audio thumbnail. None when the file has no embedded picture — callers
    then send the audio without a cover."""
    try:
        picture = None
        suffix = path.suffix.lower()
        if suffix == ".flac":
            pictures = FLAC(str(path)).pictures
            picture = pictures[0] if pictures else None
        elif suffix == ".mp3":
            tags = MP3(str(path)).tags
            apics = tags.getall("APIC") if tags else []
            picture = apics[0] if apics else None
        if picture is None or not picture.data:
            return None
        cover = Path(tempfile.mkstemp(suffix=".jpg")[1])
        cover.write_bytes(picture.data)
        return cover
    except Exception:  # noqa: BLE001
        logger.debug("Cover extraction failed | file=%s", path.name)
        return None


@dataclass(frozen=True, slots=True)
class MediaFacts:
    """What Telegram needs about the file when the download did not supply it."""

    width: int | None = None
    height: int | None = None
    duration: int | None = None


async def media_facts(artifact: DownloadArtifact) -> MediaFacts:
    """Read the missing dimensions/duration off the file, off the loop.

    Every consumer of an artifact's metadata goes through here: the single-send
    path (send_video needs width/height/duration) and the InputMedia path (a
    media group, an inline edit). Reading the file must happen before the
    artifact is uploaded and unlinked.
    """
    width, height, duration = artifact.width, artifact.height, artifact.duration
    if artifact.send_type == "video_note":
        if width is None or duration is None:
            length, fallback_duration = await asyncio.to_thread(
                video_note_metadata, artifact.path
            )
            width = length if width is None else width
            height = length if height is None else height
            duration = fallback_duration if duration is None else duration
    elif artifact.send_type == "video":
        if width is None or height is None or duration is None:
            fallback_width, fallback_height, fallback_duration = (
                await asyncio.to_thread(video_metadata, artifact.path)
            )
            width = fallback_width if width is None else width
            height = fallback_height if height is None else height
            duration = fallback_duration if duration is None else duration
    elif artifact.send_type == "audio" and duration is None:
        duration = await asyncio.to_thread(audio_duration, artifact.path)
    return MediaFacts(width=width, height=height, duration=duration)


_PHOTO_MAX_BYTES = 10 * 1024 * 1024  # Bot API sendPhoto ceiling


def send_kind(artifact: DownloadArtifact) -> str:
    """Telegram refuses photos over 10 MB, so such a photo travels as a file.

    The downgrade is recorded on the artifact: an inline edit reuses the DM
    file_id after the upload unlinked the file, and must send the same type.
    """
    if artifact.send_type == "photo":
        try:
            if artifact.path.stat().st_size > _PHOTO_MAX_BYTES:
                artifact.send_type = "document"
        except OSError:
            pass
    return artifact.send_type


async def media_item(
    artifact: DownloadArtifact,
    caption: str | None,
    *,
    media: FSInputFile | str,
    facts: MediaFacts | None = None,
) -> tuple[object, Path | None]:
    """Build one InputMedia entry for an artifact.

    ``media`` is either a local file (a media group) or an existing ``file_id``
    (editing an inline message, where the Bot API refuses to upload). The second
    element is a temp cover file the caller must unlink after the request;
    aiogram reads InputFile bodies lazily at send time. A ``file_id`` never gets
    a thumbnail: an inline edit cannot upload one.
    """
    facts = facts if facts is not None else await media_facts(artifact)
    local = isinstance(media, FSInputFile)
    send_type = send_kind(artifact)
    if send_type == "photo":
        return InputMediaPhoto(media=media, caption=caption), None
    if send_type == "video":
        return (
            InputMediaVideo(
                media=media,
                width=facts.width,
                height=facts.height,
                duration=facts.duration,
                supports_streaming=True,
                caption=caption,
            ),
            None,
        )
    if send_type == "audio":
        cover = await asyncio.to_thread(_extract_cover, artifact.path) if local else None
        return (
            InputMediaAudio(
                media=media,
                duration=facts.duration,
                caption=caption,
                title=artifact.audio_title or artifact.file_name,
                performer=artifact.audio_performer,
                thumbnail=FSInputFile(cover) if cover is not None else None,
            ),
            cover,
        )
    if send_type == "video_note":
        # A video note cannot travel in a media group or an inline edit, so it
        # degrades to a regular video of the same length.
        return (
            InputMediaVideo(
                media=media,
                width=facts.width,
                height=facts.width,
                duration=facts.duration,
                supports_streaming=True,
                caption=caption,
            ),
            None,
        )
    return InputMediaDocument(media=media, caption=caption), None


def _action(reply: Message | None, method: Any, bot: Bot, chat_id: int) -> Any:
    """Chat actions only exist in a real chat; DM spillover sends silently."""
    if reply is None:
        return nullcontext()
    return method(bot=bot, chat_id=chat_id)


async def _send_overflow(
    bot: Bot, chat_id: int, reply: Message | None, chunks: Sequence[str]
) -> None:
    """Post caption text that does not fit Telegram's 1024-character caption
    limit as plain follow-up messages."""
    for chunk in chunks:
        if reply is not None:
            await reply.reply(chunk)
        else:
            await bot.send_message(chat_id, chunk)


async def _send_single(
    bot: Bot,
    chat_id: int,
    artifact: DownloadArtifact,
    *,
    reply: Message | None,
) -> CachedMedia | None:
    """Send one artifact (reply in a chat, plain send in DM) and return its
    cache entry. Metadata the download did not provide is read from the
    file itself."""
    file_input = FSInputFile(artifact.path, filename=artifact.file_name)
    logger.info(
        "Upload starting | chat=%s file=%s send_type=%s size=%s",
        chat_id,
        artifact.file_name,
        artifact.send_type,
        artifact.path.stat().st_size if artifact.path.exists() else 0,
    )

    async def send(method: str, **kwargs: object) -> Message:
        if reply is not None:
            return await getattr(reply, f"reply_{method}")(**kwargs)
        return await getattr(bot, f"send_{method}")(chat_id=chat_id, **kwargs)

    send_type = send_kind(artifact)
    caption, overflow = text.split_caption(artifact.caption)
    facts = await media_facts(artifact)
    extracted_cover: Path | None = None
    if send_type == "video":
        async with _action(reply, ChatActionSender.upload_video, bot, chat_id):
            sent = await send(
                "video",
                video=file_input,
                caption=caption,
                duration=facts.duration,
                width=facts.width,
                height=facts.height,
                supports_streaming=True,
            )
    elif send_type == "audio":
        extracted_cover = await asyncio.to_thread(_extract_cover, artifact.path)
        try:
            async with _action(reply, ChatActionSender.upload_document, bot, chat_id):
                sent = await send(
                    "audio",
                    audio=file_input,
                    caption=caption,
                    duration=facts.duration,
                    thumbnail=(
                        FSInputFile(extracted_cover) if extracted_cover is not None else None
                    ),
                    title=artifact.audio_title or artifact.file_name,
                    performer=artifact.audio_performer,
                )
        finally:
            if extracted_cover is not None:
                extracted_cover.unlink(missing_ok=True)
    elif send_type == "video_note":
        async with _action(reply, ChatActionSender.upload_video_note, bot, chat_id):
            sent = await send(
                "video_note",
                video_note=file_input,
                duration=facts.duration,
                length=facts.width,
            )
    elif send_type == "photo":
        async with _action(reply, ChatActionSender.upload_photo, bot, chat_id):
            sent = await send("photo", photo=file_input, caption=caption)
    else:
        async with _action(reply, ChatActionSender.upload_document, bot, chat_id):
            sent = await send("document", document=file_input, caption=caption)
    await _send_overflow(bot, chat_id, reply, overflow)
    return cached_media_from(
        sent, send_type=send_type, file_name=artifact.file_name, caption=artifact.caption
    )


async def _send_group(
    bot: Bot,
    chat_id: int,
    chunk: Sequence[DownloadArtifact],
    *,
    reply: Message | None,
    caption_first: bool,
) -> list[CachedMedia]:
    """Send a chunk as one media group. Chat replies leave the caption off
    (the album header travels as its own message); DM spillover keeps the
    first caption."""
    entries: list[tuple[DownloadArtifact, object]] = []
    covers: list[Path] = []
    sent_group: list = []
    caption, overflow = (
        text.split_caption(chunk[0].caption) if caption_first else (None, [])
    )
    try:
        for index, artifact in enumerate(chunk):
            item, cover = await media_item(
                artifact,
                caption if index == 0 else None,
                media=FSInputFile(artifact.path, filename=artifact.file_name),
            )
            entries.append((artifact, item))
            if cover is not None:
                covers.append(cover)
        media = [item for _, item in entries]
        if reply is not None:
            sent_group = await _retry_media_group(
                lambda items: reply.reply_media_group(media=items), media
            )
        else:
            sent_group = await _retry_media_group(
                lambda items: bot.send_media_group(chat_id=chat_id, media=items),
                media,
            )
    finally:
        for cover in covers:
            cover.unlink(missing_ok=True)
    cached: list[CachedMedia] = []
    for (artifact, _), sent in zip(entries, sent_group, strict=False):
        entry = cached_media_from(
            sent,
            send_type=send_kind(artifact),
            file_name=artifact.file_name,
            caption=artifact.caption,
        )
        if entry is not None:
            cached.append(entry)
    await _send_overflow(bot, chat_id, reply, overflow)
    return cached


def _chunks(artifacts: Sequence[DownloadArtifact]) -> Iterator[list[DownloadArtifact]]:
    """Group consecutive artifacts into albums of at most 10; video notes,
    documents and audio always travel alone because Telegram media groups
    reject mixing them with photos/videos. ``send_kind`` records the >10 MB
    photo downgrade, so a large photo is split out before the album is built."""
    batch: list[DownloadArtifact] = []

    def flush() -> Iterator[list[DownloadArtifact]]:
        if batch:
            yield batch[:]
            batch.clear()

    for artifact in artifacts:
        if send_kind(artifact) in {"video_note", "document", "audio"}:
            yield from flush()
            yield [artifact]
            continue
        batch.append(artifact)
        if len(batch) == _ALBUM_CHUNK:
            yield from flush()
    yield from flush()

async def _deliver(
    *,
    bot: Bot,
    chat_id: int,
    artifacts: Sequence[DownloadArtifact],
    reply: Message | None,
    status: StatusSurface | None,
    started_at: datetime,
    album_caption: bool,
) -> list[CachedMedia]:
    if not artifacts:
        return []
    upload_started = datetime.now()
    if status is not None:
        await status.uploading(text.esc(artifacts[0].file_name))
    cached: list[CachedMedia] = []
    for chunk in _chunks(artifacts):
        if len(chunk) == 1:
            entry = await _send_single(
                bot,
                chat_id,
                chunk[0],
                reply=reply,
            )
            if entry is not None:
                cached.append(entry)
            continue
        cached.extend(
            await _send_group(
                bot,
                chat_id,
                chunk,
                reply=reply,
                caption_first=album_caption,
            )
        )
    for artifact in artifacts:
        artifact.path.unlink(missing_ok=True)
    if status is not None:
        await status.done(
            download_seconds=elapsed_seconds(started_at),
            upload_seconds=elapsed_seconds(upload_started),
        )
    return cached


class ChatDelivery:
    """Deliver artifacts into the chat where the request was made: replies
    to the source message and keeps the status message in sync."""

    def __init__(self, bot: Bot, source_message: Message) -> None:
        self._bot = bot
        self._source_message = source_message

    async def deliver(
        self,
        artifacts: Sequence[DownloadArtifact],
        *,
        status: StatusSurface,
        started_at: datetime,
    ) -> list[CachedMedia]:
        return await _deliver(
            bot=self._bot,
            chat_id=self._source_message.chat.id,
            artifacts=artifacts,
            reply=self._source_message,
            status=status,
            started_at=started_at,
            album_caption=False,
        )


class DirectDelivery:
    """Deliver artifacts straight into a chat with no source message and no
    status surface (inline-mode DM spillover)."""

    def __init__(self, bot: Bot, chat_id: int) -> None:
        self._bot = bot
        self._chat_id = chat_id

    async def deliver(
        self, artifacts: Sequence[DownloadArtifact], *, started_at: datetime
    ) -> list[CachedMedia]:
        return await _deliver(
            bot=self._bot,
            chat_id=self._chat_id,
            artifacts=artifacts,
            reply=None,
            status=None,
            started_at=started_at,
            album_caption=True,
        )


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
        file_id = getattr(getattr(sent, send_type, None), "file_id", None)
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


async def send_cached(  # noqa: C901
    bot: Bot, chat_id: int, entries: list[CachedMedia]
) -> list[Message]:
    """Resend cached file_ids to a chat; raises TelegramAPIError to the
    caller (used as the invalid-file_id fallback trigger)."""
    sent_messages: list[Message] = []
    if (
        len(entries) > 1
        and all(entry.send_type in {"photo", "video"} for entry in entries)
    ):
        for start in range(0, len(entries), _ALBUM_CHUNK):
            chunk = entries[start : start + _ALBUM_CHUNK]
            caption, overflow = text.split_caption(chunk[0].caption)
            items: list[MediaUnion] = [
                (
                    InputMediaPhoto(
                        media=entry.file_id,
                        caption=caption if index == 0 else None,
                    )
                    if entry.send_type == "photo"
                    else InputMediaVideo(
                        media=entry.file_id,
                        supports_streaming=True,
                        caption=caption if index == 0 else None,
                    )
                )
                for index, entry in enumerate(chunk)
            ]
            sent_messages.extend(await bot.send_media_group(chat_id, media=items))
            for part in overflow:
                sent_messages.append(await bot.send_message(chat_id, part))
        return sent_messages

    for index, entry in enumerate(entries):
        caption, overflow = text.split_caption(entry.caption)
        if entry.send_type == "photo":
            sent_messages.append(
                await bot.send_photo(chat_id, photo=entry.file_id, caption=caption)
            )
        elif entry.send_type == "video":
            sent_messages.append(
                await bot.send_video(
                    chat_id,
                    video=entry.file_id,
                    caption=caption,
                    supports_streaming=True,
                )
            )
        elif entry.send_type == "audio":
            sent_messages.append(
                await bot.send_audio(
                    chat_id,
                    audio=entry.file_id,
                    caption=caption,
                    # An empty title is rejected (AUDIO_TITLE_EMPTY); the
                    # cached numeric names predate the music-title fix.
                    title=entry.file_name or f"Audio {index + 1}",
                )
            )
        elif entry.send_type == "animation":
            sent_messages.append(
                await bot.send_animation(
                    chat_id, animation=entry.file_id, caption=caption
                )
            )
        elif entry.send_type == "video_note":
            sent_messages.append(
                await bot.send_video_note(chat_id, video_note=entry.file_id)
            )
        else:
            sent_messages.append(
                await bot.send_document(
                    chat_id, document=entry.file_id, caption=caption
                )
            )
        for part in overflow:
            sent_messages.append(await bot.send_message(chat_id, part))
    return sent_messages


def inline_cached_results(entries: list[CachedMedia]) -> list:
    """Cached inline answers; result ids use the 'cached' prefix so the
    chosen-result handler can skip them (they post instantly client-side)."""
    results: list[Any] = []
    for index, entry in enumerate(entries):
        result_id = f"cached{index}"
        caption = (entry.caption or "")[: text.CAPTION_LIMIT] or None
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
                    # The local Bot API server rejects a title-less audio
                    # result with AUDIO_TITLE_EMPTY.
                    title=entry.file_name or f"Audio {index + 1}",
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