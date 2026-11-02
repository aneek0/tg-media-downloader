from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import FSInputFile, InputMediaVideo

from config import Settings

from services.media_cache import MediaCache, cached_media_from
from services.media import video_metadata
from services.progress import humanbytes
from services.status import StatusSurface, inline_status
from services.request_store import RequestStore
from services.mornye import MornyeService
from services.requests import lifecycle
from services.delivery import DirectDelivery, media_item
from services.pipeline import download_inline
from utils import text
from utils.models import (
    CachedMedia,
    DownloadArtifact,
    FileTooLargeError,
    InlineMediaPayload,
    StoredRequest,
)

logger = logging.getLogger(__name__)


class InlineTaskRegistry:
    """Bookkeeping of running inline download tasks, so the cancel
    callback can find and cancel the task the user started."""

    def __init__(self) -> None:
        self._tasks: dict[str, asyncio.Task] = {}

    def start(self, token: str, task: asyncio.Task) -> None:
        self._tasks[token] = task
        task.add_done_callback(lambda _: self._tasks.pop(token, None))

    def cancel(self, token: str) -> asyncio.Task | None:
        """Cancel the task for a token; return it if it was running."""
        task = self._tasks.get(token)
        if task and not task.done():
            task.cancel()
            return task
        return None

    def __contains__(self, token: str) -> bool:
        return token in self._tasks





async def _dm_send_artifacts(
    bot: Bot, user_id: int, artifacts: list[DownloadArtifact]
) -> list[CachedMedia]:
    """Send spillover artifacts into the user's private chat."""
    return await DirectDelivery(bot, user_id).deliver(
        artifacts, started_at=datetime.now()
    )


async def _download_artifacts(
    *,
    status: StatusSurface,
    stored: StoredRequest,
    settings: Settings,
    request_store: RequestStore,
    mornye: MornyeService | None = None,
) -> list[DownloadArtifact]:
    """Edit the inline message into the downloading state, then fetch the
    media through the shared probe ladder."""
    await status.edit(
        text.DOWNLOAD_START.format(name=text.esc(stored.parsed_input.source_url))
    )
    return await download_inline(
        stored,
        settings=settings,
        status=status,
        work_dir=request_store.work_directory(stored.token),
        mornye=mornye,
    )


async def _fail_inline_message(status: StatusSurface, message: str) -> None:
    """Show a failure in the inline message; a dead message must not mask
    the failure handling."""
    await status.fail(message)


async def _spillover_dm(
    bot: Bot, user_id: int, rest: list[DownloadArtifact]
) -> tuple[str, list[CachedMedia]]:
    """Send artifacts beyond the first to the user's private chat; return
    the spillover note for the inline caption and the captured entries."""
    if not rest:
        return "", []
    try:
        dm_entries = await _dm_send_artifacts(bot, user_id, rest)
        return f"\n(+{len(rest)} more in your private chat)", dm_entries
    except TelegramAPIError as exc:
        logger.info(
            "Inline DM delivery failed | user=%s error=%s", user_id, exc
        )
        return f"\n(+{len(rest)} more files — open the bot and send the link)", []


def _selected_artifact(
    artifacts: list[DownloadArtifact], payload: InlineMediaPayload
) -> int | None:
    """Index of the artifact the user tapped, or None: by exact file name
    when the probe knew it, otherwise by probe-order index."""
    preferred_name = payload.preferred_name
    if preferred_name:
        for index, artifact in enumerate(artifacts):
            if artifact.file_name == preferred_name:
                return index
        logger.info(
            "Tapped media not found in download | expected=%s files=%s",
            preferred_name,
            [artifact.file_name for artifact in artifacts],
        )
        return None
    preferred = payload.preferred_index or 0
    if 0 < preferred < len(artifacts):
        return preferred
    return None


async def run_inline_download(
    *,
    bot: Bot,
    user_id: int,
    inline_message_id: str,
    stored: StoredRequest,
    settings: Settings,
    request_store: RequestStore,
    media_cache: MediaCache,
    mornye: MornyeService | None = None,
) -> None:
    """Own the inline request lifecycle: every terminal path deletes the
    token and its work directory."""
    with lifecycle(request_store, stored.token):
        await _run_inline_download(
            bot=bot,
            user_id=user_id,
            inline_message_id=inline_message_id,
            stored=stored,
            settings=settings,
            request_store=request_store,
            media_cache=media_cache,
            mornye=mornye,
        )


async def _deliver_first(
    *,
    bot: Bot,
    user_id: int,
    token: str,
    inline_message_id: str,
    status: StatusSurface,
    first: DownloadArtifact,
    caption: str,
    dm_entries: list[CachedMedia],
    dm_upload: bool,
) -> tuple[object, list[CachedMedia]]:
    """Swap the inline message into the tapped artifact and return the
    edited message plus the DM entries to cache (the poster flow prepends
    its DM video copy).

    Bot API rule: editing an INLINE message cannot upload a new file -
    media must be a file_id or a URL. For big videos (poster flow) the URL
    fetcher also fails (>20MB), so the video is uploaded to the user's DM
    first (sendVideo accepts uploads) and its file_id edits the inline
    message. The DM copy doubles as the delivery when the inline edit is
    impossible.
    """
    item, cover = media_item(first, caption)
    edited: object = None
    try:
        if dm_upload and first.send_type == "video":
            try:
                width, height, duration = await _video_dimensions(first)
                sent = await bot.send_video(
                    chat_id=user_id,
                    video=FSInputFile(first.path),
                    caption=caption,
                    width=width,
                    height=height,
                    duration=duration,
                    supports_streaming=True,
                )
                dm_entry = cached_media_from(
                    sent,
                    send_type="video",
                    file_name=first.file_name,
                    caption=first.caption,
                )
                if dm_entry:
                    dm_entries = [dm_entry, *dm_entries]
                    item = InputMediaVideo(
                        media=dm_entry.file_id,
                        width=width,
                        height=height,
                        duration=duration,
                        supports_streaming=True,
                        caption=caption,
                    )
            except TelegramAPIError as exc:
                logger.info(
                    "Inline DM upload failed | user=%s error=%s", user_id, exc
                )
        edited = await bot.edit_message_media(
            inline_message_id=inline_message_id,
            media=item,
        )
    except TelegramAPIError as exc:
        logger.exception(
            "Inline media edit failed | user=%s token=%s error=%s",
            user_id,
            token,
            exc,
        )
        # Never hang silently: the user must learn the media could not be
        # placed into the inline message. A media message can be edited to
        # text, so this failure notice reaches the poster/photo flows too.
        await _fail_inline_message(
            status,
            f"{text.INLINE_FAILED}\n<code>{text.esc(str(exc))}</code>",
        )
    finally:
        if cover is not None:
            cover.unlink(missing_ok=True)
        first.path.unlink(missing_ok=True)
    return edited, dm_entries


async def _video_dimensions(first: DownloadArtifact) -> tuple[int, int, int]:
    """Width/height/duration for the DM upload: reuse what the downloader
    probed, otherwise read the file off the event loop."""
    if (
        first.width is not None
        and first.height is not None
        and first.duration is not None
    ):
        return first.width, first.height, first.duration
    return await asyncio.to_thread(video_metadata, first.path)



async def _run_inline_download(
    *,
    bot: Bot,
    user_id: int,
    inline_message_id: str,
    stored: StoredRequest,
    settings: Settings,
    request_store: RequestStore,
    media_cache: MediaCache,
    mornye: MornyeService | None = None,
) -> None:
    token = stored.token
    media_payload = (
        stored.payload
        if isinstance(stored.payload, InlineMediaPayload)
        else InlineMediaPayload()
    )
    # A poster-photo result is already a media message: text edits are
    # impossible there, so progress stays silent until the final media edit.
    status = inline_status(
        bot,
        inline_message_id,
        name=text.esc(stored.parsed_input.source_url),
        media_message=bool(media_payload.poster_url),
    )

    try:
        artifacts = await _download_artifacts(
            status=status,
            stored=stored,
            settings=settings,
            request_store=request_store,
            mornye=mornye,
        )
    except asyncio.CancelledError:
        # user hit Cancel: the cancel callback already edited the message
        # and removed the token; just stop quietly
        logger.info(
            "Inline download cancelled | user=%s token=%s", user_id, token
        )
        raise
    except FileTooLargeError:
        logger.warning(
            "Inline download too large | user=%s token=%s", user_id, token
        )
        await _fail_inline_message(
            status,
            text.TOO_LARGE.format(size=humanbytes(settings.max_upload_bytes)),
        )
        return
    except Exception as exc:  # pylint: disable=broad-exception-caught  # pragma: no cover - user-facing safety boundary
        logger.exception(
            "Inline request failed | user=%s token=%s", user_id, token
        )
        await _fail_inline_message(
            status,
            f"{text.INLINE_FAILED}\n<code>{text.esc(str(exc))}</code>",
        )
        return

    # The tapped item goes into the message; the rest keep their original
    # order for the DM spillover. Videos are identified by their expected
    # download name (probe order != artifact order in mixed-media albums);
    # photos by their probe-order index.
    selected = _selected_artifact(artifacts, media_payload)
    if selected is not None and selected != 0:
        artifacts = [artifacts[selected]] + artifacts[:selected] + artifacts[selected + 1 :]

    rest = artifacts[1:]
    dm_note, dm_entries = await _spillover_dm(bot, user_id, rest)

    first = artifacts[0]
    caption = (first.caption or first.file_name)[:1000] + dm_note
    edited, dm_entries = await _deliver_first(
        bot=bot,
        user_id=user_id,
        token=token,
        inline_message_id=inline_message_id,
        status=status,
        first=first,
        caption=caption,
        dm_entries=dm_entries,
        dm_upload=bool(media_payload.video_url),
    )
    logger.info(
        "Inline request complete | user=%s token=%s files=%s dm_rest=%s",
        user_id,
        token,
        len(artifacts),
        len(rest),
    )
    first_entry = cached_media_from(
        edited,
        send_type=first.send_type,
        file_name=first.file_name,
        caption=(first.caption or first.file_name),
    )
    entries = ([first_entry] if first_entry else []) + dm_entries
    if entries:
        await media_cache.record(stored.parsed_input.source_url, entries)
