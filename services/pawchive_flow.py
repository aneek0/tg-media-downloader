"""Pawchive creator profile browsing: a paged list of an artist's posts.

Each post downloads through the generic gallery downloader on its post URL,
exactly like a Pixiv Work does. The profile reuses the Pixiv page keyboard
and pager, so browsing feels identical across the two art sources.
"""

from __future__ import annotations

import logging
from datetime import datetime

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import InputMediaPhoto, Message

from config import Settings
from services import art_search
from services.delivery import ChatDelivery, DirectDelivery
from services.gallery import download_gallery_media
from services.session import Session
from services.status import StatusSurface
from utils import text
from utils.keyboards import pixiv_page_keyboard
from utils.models import (
    ParsedInput,
    PawchiveProfilePayload,
    RequestType,
    StoredRequest,
)

logger = logging.getLogger(__name__)

#: Posts per profile page; matches the Pixiv Profile Page size.
_PAGE_SIZE = 10

#: In-memory page cache: (service, user_id, offset) -> (titles, post_ids, thumbs, more).
#: shortcut: capped; a restart or a 17th page drops it.
_PAGES: dict[tuple[str, str, int], tuple[list[str], list[str], list[str], bool]] = {}
_PAGES_KEPT = 16


def _remember(
    key: tuple[str, str, int],
    titles: list[str],
    post_ids: list[str],
    thumbs: list[str],
    more: bool,
) -> None:
    while len(_PAGES) >= _PAGES_KEPT:
        _PAGES.pop(next(iter(_PAGES)))
    _PAGES[key] = (titles, post_ids, thumbs, more)


async def _load_page(
    settings: Settings, payload: PawchiveProfilePayload, page: int
) -> tuple[int, list[str], list[str], list[str], bool]:
    """One window of a creator's posts (titles, ids, thumbs) plus whether a
    next page follows. Reuses the cached copy when visited before."""
    if page < 0 or page >= len(payload.offsets):
        raise art_search.PawchiveError(text.PAWCHIVE_NO_MORE_PAGES)
    offset = payload.offsets[page]
    key = (payload.service, payload.user_id, offset)
    cached = _PAGES.get(key)
    if cached is not None:
        titles, post_ids, thumbs, more = cached
        return offset, titles, post_ids, thumbs, more
    posts, next_offset = await art_search.pawchive_creator_posts(
        settings, payload.service, payload.user_id, offset, limit=_PAGE_SIZE
    )
    if not posts:
        raise art_search.PawchiveError(text.PAWCHIVE_NO_MORE_PAGES)
    titles = [title for title, _, _ in posts]
    post_ids = [post_id for _, post_id, _ in posts]
    thumbs = [thumb for _, _, thumb in posts]
    more = next_offset is not None
    _remember(key, titles, post_ids, thumbs, more)
    return offset, titles, post_ids, thumbs, more


def _record_page(
    payload: PawchiveProfilePayload,
    page: int,
    offset: int,
    post_ids: list[str],
    thumbs: list[str],
    more: bool,
) -> None:
    while len(payload.offsets) <= page:
        payload.offsets.append(offset)
    while len(payload.more) <= page:
        payload.more.append(True)
    while len(payload.post_ids) <= page:
        payload.post_ids.append([])
    while len(payload.post_thumbs) <= page:
        payload.post_thumbs.append([])
    payload.offsets[page] = offset
    payload.more[page] = more
    payload.post_ids[page] = post_ids
    payload.post_thumbs[page] = thumbs
    if len(payload.offsets) == page + 1:
        payload.offsets.append(offset + len(post_ids))
        payload.more.append(True)


def _header(payload: PawchiveProfilePayload, offset: int, titles: list[str]) -> str:
    return text.PAWCHIVE_PAGE_HEADER.format(
        artist=text.esc(payload.user_name or text.PAWCHIVE_UNKNOWN_ARTIST),
        first=offset + 1,
        last=offset + len(titles),
        count=len(titles),
    )


def post_labels(offset: int, titles: list[str]) -> list[str]:
    """Button per post, labelled with its number and title."""
    return [
        text.PAWCHIVE_POST_BUTTON.format(index=offset + i, title=title)[:64]
        for i, title in enumerate(titles, 1)
    ]


def _keyboard(
    stored: StoredRequest,
    payload: PawchiveProfilePayload,
    page: int,
    offset: int,
    titles: list[str],
):
    has_next = payload.more[page] if page < len(payload.more) else True
    return pixiv_page_keyboard(
        token=stored.token,
        page=page,
        download_label=text.PAWCHIVE_DOWNLOAD_PAGE,
        has_next=has_next,
        works=post_labels(offset, titles),
    )


async def _send_previews(bot: Bot, chat_id: int, thumbs: list[str]) -> list[int]:
    """Send one thumbnail per post as a photo album and return the sent
    message ids. Pawchive serves these on ``img.<host>/thumbnail`` which
    Telegram can fetch itself, so no local download or file_id cache."""
    photos = [thumb for thumb in thumbs if thumb]
    if not photos:
        return []
    try:
        if len(photos) == 1:
            sent = await bot.send_photo(chat_id=chat_id, photo=photos[0])
            return [sent.message_id]
        sent = await bot.send_media_group(
            chat_id=chat_id,
            media=[InputMediaPhoto(media=url) for url in photos],
        )
        return [message.message_id for message in sent]
    except TelegramAPIError as exc:
        logger.info("Pawchive previews failed | chat=%s error=%s", chat_id, exc)
        return []


async def _delete_previews(bot: Bot, chat_id: int, message_ids: list[int]) -> None:
    """Drop the previous page's album in one call."""
    if not message_ids:
        return
    try:
        await bot.delete_messages(chat_id=chat_id, message_ids=message_ids)
    except TelegramAPIError as exc:
        logger.info("Pawchive preview delete failed | chat=%s error=%s", chat_id, exc)


async def open_profile(
    session: Session,
    *,
    bot: Bot,
    message: Message,
    service: str,
    user_id: str,
    status: StatusSurface,
) -> None:
    """Resolve the creator and show its first profile page."""
    settings = session.settings
    await status.edit(text.PAWCHIVE_LOADING)
    try:
        offset, titles, post_ids, thumbs, more = await _load_page(
            settings,
            PawchiveProfilePayload(
                service=service, user_id=user_id, user_name="", offsets=[0]
            ),
            0,
        )
    except art_search.PawchiveError as exc:
        await status.fail(text.esc(str(exc)))
        return
    payload = PawchiveProfilePayload(
        service=service,
        user_id=user_id,
        user_name=user_id,
        offsets=[0],
        more=[more],
        post_ids=[post_ids],
        post_thumbs=[thumbs],
    )
    stored = session.request_store.open(
        RequestType.PAWCHIVE_PROFILE,
        ParsedInput(source_url=f"{settings.pawchive_root}/{service}/user/{user_id}"),
        payload=payload,
    )
    logger.info(
        "Pawchive profile opened | token=%s service=%s user=%s posts=%s",
        stored.token, service, user_id, len(post_ids),
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
    """Replace the visible profile page: delete the previous thumbnail
    album, send the new one, then the panel under it."""
    payload = stored.payload
    if not isinstance(payload, PawchiveProfilePayload):
        await status.fail(text.esc("Profile expired."))
        return
    try:
        offset, titles, post_ids, thumbs, more = await _load_page(
            session.settings, payload, page
        )
    except art_search.PawchiveError as exc:
        await status.fail(text.esc(str(exc)))
        return
    _record_page(payload, page, offset, post_ids, thumbs, more)
    old_ids = list(payload.preview_ids)
    preview_ids = await _send_previews(bot, chat_id, thumbs)
    if old_ids:
        await _delete_previews(bot, chat_id, old_ids)
    payload.preview_ids = preview_ids
    session.request_store.update_payload(stored, payload)
    await bot.send_message(
        chat_id,
        _header(payload, offset, titles),
        reply_markup=_keyboard(stored, payload, page, offset, titles),
    )


async def _fetch_post(
    session: Session,
    stored: StoredRequest,
    service: str,
    user_id: str,
    post_id: str,
    status: StatusSurface,
) -> list | None:
    """One post through the generic gallery downloader. None on failure."""
    root = session.settings.pawchive_root
    url = f"{root}/{service}/user/{user_id}/post/{post_id}"
    work_dir = session.request_store.work_directory(stored.token) / post_id
    try:
        return await download_gallery_media(
            parsed_input=ParsedInput(source_url=url),
            settings=session.settings,
            work_dir=work_dir,
            status=status,
            download_limiter=session.gallery_download_limiter,
        )
    except Exception as exc:  # noqa: BLE001 - one post must not stop a page
        logger.warning("Pawchive post download failed | post=%s error=%s", post_id, exc)
        return None


async def download_post(
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
    """Download the one post a page button chose, then keep the page pager."""
    payload = stored.payload
    if not isinstance(payload, PawchiveProfilePayload):
        await status.fail(text.esc("Profile expired."))
        return
    try:
        offset, titles, post_ids, thumbs, more = await _load_page(session.settings, payload, page)
    except art_search.PawchiveError as exc:
        await status.fail(text.esc(str(exc)))
        return
    if index < 0 or index >= len(post_ids):
        await status.fail(text.PAWCHIVE_POST_GONE)
        return
    post_id = post_ids[index]
    await status.edit(text.PAWCHIVE_DOWNLOADING.format(title=text.esc(titles[index])))
    artifacts = await _fetch_post(session, stored, payload.service, payload.user_id, post_id, status)
    if not artifacts:
        await status.fail(
            text.PAWCHIVE_PARTIAL.format(sent=0, failed=1, titles=text.esc(titles[index]))
        )
        return
    if reply is None:
        await DirectDelivery(bot, chat_id).deliver(artifacts, started_at=datetime.now())
    else:
        await ChatDelivery(bot, reply).deliver(
            artifacts, status=status, started_at=datetime.now()
        )
    _record_page(payload, page, offset, post_ids, thumbs, more)
    session.request_store.update_payload(stored, payload)
    await bot.send_message(
        chat_id,
        _header(payload, offset, titles),
        reply_markup=_keyboard(stored, payload, page, offset, titles),
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
    """Download every post on the current profile page, newest first."""
    payload = stored.payload
    if not isinstance(payload, PawchiveProfilePayload):
        await status.fail(text.esc("Profile expired."))
        return
    try:
        offset, titles, post_ids, thumbs, more = await _load_page(session.settings, payload, page)
    except art_search.PawchiveError as exc:
        await status.fail(text.esc(str(exc)))
        return
    await status.edit(
        text.PAWCHIVE_DOWNLOADING_ALL.format(total=len(post_ids), count=len(post_ids))
    )
    chat_delivery = ChatDelivery(bot, reply) if reply is not None else None
    failed: list[str] = []
    for post_id, title in zip(post_ids, titles, strict=True):
        artifacts = await _fetch_post(session, stored, payload.service, payload.user_id, post_id, status)
        if not artifacts:
            failed.append(title)
            continue
        if chat_delivery is None:
            await DirectDelivery(bot, chat_id).deliver(artifacts, started_at=datetime.now())
        else:
            await chat_delivery.deliver(artifacts, status=status, started_at=datetime.now())
    _record_page(payload, page, offset, post_ids, thumbs, more)
    session.request_store.update_payload(stored, payload)
    caption = (
        text.PAWCHIVE_PARTIAL.format(
            sent=len(post_ids) - len(failed),
            failed=len(failed),
            titles=text.esc(", ".join(failed[:5])),
        )
        if failed
        else _header(payload, offset, titles)
    )
    await bot.send_message(
        chat_id,
        caption,
        reply_markup=_keyboard(stored, payload, page, offset, titles),
    )