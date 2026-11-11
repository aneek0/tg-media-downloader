from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from services.catalogue_search import artist_source
from services.follow_store import Follow
from services.session import Session
from utils import text
from utils.callbacks import FollowCallback

router = Router(name="follows")


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
        source = artist_source(args[1].strip())
        if source is None:
            raise ValueError("Expected a public Deezer, Spotify, or Tidal artist URL")
        provider, artist_id = source
        catalogue = await session.catalogue_search.artist(provider, artist_id)
        existing = session.follows.get(provider, artist_id)
        if any(follow.user_id == message.from_user.id for follow in existing):
            await message.answer(
                text.FOLLOW_ALREADY.format(name=text.esc(catalogue.artist.title))
            )
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
    await message.answer(text.FOLLOW_ADDED.format(name=text.esc(catalogue.artist.title)))


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
