from __future__ import annotations

import asyncio
import logging

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

from config import Settings
from services.direct_downloads import download_direct_file
from services.gallery import download_gallery_media
from services.media_cache import MediaCache, cached_media_from
from services.parsing import is_twitter_status_url
from services.progress import humanbytes
from services.request_store import RequestStore
from services.telegram_uploads import _media_item
from services.ytdlp import build_direct_options
from utils import text
from utils.models import (
    CachedMedia,
    DownloadArtifact,
    FileTooLargeError,
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


class _InlineStatus:
    """Duck-typed status-message shim: lets download_direct_file edit the
    inline message's text while it streams progress updates."""

    def __init__(self, bot: Bot, inline_message_id: str) -> None:
        self._bot = bot
        self._inline_message_id = inline_message_id

    async def edit_text(self, message_text: str, **_: object) -> None:
        await self._bot.edit_message_text(
            inline_message_id=self._inline_message_id,
            text=message_text,
        )


async def _dm_send_artifacts(
    bot: Bot, user_id: int, artifacts: list[DownloadArtifact]
) -> list[CachedMedia]:
    sent_media: list[CachedMedia] = []
    for start in range(0, len(artifacts), 10):
        chunk = artifacts[start : start + 10]
        if len(chunk) == 1:
            artifact = chunk[0]
            if artifact.send_type == "photo":
                sent = await bot.send_photo(
                    chat_id=user_id, photo=artifact.path, caption=artifact.caption
                )
                sent_media.append(
                    cached_media_from(
                        sent,
                        send_type="photo",
                        file_name=artifact.file_name,
                        caption=artifact.caption,
                    )
                )
            elif artifact.send_type == "video":
                sent = await bot.send_video(
                    chat_id=user_id,
                    video=artifact.path,
                    caption=artifact.caption,
                    supports_streaming=True,
                )
                sent_media.append(
                    cached_media_from(
                        sent,
                        send_type="video",
                        file_name=artifact.file_name,
                        caption=artifact.caption,
                    )
                )
            else:
                sent = await bot.send_document(
                    chat_id=user_id,
                    document=artifact.path,
                    caption=artifact.caption,
                )
                sent_media.append(
                    cached_media_from(
                        sent,
                        send_type="document",
                        file_name=artifact.file_name,
                        caption=artifact.caption,
                    )
                )
        else:
            sent_group = await bot.send_media_group(
                chat_id=user_id,
                media=[
                    _media_item(
                        artifact,
                        artifacts[0].caption if start == 0 and index == 0 else None,
                    )
                    for index, artifact in enumerate(chunk)
                ],
            )
            for artifact, sent in zip(chunk, sent_group):
                sent_media.append(
                    cached_media_from(
                        sent,
                        send_type=artifact.send_type,
                        file_name=artifact.file_name,
                        caption=artifact.caption,
                    )
                )
    return sent_media


async def _download_artifacts(
    *,
    bot: Bot,
    inline_message_id: str,
    stored: StoredRequest,
    settings: Settings,
    request_store: RequestStore,
) -> list[DownloadArtifact]:
    """Edit the inline message into the downloading state, then fetch the
    media via the gallery or the direct downloader."""
    await bot.edit_message_text(
        inline_message_id=inline_message_id,
        text=text.DOWNLOAD_START.format(
            name=text.esc(stored.parsed_input.source_url)
        ),
    )

    if is_twitter_status_url(stored.parsed_input.source_url):
        return await download_gallery_media(
            parsed_input=stored.parsed_input,
            settings=settings,
            work_dir=request_store.work_directory(stored.token),
        )
    option = build_direct_options(stored.parsed_input)[0]
    return [
        await download_direct_file(
            status_message=_InlineStatus(bot, inline_message_id),
            parsed_input=stored.parsed_input,
            option=option,
            settings=settings,
            work_dir=request_store.work_directory(stored.token),
        )
    ]


async def _fail_inline_message(
    bot: Bot, inline_message_id: str, message: str
) -> None:
    """Edit the inline message to a failure text; edit errors (message
    deleted, not modified) must not mask the failure handling."""
    try:
        await bot.edit_message_text(
            inline_message_id=inline_message_id,
            text=message,
        )
    except TelegramAPIError:
        pass


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


async def run_inline_download(
    *,
    bot: Bot,
    user_id: int,
    inline_message_id: str,
    stored: StoredRequest,
    settings: Settings,
    request_store: RequestStore,
    media_cache: MediaCache,
) -> None:
    token = stored.token

    try:
        artifacts = await _download_artifacts(
            bot=bot,
            inline_message_id=inline_message_id,
            stored=stored,
            settings=settings,
            request_store=request_store,
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
            bot,
            inline_message_id,
            text.TOO_LARGE.format(size=humanbytes(settings.max_upload_bytes)),
        )
        request_store.delete(token)
        return
    except Exception as exc:  # pylint: disable=broad-exception-caught  # pragma: no cover - user-facing safety boundary
        logger.exception(
            "Inline request failed | user=%s token=%s", user_id, token
        )
        await _fail_inline_message(
            bot,
            inline_message_id,
            f"{text.INLINE_FAILED}\n<code>{text.esc(str(exc))}</code>",
        )
        request_store.delete(token)
        return

    rest = artifacts[1:]
    dm_note, dm_entries = await _spillover_dm(bot, user_id, rest)

    first = artifacts[0]
    caption = (first.caption or first.file_name)[:1000] + dm_note
    edited: object = None
    try:
        edited = await bot.edit_message_media(
            inline_message_id=inline_message_id,
            media=_media_item(first, caption),
        )
    except TelegramAPIError as exc:
        logger.exception(
            "Inline media edit failed | user=%s token=%s error=%s",
            user_id,
            token,
            exc,
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
    request_store.delete(token)
