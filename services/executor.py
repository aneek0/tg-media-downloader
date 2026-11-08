from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import FSInputFile, Message

from services.delivery import ChatDelivery, send_url_media
from services.media_cache import CachedMedia
from services.mornye import audio_cache_key
from services.pipeline import download_artifacts
from services.progress import humanbytes
from services.session import Session
from services.status import StatusSurface, elapsed_seconds
from utils import text
from utils.logging_config import safe_url_label
from utils.models import (
    DownloadOption,
    FileTooLargeError,
    GalleryPayload,
    RequestType,
    StoredRequest,
)

logger = logging.getLogger(__name__)


async def execute_request(  # noqa: C901
    session: Session,
    stored: StoredRequest,
    option: DownloadOption,
    *,
    bot: Bot,
    status: StatusSurface,
    source_message: Message,
    user_id: int,
) -> None:
    settings = session.settings
    request_store = session.request_store
    media_cache = session.media_cache
    started_at = datetime.now()

    file_name = stored.parsed_input.custom_file_name or "downloaded-file"
    safe_name = text.esc(file_name)
    logger.info(
        "Starting request action | user=%s token=%s type=%s option=%s send_type=%s source=%s",
        user_id,
        stored.token,
        stored.request_type,
        option.option_id,
        option.send_type,
        safe_url_label(stored.parsed_input.source_url),
    )
    delivery = ChatDelivery(bot, source_message)
    try:
        payload = stored.payload
        if (
            stored.request_type == RequestType.GALLERY_MEDIA
            and isinstance(payload, GalleryPayload)
            and payload.content
        ):
            chunks = text.split_message(payload.content)
            await status.edit(text.esc(chunks[0]))
            for chunk in chunks[1:]:
                await source_message.answer(text.esc(chunk))
            return
        await status.start_download(safe_name)
        if stored.request_type == RequestType.GALLERY_MEDIA:
            url_items = (
                payload.url_media if isinstance(payload, GalleryPayload) else []
            )
            if url_items:
                try:
                    sent_media = await send_url_media(
                        bot=bot, chat_id=source_message.chat.id, items=url_items
                    )
                except TelegramAPIError as exc:
                    logger.warning(
                        "CDN URL send failed, falling back to local download | "
                        "user=%s token=%s error=%s",
                        user_id,
                        stored.token,
                        exc,
                    )
                    sent_media = None
                if sent_media is not None:
                    if sent_media:
                        await media_cache.record(
                            stored.parsed_input.source_url, sent_media
                        )
                    await status.done(
                        download_seconds=elapsed_seconds(started_at),
                        upload_seconds=elapsed_seconds(started_at),
                    )
                    logger.info(
                        "Completed request via Telegram CDN | user=%s token=%s files=%s",
                        user_id,
                        stored.token,
                        len(sent_media),
                    )
                    return
            artifacts = await download_artifacts(
                session, stored, option, status=status
            )
            uploaded = await delivery.deliver(
                artifacts=artifacts, status=status, started_at=started_at
            )
            if uploaded:
                await media_cache.record(stored.parsed_input.source_url, uploaded)
            logger.info(
                "Completed request action | user=%s token=%s files=%s",
                user_id,
                stored.token,
                len(artifacts),
            )
            return
        if stored.request_type == RequestType.MORNYE:
            await _run_mornye_request(
                session,
                stored,
                delivery=delivery,
                status=status,
                source_message=source_message,
                user_id=user_id,
                started_at=started_at,
            )
            return
        artifacts = await download_artifacts(
            session, stored, option, status=status
        )
        sent = await delivery.deliver(
            artifacts=artifacts, status=status, started_at=started_at
        )
        if sent:
            await media_cache.record(stored.parsed_input.source_url, sent)
        logger.info(
            "Completed request action | user=%s token=%s files=%s",
            user_id,
            stored.token,
            len(artifacts),
        )
    except FileTooLargeError as exc:
        logger.warning(
            "Download exceeded upload limit | user=%s token=%s type=%s option=%s error=%s",
            user_id,
            stored.token,
            stored.request_type,
            option.option_id,
            exc,
        )
        await status.fail(
            text.TOO_LARGE.format(
                size=humanbytes(settings.max_upload_bytes),
            )
        )
        return
    except Exception as exc:  # pragma: no cover - user-facing safety boundary
        logger.exception(
            "Request action failed | user=%s token=%s type=%s option=%s",
            user_id,
            stored.token,
            stored.request_type,
            option.option_id,
        )
        await status.fail(f"{text.DOWNLOAD_FAILED}\n<code>{text.esc(str(exc))}</code>")
    finally:
        request_store.delete(stored.token)
        logger.info("Cleaned request state | token=%s", stored.token)


def _album_caption(target) -> str | None:
    """💽/👤/📅/🎧 header for the cover photo; single tracks get none.
    Falls back to plain track album/artist fields when lists are empty."""
    if target.kind == "track" or not target.tracks:
        return None
    track = target.tracks[0]
    date = getattr(track, "release_date", "") or ""
    return text.ALBUM_INFO_CAPTION.format(
        name=text.esc(target.name),
        artist=text.esc(getattr(track, "artists", "") or "Unknown Artist"),
        date=date[:10],
        count=len(target.tracks),
    )


async def _send_cover_photo(
    source_message: Message,
    cover_path: Path | None,
    user_id: int,
    token: str,
    caption: str | None = None,
) -> None:
    """Album cover right before the audio group; non-fatal on failure.
    The downloaded cover file is removed afterwards."""
    if cover_path is None or not cover_path.exists():
        return
    try:
        await source_message.reply_photo(photo=FSInputFile(cover_path), caption=caption)
    except TelegramAPIError as exc:
        logger.warning(
            "Cover photo send failed | user=%s token=%s error=%s",
            user_id,
            token,
            exc,
        )
    cover_path.unlink(missing_ok=True)


async def _run_mornye_request(
    session: Session,
    stored: StoredRequest,
    *,
    status: StatusSurface,
    source_message: Message,
    user_id: int,
    started_at: datetime,
    delivery: ChatDelivery,
) -> None:
    """mornye flow: resolve → download → per-file uploads.

    Failed-tolerance: whatever downloaded gets sent; failures are listed in
    one summary edit. Only single tracks go to MediaCache.
    """
    work_dir = session.request_store.work_directory(stored.token)
    await status.edit(text.MUSIC_RESOLVING)
    target = await session.mornye.resolve_url(stored.parsed_input.source_url)
    total = len(target.tracks)
    kind_label = target.kind if target.kind == "track" else "collection"
    await status.edit(
        text.MUSIC_DOWNLOAD.format(
            name=text.esc(target.name), kind=kind_label, count=total
        )
    )
    preferences = session.preferences(user_id)
    artifacts, cover_path = await session.mornye.download_target(
        target,
        work_dir,
        audio_format=preferences.audio_format,
        audio_quality=preferences.audio_quality,
    )
    logger.info(
        "Music download finished | user=%s token=%s files=%s/%s",
        user_id,
        stored.token,
        len(artifacts),
        total,
    )
    if target.kind != "track":
        await _send_cover_photo(
            source_message,
            cover_path,
            user_id,
            stored.token,
            caption=_album_caption(target),
        )
    upload_started = datetime.now()
    sent_cache: list[CachedMedia] = []
    failed: list[tuple[int, str]] = []  # (position, title)
    try:
        # Albums/playlists go out in media groups (10 per message); a
        # single track is just a one-element list.
        sent_cache.extend(
            await delivery.deliver(
                artifacts=artifacts, status=status, started_at=started_at
            )
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Spotify upload failed | user=%s token=%s error=%s",
            user_id,
            stored.token,
            exc,
        )
        failed.append((0, str(exc)))

    for artifact in artifacts:
        artifact.path.unlink(missing_ok=True)

    if target.kind == "track" and sent_cache:
        await session.media_cache.record(
            audio_cache_key(
                stored.parsed_input.source_url,
                preferences.audio_quality,
                preferences.audio_format,
            ),
            sent_cache,
        )
    logger.info(
        "Spotify upload finished | user=%s token=%s sent=%s failed=%s seconds=%s",
        user_id,
        stored.token,
        len(sent_cache),
        len(failed),
        int((datetime.now() - upload_started).total_seconds()),
    )
    if failed and sent_cache:
        await status.fail(
            text.SPOTIFY_PARTIAL.format(
                sent=len(sent_cache),
                failed=len(failed),
                titles=text.esc(", ".join(title for _, title in failed[:5])),
            )
        )
    elif failed:
        await status.fail(text.SPOTIFY_FAILED_SINGLE)
    else:
        await status.done(
            download_seconds=elapsed_seconds(started_at),
            upload_seconds=elapsed_seconds(upload_started),
        )
