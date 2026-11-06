"""Six-hour artist catalogue snapshots and new-release notifications."""

from __future__ import annotations

import asyncio
import logging

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from services.catalogue_search import ArtistCatalogue, SearchItem
from services.follow_store import Follow
from services.session import Session
from utils import text
from utils.callbacks import FollowCallback

logger = logging.getLogger(__name__)


def _unfollow_keyboard(follow: Follow) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=text.FOLLOW_REMOVE,
                    callback_data=FollowCallback(
                        action="remove",
                        provider=follow.provider,
                        artist_id=follow.artist_id.removeprefix("deezer:"),
                    ).pack(),
                )
            ]
        ]
    )


def release_caption(artist_name: str, releases: list[SearchItem]) -> str:
    lines = []
    for release in releases[:5]:
        date = f" — {release.release_date}" if release.release_date else ""
        lines.append(f"• {text.esc(release.title)}{text.esc(date)}")
    return text.NEW_RELEASE_CAPTION.format(artist=text.esc(artist_name)) + "\n" + "\n".join(lines)


class FollowPoller:
    def __init__(self, session: Session, bot: Bot) -> None:
        self._session = session
        self._bot = bot
        self._interval = session.settings.follow_poll_seconds

    async def run(self) -> None:
        while True:
            await self.poll_once()
            await asyncio.sleep(self._interval)

    async def poll_once(self) -> None:
        for provider, artist_id in self._session.follows.all_artists():
            followers = self._session.follows.get(provider, artist_id)
            try:
                catalogue = await self._session.catalogue_search.artist(
                    provider, artist_id
                )
            except RuntimeError as exc:
                logger.info(
                    "Follow poll failed | provider=%s artist=%s error=%s",
                    provider,
                    artist_id,
                    exc,
                )
                continue
            current_ids = [album.item_id for album in catalogue.albums]
            for follow in followers:
                await self._notify_follower(follow, catalogue, current_ids)

    async def _notify_follower(
        self,
        follow: Follow,
        catalogue: ArtistCatalogue,
        current_ids: list[str],
    ) -> None:
        known = set(follow.known_album_ids)
        releases = [album for album in catalogue.albums if album.item_id not in known]
        try:
            if releases:
                caption = release_caption(follow.artist_name, releases)
                keyboard = _unfollow_keyboard(follow)
                cover = follow.cover_url or catalogue.artist.cover_url
                if cover:
                    try:
                        await self._bot.send_photo(
                            follow.user_id,
                            photo=cover,
                            caption=caption,
                            reply_markup=keyboard,
                        )
                    except TelegramAPIError:
                        await self._bot.send_message(
                            follow.user_id, caption, reply_markup=keyboard
                        )
                else:
                    await self._bot.send_message(
                        follow.user_id, caption, reply_markup=keyboard
                    )
        except TelegramAPIError as exc:
            logger.info(
                "Release notification failed | user=%s artist=%s error=%s",
                follow.user_id,
                follow.artist_id,
                exc,
            )
            return
        self._session.follows.update_user_albums(
            follow.user_id, follow.provider, follow.artist_id, current_ids
        )
