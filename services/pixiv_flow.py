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
from pathlib import Path

import aiohttp
from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import FSInputFile, InputMediaPhoto, Message

from config import Settings
from services import pixiv
from services.delivery import ChatDelivery, DirectDelivery
from services.gallery import download_gallery_media
from services.media_cache import MediaCache
from services.session import Session
from services.status import StatusSurface
from utils import text
from utils.keyboards import pixiv_page_keyboard
from utils.logging_config import safe_url_label
from utils.models import (
    CachedMedia,
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


# One artist page is a Pixiv API round trip (~2-4 s behind the proxy) plus its
# covers (~2 s), so every page is remembered once fetched and the next one is
# warmed while the user looks at this one: (user_id, start) -> Works, more.
# shortcut: in-memory only and capped; a restart or a 17th page drops it.
_PAGES: dict[tuple[str, int], tuple[list[pixiv.PixivWork], bool]] = {}
_PAGES_KEPT = 16
_prefetch_tasks: set[asyncio.Task] = set()


def _remember(
    user_id: str, start: int, taken: list[pixiv.PixivWork], more: bool
) -> None:
    while len(_PAGES) >= _PAGES_KEPT:
        _PAGES.pop(next(iter(_PAGES)))
    _PAGES[(user_id, start)] = (taken, more)


async def _load_page(
    settings: Settings, payload: PixivProfilePayload, page: int
) -> tuple[int, list[pixiv.PixivWork], bool]:
    """One window of Works from the App API, split into whole Works, plus
    whether a next Profile Page follows it."""
    if page < 0 or page >= len(payload.offsets):
        raise pixiv.PixivError(text.PIXIV_NO_MORE_PAGES)
    start = payload.offsets[page]
    cached = _PAGES.get((payload.user_id, start))
    if cached is not None:
        taken, more = cached
        return start, taken, more
    works, next_offset = await pixiv.user_works(settings, payload.user_id, start)
    taken = pixiv.page_works(works)
    if not taken:
        raise pixiv.PixivError(text.PIXIV_NO_MORE_PAGES)
    more = next_offset is not None or len(taken) < len(works)
    _remember(payload.user_id, start, taken, more)
    return start, taken, more


async def _prefetch_next(
    session: Session,
    payload: PixivProfilePayload,
    page: int,
    work_dir: Path,
) -> None:
    """Warm the page the next arrow opens: its Works and their covers."""
    if page + 1 >= len(payload.offsets) or not payload.more[page]:
        return
    start = payload.offsets[page + 1]
    if (payload.user_id, start) in _PAGES:
        return
    try:
        works, next_offset = await pixiv.user_works(
            session.settings, payload.user_id, start
        )
        taken = pixiv.page_works(works)
        if not taken:
            return
        known = _cached_cover_ids(session.media_cache, taken)
        await pixiv.download_covers(
            session.settings,
            [work for work in taken if work.work_id not in known],
            work_dir / str(start),
        )
    except (pixiv.PixivError, aiohttp.ClientError, OSError) as exc:  # warm-up only
        logger.info(
            "Pixiv page prefetch failed | user=%s start=%s error=%s",
            payload.user_id,
            start,
            exc,
        )
        return
    _remember(
        payload.user_id, start, taken, next_offset is not None or len(taken) < len(works)
    )


def _start_prefetch(
    session: Session, payload: PixivProfilePayload, page: int, work_dir: Path
) -> None:
    task = asyncio.create_task(_prefetch_next(session, payload, page, work_dir))
    _prefetch_tasks.add(task)
    task.add_done_callback(_prefetch_tasks.discard)


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


def _photo_file_id(message) -> str | None:
    photos = getattr(message, "photo", None)
    return photos[-1].file_id if photos else None


def _cover_path(page_dir: Path, work: pixiv.PixivWork) -> Path | None:
    path = page_dir / f"{work.work_id}.jpg"
    return path if path.exists() else None


def _cached_cover_ids(
    media_cache: MediaCache, works: list[pixiv.PixivWork]
) -> dict[str, str]:
    """work_id -> file_id for covers this chat already sent."""
    known: dict[str, str] = {}
    for work in works:
        cached = media_cache.get(work.cover_url) if work.cover_url else None
        if cached:
            known[work.work_id] = cached[0].file_id
    return known


async def _send_cover_album(
    media_cache: MediaCache,
    bot: Bot,
    chat_id: int,
    items: list[tuple[str, str | None, Path | None]],
) -> list[int]:
    """Covers travel as files: Telegram cannot fetch i.pximg.net URLs.

    ``items`` is ``(cover_url, file_id, path)`` per Work in page order; a
    file_id means Telegram already holds the photo, so nothing is uploaded."""
    used: list[tuple[str, str | None, Path | None]] = []
    media: list[str | FSInputFile] = []
    for item in items:
        if item[1]:
            media.append(item[1])
        elif item[2] is not None:
            media.append(FSInputFile(item[2]))
        else:
            continue
        used.append(item)
    if not media:
        return []
    if len(media) == 1:
        sent_messages = [await bot.send_photo(chat_id=chat_id, photo=media[0])]
    else:
        sent_messages = await bot.send_media_group(
            chat_id=chat_id,
            media=[InputMediaPhoto(media=item) for item in media],
        )
    await asyncio.gather(
        *(
            media_cache.record(
                url,
                [CachedMedia(file_id=new_id, send_type="photo", file_name="cover")],
            )
            for (url, uploaded, _path), message in zip(used, sent_messages, strict=False)
            if not uploaded and (new_id := _photo_file_id(message))
        )
    )
    return [message.message_id for message in sent_messages]


async def _delete_previews(bot: Bot, chat_id: int, message_ids: list[int]) -> None:
    """Drop the previous page's album in one call: a media group is a set of
    separate messages, so Telegram has no album-level delete, but
    deleteMessages takes them all at once."""
    if not message_ids:
        return
    try:
        await bot.delete_messages(chat_id=chat_id, message_ids=message_ids)
    except TelegramAPIError as exc:
        logger.info("Pixiv preview delete failed | chat=%s error=%s", chat_id, exc)


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
    awaiting = time.monotonic()
    await status.edit(text.PIXIV_LOADING)
    edited = time.monotonic()
    try:
        user_id = await pixiv.resolve_profile_user_id(source_url, settings)
        resolved = time.monotonic()
        works, next_offset = await pixiv.user_works(settings, user_id, 0)
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
    taken = pixiv.page_works(works)
    if not taken:
        await status.fail(text.PIXIV_NO_WORKS)
        return
    # The first page was just fetched; hand it to render_page instead of
    # asking Pixiv for the same window a second time.
    _remember(user_id, 0, taken, next_offset is not None or len(taken) < len(works))
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
        "Pixiv profile opened | token=%s user=%s loading=%.2fs resolve=%.2fs"
        " works=%.2fs",
        stored.token,
        user_id,
        edited - awaiting,
        resolved - edited,
        time.monotonic() - resolved,
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
    page_dir = work_dir / str(start)
    # Cover photos this chat already holds come from the media cache: no
    # download, no upload. The rest are fetched once and kept on disk under
    # the token's work directory until browsing ends.
    known = _cached_cover_ids(session.media_cache, taken)
    await pixiv.download_covers(
        settings, [work for work in taken if work.work_id not in known], page_dir
    )
    fetched_covers = time.monotonic()
    items = [
        (work.cover_url, known.get(work.work_id), _cover_path(page_dir, work))
        for work in taken
    ]
    # The old album and the old panel go away while the new album uploads;
    # waiting for ten deletes first is what made a page switch feel slow.
    old_ids = list(payload.preview_ids)

    async def drop_old() -> None:
        await asyncio.gather(
            _delete_previews(bot, chat_id, old_ids), status.retire()
        )

    started_drop = time.monotonic()
    dropped = asyncio.create_task(drop_old())
    try:
        message_ids = await _send_cover_album(session.media_cache, bot, chat_id, items)
    except TelegramAPIError as exc:
        logger.warning("Pixiv cover album failed | token=%s error=%s", stored.token, exc)
        message_ids = []
    album_done = time.monotonic()
    await dropped
    logger.info(
        "Pixiv page rendered | token=%s page=%s works=%s covers=%s api=%.2fs"
        " covers=%.2fs album=%.2fs drop=%.2fs",
        stored.token,
        page,
        len(taken),
        len(items),
        fetched - started,
        fetched_covers - fetched,
        album_done - fetched_covers,
        time.monotonic() - started_drop,
    )
    payload.preview_ids = message_ids
    _record_page(payload, page, start, taken, more)
    _persist(session, stored, payload)
    _start_prefetch(session, payload, page, work_dir)
    # The Work buttons belong under the previews; the previous panel was
    # already retired above, so this one lands after the album.
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