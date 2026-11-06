from __future__ import annotations

from urllib.parse import urlparse

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from services.follow_store import Follow
from services.session import Session
from utils import text
from utils.callbacks import FollowCallback

router = Router(name="follows")


def _artist_source(url: str) -> tuple[str, str]:
    parsed = urlparse(url)
    parts = [part for part in parsed.path.split("/") if part]
    try:
        index = parts.index("artist")
        artist_id = parts[index + 1]
    except (ValueError, IndexError) as exc:
        raise ValueError("Expected a public Deezer or Spotify artist URL") from exc
    if parsed.hostname and "deezer.com" in parsed.hostname:
        return "deezer", f"deezer:{artist_id}"
    if parsed.hostname == "open.spotify.com":
        return "spotify-web", artist_id
    raise ValueError("Expected a public Deezer or Spotify artist URL")


def _view(follows: list[Follow]) -> tuple[str, InlineKeyboardMarkup | None]:
    if not follows:
        return text.FOLLOW_LIST_EMPTY, None
    rows = [
        [
            InlineKeyboardButton(
                text=text.FOLLOW_REMOVE_NAME.format(name=follow.artist_name),
                callback_data=FollowCallback(
                    action="remove",
                    provider=follow.provider,
                    artist_id=follow.artist_id.removeprefix("deezer:"),
                ).pack(),
            )
        ]
        for follow in follows
    ]
    return text.FOLLOW_LIST_TITLE, InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(Command("follow"), F.chat.type == "private")
async def follow_command(message: Message, session: Session) -> None:
    if not message.from_user:
        return
    args = (message.text or "").split(maxsplit=1)
    if len(args) != 2:
        await message.answer(text.FOLLOW_USAGE)
        return
    try:
        provider, artist_id = _artist_source(args[1].strip())
        catalogue = await session.catalogue_search.artist(provider, artist_id)
        existing = session.follows.get(provider, artist_id)
        if any(follow.user_id == message.from_user.id for follow in existing):
            await message.answer(text.FOLLOW_ALREADY.format(name=catalogue.artist.title))
            return
        session.follows.toggle(
            message.from_user.id,
            provider,
            artist_id,
            catalogue.artist.title,
            cover_url=catalogue.artist.cover_url,
            known_album_ids=[album.item_id for album in catalogue.albums],
        )
    except (ValueError, RuntimeError) as exc:
        await message.answer(text.esc(str(exc)))
        return
    await message.answer(text.FOLLOW_ADDED.format(name=catalogue.artist.title))


@router.message(Command("follows", "unfollow"), F.chat.type == "private")
async def follows_command(message: Message, session: Session) -> None:
    if not message.from_user:
        return
    content, keyboard = _view(session.follows.list_for_user(message.from_user.id))
    await message.answer(content, reply_markup=keyboard)


@router.callback_query(FollowCallback.filter())
async def follow_callback(
    query: CallbackQuery, callback_data: FollowCallback, session: Session
) -> None:
    if callback_data.action != "remove":
        await query.answer()
        return
    artist_id = callback_data.artist_id
    if callback_data.provider == "deezer":
        artist_id = f"deezer:{artist_id}"
    removed = session.follows.remove(
        query.from_user.id, callback_data.provider, artist_id
    )
    if isinstance(query.message, Message):
        content, keyboard = _view(session.follows.list_for_user(query.from_user.id))
        await query.message.edit_text(content, reply_markup=keyboard)
    await query.answer(
        text.FOLLOW_REMOVED_CONFIRMATION if removed else text.FOLLOW_ALREADY_REMOVED
    )
