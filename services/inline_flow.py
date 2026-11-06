from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import (
    FSInputFile,
    InputMediaAudio,
    InputMediaDocument,
    InputMediaVideo,
)

from services.media_cache import cached_media_from
from services.media import video_metadata
from services.progress import humanbytes
from services.status import StatusSurface, inline_caption_status, inline_status
from services.session import Session
from services.user_settings import UserPreferences
from services.requests import lifecycle
from services.delivery import DirectDelivery, media_item
from services.pipeline import download_inline
from services.sources import SourceKind, classify
from services.mornye import audio_cache_key
from utils import text
from utils.models import (
    CachedMedia,
    DownloadArtifact,
    FileTooLargeError,
    InlineMediaPayload,
    StoredRequest,
)

logger = logging.getLogger(__name__)


def _open_bot_hint(mention: str = "") -> str:
    return f"open {mention or text.GENERIC_BOT} and send the link"


class InlineDmBlocked(RuntimeError):
    """Telegram refused the DM copy an inline result needs (the user never
    started the bot), so the file cannot be placed into the message."""

    def __init__(self, mention: str = "") -> None:
        super().__init__(
            f"This result can only be delivered from the bot chat — {_open_bot_hint(mention)}."
        )


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
    session: Session,
    *,
    status: StatusSurface,
    stored: StoredRequest,
    audio_format: str | None = None,
    audio_quality: str | None = None,
) -> list[DownloadArtifact]:
    """Edit the inline message into the downloading state, then fetch the
    media through the shared probe ladder."""
    await status.edit(
        text.DOWNLOAD_START.format(name=text.esc(stored.parsed_input.source_url))
    )
    return await download_inline(
        session,
        stored,
        status=status,
        audio_format=audio_format,
        audio_quality=audio_quality,
    )


async def _fail_inline_message(status: StatusSurface, message: str) -> None:
    """Show a failure in the inline message; a dead message must not mask
    the failure handling."""
    await status.fail(message)


def _spillover_note(count: int, *, delivered: bool, mention: str = "") -> str:
    if not count:
        return ""
    detail = (
        f"check your DM with {mention or text.GENERIC_BOT}"
        if delivered
        else _open_bot_hint(mention)
    )
    return f"\n(+{count} more files — {detail})"


async def _spillover_dm(
    bot: Bot,
    user_id: int,
    rest: list[DownloadArtifact],
    *,
    inline_message_id: str,
    base_caption: str,
    mention: str = "",
) -> tuple[str, list[CachedMedia]]:
    """Send the remainder after the inline track and correct its caption on failure."""
    if not rest:
        return "", []
    try:
        entries = await _dm_send_artifacts(bot, user_id, rest)
        note = _spillover_note(len(rest), delivered=True, mention=mention)
    except Exception as exc:  # pylint: disable=broad-exception-caught
        logger.info("Inline DM delivery failed | user=%s error=%s", user_id, exc)
        entries = []
        note = _spillover_note(len(rest), delivered=False, mention=mention)
    if note != _spillover_note(len(rest), delivered=True, mention=mention):
        try:
            await bot.edit_message_caption(
                inline_message_id=inline_message_id,
                caption=base_caption + note,
            )
        except TelegramAPIError as exc:
            logger.info("Inline spillover caption update failed | error=%s", exc)
    return note, entries


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
    session: Session,
    *,
    bot: Bot,
    user_id: int,
    inline_message_id: str,
    stored: StoredRequest,
    caption_status: bool = False,
) -> None:
    """Own the inline request lifecycle: every terminal path deletes the
    token and its work directory."""
    with lifecycle(session.request_store, stored.token):
        await _run_inline_download(
            session,
            bot=bot,
            user_id=user_id,
            inline_message_id=inline_message_id,
            stored=stored,
            caption_status=caption_status,
        )


def _needs_dm_file_id(stored: StoredRequest, first: DownloadArtifact) -> bool:
    """Catalogue music and album ZIPs need a DM-uploaded file_id; ordinary
    direct documents still edit inline from their source URL."""
    return (
        classify(stored.parsed_input).kind is SourceKind.MUSIC
        and first.send_type in {"audio", "document"}
    )


async def _music_dm_media(
    bot: Bot, user_id: int, first: DownloadArtifact, caption: str, mention: str
) -> tuple[Any, CachedMedia]:
    """DM-upload catalogue audio/ZIP and return its inline media plus cache
    entry. Inline edits cannot upload files, so the file_id must come from
    the DM copy; a refused DM is surfaced as InlineDmBlocked."""
    try:
        uploaded = await DirectDelivery(bot, user_id).deliver(
            [first], started_at=datetime.now()
        )
    except TelegramAPIError as exc:
        logger.info("Inline DM upload refused | user=%s error=%s", user_id, exc)
        raise InlineDmBlocked(mention) from exc
    if not uploaded:
        raise RuntimeError("Telegram did not return an uploaded file id")
    dm_entry = uploaded[0]
    if first.send_type == "audio":
        return (
            InputMediaAudio(
                media=dm_entry.file_id,
                caption=caption,
                duration=first.duration,
                title=first.audio_title or first.file_name,
                performer=first.audio_performer,
            ),
            dm_entry,
        )
    return InputMediaDocument(media=dm_entry.file_id, caption=caption), dm_entry


async def _deliver_first(
    *,
    bot: Bot,
    user_id: int,
    token: str,
    inline_message_id: str,
    status: StatusSurface,
    first: DownloadArtifact,
    caption: str,
    dm_upload: bool,
    dm_audio: bool = False,
    mention: str = "",
) -> tuple[object, list[CachedMedia]]:
    """Swap the inline message into the tapped artifact and return the
    edited message plus the DM entries to cache (the poster flow prepends
    its DM video copy).

    Bot API rule: editing an INLINE message cannot upload a new file -
    media must be a file_id or a URL. For big videos (poster flow) the URL
    fetcher also fails (>20MB), so the video is uploaded to the user's DM
    first (sendVideo accepts uploads) and its file_id edits the inline
    message. The DM copy doubles as the delivery when the inline edit is
    impossible. Audio and documents need that same file_id; when Telegram
    refuses the DM (the user never started the bot) :class:`InlineDmBlocked`
    tells the caller to finish with the open-bot note instead of hanging.
    """
    item: Any
    item, cover = media_item(first, caption)
    edited: object = None
    dm_entries: list[CachedMedia] = []
    try:
        if dm_audio and first.send_type in {"audio", "document"}:
            item, dm_entry = await _music_dm_media(bot, user_id, first, caption, mention)
            dm_entries = [dm_entry, *dm_entries]
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



async def _obtain_artifacts(
    session: Session,
    *,
    bot: Bot,
    user_id: int,
    token: str,
    inline_message_id: str,
    stored: StoredRequest,
    status: StatusSurface,
    preferences: UserPreferences,
) -> list[DownloadArtifact] | None:
    """Cached audio fast path + download with the inline error boundary.
    None means the flow already finished (cache hit or reported failure)."""
    try:
        cached = session.media_cache.get(
            audio_cache_key(
                stored.parsed_input.source_url,
                preferences.audio_quality,
                preferences.audio_format,
            )
        )
        if cached and len(cached) == 1 and cached[0].send_type == "audio":
            entry = cached[0]
            await bot.edit_message_media(
                inline_message_id=inline_message_id,
                media=InputMediaAudio(
                    media=entry.file_id,
                    caption=(entry.caption or entry.file_name)[:1000] or None,
                ),
            )
            logger.info("Inline audio cache hit | user=%s token=%s", user_id, token)
            return None
        return await _download_artifacts(
            session,
            status=status,
            stored=stored,
            audio_format=preferences.audio_format,
            audio_quality=preferences.audio_quality,
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
            text.TOO_LARGE.format(
                size=humanbytes(session.settings.max_upload_bytes)
            ),
        )
        return None
    except Exception as exc:  # pylint: disable=broad-exception-caught  # pragma: no cover - user-facing safety boundary
        logger.exception(
            "Inline request failed | user=%s token=%s", user_id, token
        )
        await _fail_inline_message(
            status,
            f"{text.INLINE_FAILED}\n<code>{text.esc(str(exc))}</code>",
        )
        return None


async def _run_inline_download(
    session: Session,
    *,
    bot: Bot,
    user_id: int,
    inline_message_id: str,
    stored: StoredRequest,
    caption_status: bool = False,
) -> None:
    token = stored.token
    media_payload = (
        stored.payload
        if isinstance(stored.payload, InlineMediaPayload)
        else InlineMediaPayload()
    )
    # A poster-photo result is already a media message: text edits are
    # impossible there, so progress stays silent until the final media edit.
    if caption_status:
        status = inline_caption_status(
            bot, inline_message_id, name=text.esc(stored.parsed_input.source_url)
        )
    else:
        status = inline_status(
            bot,
            inline_message_id,
            name=text.esc(stored.parsed_input.source_url),
            media_message=bool(media_payload.poster_url),
        )

    preferences = session.preferences(user_id)
    artifacts = await _obtain_artifacts(
        session,
        bot=bot,
        user_id=user_id,
        token=token,
        inline_message_id=inline_message_id,
        stored=stored,
        status=status,
        preferences=preferences,
    )
    if artifacts is None:
        return

    # The tapped item goes into the message; the rest keep their original
    # order for the DM spillover. Videos are identified by their expected
    # download name (probe order != artifact order in mixed-media albums);
    # photos by their probe-order index.
    selected = _selected_artifact(artifacts, media_payload)
    if selected is not None and selected != 0:
        artifacts = [artifacts[selected]] + artifacts[:selected] + artifacts[selected + 1 :]

    rest = artifacts[1:]
    first = artifacts[0]
    base_caption = (first.caption or first.file_name)[:1000]
    mention = session.identity.mention
    expected_dm_note = _spillover_note(len(rest), delivered=True, mention=mention)
    caption = base_caption + expected_dm_note
    try:
        edited, dm_entries = await _deliver_first(
            bot=bot,
            user_id=user_id,
            token=token,
            inline_message_id=inline_message_id,
            status=status,
            first=first,
            caption=caption,
            dm_upload=bool(media_payload.video_url),
            dm_audio=_needs_dm_file_id(stored, first),
            mention=mention,
        )
    except InlineDmBlocked as exc:
        # No DM is possible, so neither the tapped file nor the remainder
        # can be delivered: say where to get it and stop claiming a DM.
        logger.info(
            "Inline delivery needs the bot chat | user=%s token=%s", user_id, token
        )
        await _fail_inline_message(status, text.esc(str(exc)))
        return
    _, rest_entries = await _spillover_dm(
        bot,
        user_id,
        rest,
        inline_message_id=inline_message_id,
        base_caption=base_caption,
        mention=mention,
    )
    dm_entries.extend(rest_entries)
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
        await session.media_cache.record(
            audio_cache_key(
                stored.parsed_input.source_url,
                preferences.audio_quality,
                preferences.audio_format,
            ),
            entries,
        )
