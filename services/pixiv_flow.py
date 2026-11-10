"""Pixiv profile browsing: Cover Album, Profile Page and page downloads.

The profile token lives across page switches; each page download reuses the
generic gallery downloader per Work so every Work keeps its own caption.
Pages are App API offsets, so jumping back and forth never re-scans the
artist from the newest Work.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import FSInputFile, InputMediaPhoto, Message

from config import Settings
from services import pixiv
from services.delivery import ChatDelivery, DirectDelivery
from services.gallery import download_gallery_media
from services.session import Session
from services.status import StatusSurface
from utils import text
from utils.keyboards import pixiv_page_keyboard
from utils.logging_config import safe_url_label
from utils.models import (
    ParsedInput,
    PixivProfilePayload,
    RequestType,
    StoredRequest,
)

logger = logging.getLogger(__name__)

# Works of one Profile Page fetched at once; the Session gallery limiter still
# caps how many files are in flight across the bot.
PIXIV_WORK_DOWNLOADS = 3


def payload_of(stored: StoredRequest) -> PixivProfilePayload:
    payload = stored.payload
    if isinstance(payload, PixivProfilePayload):
        return payload
    return PixivProfilePayload()


def _persist(session: Session, stored: StoredRequest, payload: PixivProfilePayload) -> None:
    session.request_store.update_payload(stored, payload)


def _header(
    payload: PixivProfilePayload, start: int, taken: list[pixiv.PixivWork]
) -> str:
    return text.PIXIV_PAGE_HEADER.format(
        artist=text.esc(payload.user_name or text.PIXIV_UNKNOWN_ARTIST),
        first=start + 1,
        last=start + len(taken),
        files=pixiv.page_file_count(taken),
    )


def _download_label(start: int, taken: list[pixiv.PixivWork]) -> str:
    return text.PIXIV_DOWNLOAD_PAGE.format(first=start + 1, last=start + len(taken))


def _record_page(
    payload: PixivProfilePayload, page: int, start: int, taken: list, more: bool
) -> None:
    """Remember where this page started and whether another one follows, so
    the pager never re-reads an artist from the top. ``offsets`` and ``more``
    stay index-aligned with the pages visited."""
    while len(payload.offsets) <= page:
        payload.offsets.append(start)
    while len(payload.more) <= page:
        payload.more.append(True)
    payload.offsets[page] = start
    payload.more[page] = more
    if len(payload.offsets) == page + 1:
        payload.offsets.append(start + len(taken))
        payload.more.append(True)


async def _load_page(
    settings: Settings, payload: PixivProfilePayload, page: int
) -> tuple[int, list[pixiv.PixivWork], bool]:
    """One window of Works from the App API, split into whole Works, plus
    whether a next Profile Page follows it."""
    if page < 0 or page >= len(payload.offsets):
        raise pixiv.PixivError(text.PIXIV_NO_MORE_PAGES)
    start = payload.offsets[page]
    works, next_offset = await pixiv.user_works(settings, payload.user_id, start)
    taken = pixiv.page_works(works)
    if not taken:
        raise pixiv.PixivError(text.PIXIV_NO_MORE_PAGES)
    return start, taken, next_offset is not None or len(taken) < len(works)


def work_labels(start: int, taken: list[pixiv.PixivWork]) -> list[str]:
    """Button per Work, labelled with its number and title (button text is
    plain, so no HTML escaping here)."""
    return [
        text.PIXIV_WORK_BUTTON.format(
            index=start + offset, title=work.title or work.work_id
        )[:64]
        for offset, work in enumerate(taken, 1)
    ]


def _keyboard(
    stored: StoredRequest,
    payload: PixivProfilePayload,
    page: int,
    start: int,
    taken: list,
):
    has_next = payload.more[page] if page < len(payload.more) else True
    return pixiv_page_keyboard(
        token=stored.token,
        page=page,
        download_label=_download_label(start, taken),
        has_next=has_next,
        works=work_labels(start, taken),
    )


async def _show_panel(
    bot: Bot,
    status: StatusSurface,
    *,
    chat_id: int,
    caption: str,
    keyboard,
    relocatable: bool,
) -> None:
    """Put the page panel under the content it controls.

    A chat message cannot be moved, so the old panel is deleted and a new one
    posted after the media. Inline surfaces keep editing their own message -
    they have nowhere else to put it."""
    if not relocatable:
        await status.edit(caption, reply_markup=keyboard)
        return
    await status.retire()
    await bot.send_message(chat_id, caption, reply_markup=keyboard)


async def _send_cover_album(bot: Bot, chat_id: int, covers: list) -> list[int]:
    """Covers travel as files: Telegram cannot fetch i.pximg.net URLs."""
    if not covers:
        return []
    if len(covers) == 1:
        sent = await bot.send_photo(chat_id=chat_id, photo=FSInputFile(covers[0]))
        return [sent.message_id]
    group = await bot.send_media_group(
        chat_id=chat_id,
        media=[InputMediaPhoto(media=FSInputFile(path)) for path in covers],
    )
    return [message.message_id for message in group]


async def _delete_previews(bot: Bot, chat_id: int, message_ids: list[int]) -> None:
    for message_id in message_ids:
        try:
            await bot.delete_message(chat_id=chat_id, message_id=message_id)
        except TelegramAPIError:
            continue


async def open_profile(
    session: Session,
    *,
    bot: Bot,
    message: Message,
    source_url: str,
    status: StatusSurface,
) -> None:
    """Resolve the artist and show its first Profile Page."""
    settings = session.settings
    await status.edit(text.PIXIV_LOADING)
    try:
        user_id = await pixiv.resolve_profile_user_id(source_url, settings)
        works, _ = await pixiv.user_works(settings, user_id, 0)
    except Exception as exc:  # noqa: BLE001 - user-facing boundary
        logger.warning(
            "Pixiv profile lookup failed | source=%s error=%s",
            safe_url_label(source_url),
            exc,
        )
        await status.fail(
            f"{text.DOWNLOAD_FAILED}\n<code>{text.esc(pixiv.friendly_error(exc))}</code>"
        )
        return
    if not works:
        await status.fail(text.PIXIV_NO_WORKS)
        return
    stored = session.request_store.open(
        RequestType.PIXIV_PROFILE,
        ParsedInput(source_url=source_url),
        payload=PixivProfilePayload(
            user_id=user_id,
            user_name=works[0].artist,
            offsets=[0],
        ),
    )
    logger.info(
        "Pixiv profile opened | token=%s user=%s",
        stored.token,
        user_id,
    )
    await render_page(
        session, bot=bot, stored=stored, page=0, chat_id=message.chat.id, status=status
    )


async def render_page(
    session: Session,
    *,
    bot: Bot,
    stored: StoredRequest,
    page: int,
    chat_id: int,
    status: StatusSurface,
) -> None:
    """Replace the visible Profile Page with another one."""
    settings = session.settings
    payload = payload_of(stored)
    try:
        started = time.monotonic()
        start, taken, more = await _load_page(settings, payload, page)
    except pixiv.PixivError as exc:
        await status.fail(text.esc(str(exc)))
        return
    fetched = time.monotonic()
    work_dir = session.request_store.work_directory(stored.token) / "previews"
    # Covers are cached on disk per Work, so a page the user returns to costs
    # no Pixiv round trip; the work directory clears them with the token.
    covers = await pixiv.download_covers(settings, taken, work_dir / str(start))
    fetched_covers = time.monotonic()
    await _delete_previews(bot, chat_id, payload.preview_ids)
    try:
        message_ids = await _send_cover_album(bot, chat_id, covers)
    except TelegramAPIError as exc:
        logger.warning("Pixiv cover album failed | token=%s error=%s", stored.token, exc)
        message_ids = []
    logger.info(
        "Pixiv page rendered | token=%s page=%s works=%s covers=%s api=%.2fs"
        " covers=%.2fs album=%.2fs",
        stored.token,
        page,
        len(taken),
        len(covers),
        fetched - started,
        fetched_covers - fetched,
        time.monotonic() - fetched_covers,
    )
    payload.preview_ids = message_ids
    _record_page(payload, page, start, taken, more)
    _persist(session, stored, payload)
    # The Work buttons belong under the previews, and an edited message cannot
    # move: drop the previous page message and post the new one after the
    # album. Inline surfaces keep their own message (see _pixiv_inline).
    await status.retire()
    await bot.send_message(
        chat_id,
        _header(payload, start, taken),
        reply_markup=_keyboard(stored, payload, page, start, taken),
    )



async def _fetch_work(
    session: Session,
    stored: StoredRequest,
    work: pixiv.PixivWork,
    status: StatusSurface,
) -> list | None:
    """One Work through the generic gallery downloader, in its own folder.
    None when that Work failed; a page download keeps going."""
    work_dir = session.request_store.work_directory(stored.token) / work.work_id
    try:
        return await download_gallery_media(
            parsed_input=ParsedInput(source_url=work.url),
            settings=session.settings,
            work_dir=work_dir,
            status=status,
            download_limiter=session.gallery_download_limiter,
        )
    except Exception as exc:  # noqa: BLE001 - one Work must not stop the page
        logger.warning(
            "Pixiv work download failed | work=%s error=%s",
            work.work_id,
            exc,
        )
        return None


async def download_work(
    session: Session,
    *,
    bot: Bot,
    stored: StoredRequest,
    page: int,
    index: int,
    status: StatusSurface,
    reply: Message | None,
    chat_id: int,
) -> None:
    """Download the one Work a page button chose, then keep the page pager."""
    payload = payload_of(stored)
    try:
        start, taken, more = await _load_page(session.settings, payload, page)
    except pixiv.PixivError as exc:
        await status.fail(text.esc(str(exc)))
        return
    if index < 0 or index >= len(taken):
        await status.fail(text.PIXIV_WORK_GONE)
        return
    work = taken[index]
    await status.edit(
        text.PIXIV_DOWNLOADING_ALL.format(total=1, files=pixiv.page_file_count([work]))
    )
    artifacts = await _fetch_work(session, stored, work, status)
    if not artifacts:
        await status.fail(
            text.PIXIV_PARTIAL.format(
                sent=0, failed=1, titles=text.esc(work.title or work.work_id)
            )
        )
        return
    if reply is None:
        await DirectDelivery(bot, chat_id).deliver(
            artifacts=artifacts, started_at=datetime.now()
        )
    else:
        await ChatDelivery(bot, reply).deliver(
            artifacts=artifacts, status=status, started_at=datetime.now()
        )
    _record_page(payload, page, start, taken, more)
    _persist(session, stored, payload)
    logger.info(
        "Pixiv work downloaded | token=%s work=%s", stored.token, work.work_id
    )
    await _show_panel(
        bot,
        status,
        chat_id=chat_id,
        caption=_header(payload, start, taken),
        keyboard=_keyboard(stored, payload, page, start, taken),
        relocatable=reply is not None,
    )



async def download_page(
    session: Session,
    *,
    bot: Bot,
    stored: StoredRequest,
    page: int,
    status: StatusSurface,
    reply: Message | None,
    chat_id: int,
) -> None:
    """Download one Profile Page: every Work whole, at most 50 files."""
    payload = payload_of(stored)
    try:
        start, taken, more = await _load_page(session.settings, payload, page)
    except pixiv.PixivError as exc:
        await status.fail(text.esc(str(exc)))
        return
    if pixiv.page_file_count(taken) > pixiv.PIXIV_PAGE_FILES:
        await status.fail(text.PIXIV_WORK_TOO_LARGE.format(pages=taken[0].pages))
        return

    # Works download concurrently (the Session's gallery limiter caps the
    # total number of files in flight); delivery stays in page order so the
    # albums of each Work keep their own captions.
    await status.edit(
        text.PIXIV_DOWNLOADING_ALL.format(
            total=len(taken), files=pixiv.page_file_count(taken)
        )
    )
    works = asyncio.Semaphore(PIXIV_WORK_DOWNLOADS)

    async def fetch(work: pixiv.PixivWork) -> list | None:
        async with works:
            return await _fetch_work(session, stored, work, status)

    chat_delivery = ChatDelivery(bot, reply) if reply is not None else None
    failed: list[str] = []
    downloaded = await asyncio.gather(*(fetch(work) for work in taken))
    for work, artifacts in zip(taken, downloaded, strict=True):
        if not artifacts:
            failed.append(work.title or work.work_id)
            continue
        if chat_delivery is None:
            await DirectDelivery(bot, chat_id).deliver(
                artifacts=artifacts, started_at=datetime.now()
            )
        else:
            await chat_delivery.deliver(
                artifacts=artifacts, status=status, started_at=datetime.now()
            )
    _record_page(payload, page, start, taken, more)
    _persist(session, stored, payload)
    logger.info(
        "Pixiv page downloaded | token=%s start=%s works=%s failed=%s",
        stored.token,
        start,
        len(taken),
        len(failed),
    )
    caption = (
        text.PIXIV_PARTIAL.format(
            sent=len(taken) - len(failed),
            failed=len(failed),
            titles=text.esc(", ".join(failed[:5])),
        )
        if failed
        else _header(payload, start, taken)
    )
    await _show_panel(
        bot,
        status,
        chat_id=chat_id,
        caption=caption,
        keyboard=_keyboard(stored, payload, page, start, taken),
        relocatable=reply is not None,
    )


def page_text(
    payload: PixivProfilePayload,
    start: int,
    taken: list[pixiv.PixivWork],
    *,
    escape: bool = True,
) -> str:
    def clean(value: str) -> str:
        return text.esc(value) if escape else value

    lines = [
        text.PIXIV_INLINE_HEADER.format(
            artist=clean(payload.user_name or text.PIXIV_UNKNOWN_ARTIST),
            first=start + 1,
            last=start + len(taken),
        )
    ]
    for offset, work in enumerate(taken, 1):
        lines.append(
            text.PIXIV_INLINE_WORK.format(
                index=start + offset, title=clean(work.title), pages=work.pages
            )
        )
    return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class PageView:
    """What an inline Profile Page shows: the text list of Works, the titles
    its buttons carry, and whether another page follows."""

    text: str
    titles: list[str]
    has_next: bool


async def page_view(session: Session, stored: StoredRequest, page: int) -> PageView:
    payload = payload_of(stored)
    start, taken, more = await _load_page(session.settings, payload, page)
    _record_page(payload, page, start, taken, more)
    _persist(session, stored, payload)
    return PageView(page_text(payload, start, taken), work_labels(start, taken), more)