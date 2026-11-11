"""Artist Card and album browsing for catalogue inline results."""

from __future__ import annotations

import asyncio
import contextlib
import html
import zipfile
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import cast

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    Message,
)

from services.catalogue_search import SearchItem
from services.delivery import ChatDelivery
from services.inline_flow import _deliver_first
from services.session import Session
from services.status import chat_status, inline_caption_status, inline_status
from utils import text
from utils.callbacks import CatalogueCallback, join_token
from utils.models import (
    CataloguePayload,
    DownloadArtifact,
    FileTooLargeError,
    MusicPayload,
    ParsedInput,
    RequestType,
    StoredRequest,
)


@dataclass(slots=True)
class CatalogueCard:
    """What a surface needs to show one card. Rendering it never writes state;
    track tokens are minted when a track is pressed, not on every render."""

    stored: StoredRequest
    caption: str
    keyboard: InlineKeyboardMarkup
    photo_url: str | None = None


class CardSurface(StrEnum):
    """Where a card is shown: an inline message, the bot's own message, or a
    brand-new bot message."""

    INLINE = "inline"
    CHAT = "chat"
    ANSWER = "answer"


async def present_card(
    card: CatalogueCard,
    *,
    surface: CardSurface,
    bot: Bot | None = None,
    message: Message | None = None,
    inline_message_id: str | None = None,
) -> None:
    """Show one card on the surface that asked for it.

    Whether the target is a media message follows from the card's cover, so no
    flag has to travel through the payload. INLINE edits an inline message,
    CHAT edits the bot's own message (deleting it when the shape must change),
    ANSWER posts a fresh one.
    """
    if surface is CardSurface.INLINE:
        if bot is None or not inline_message_id:
            raise ValueError("Artist Cards work only in inline messages")
        if card.photo_url:
            await bot.edit_message_media(
                inline_message_id=inline_message_id,
                media=InputMediaPhoto(media=card.photo_url, caption=card.caption),
                reply_markup=card.keyboard,
            )
        else:
            await bot.edit_message_text(
                inline_message_id=inline_message_id,
                text=card.caption,
                reply_markup=card.keyboard,
            )
        return
    if message is None:
        raise ValueError("a chat surface needs its message")
    if surface is CardSurface.ANSWER:
        if card.photo_url:
            await message.answer_photo(
                photo=card.photo_url,
                caption=card.caption,
                reply_markup=card.keyboard,
            )
        else:
            await message.answer(card.caption, reply_markup=card.keyboard)
        return
    on_photo = bool(getattr(message, "photo", None))
    if card.photo_url:
        if on_photo:
            await message.edit_media(
                media=InputMediaPhoto(media=card.photo_url, caption=card.caption),
                reply_markup=card.keyboard,
            )
        else:
            await message.delete()
            await message.answer_photo(
                photo=card.photo_url,
                caption=card.caption,
                reply_markup=card.keyboard,
            )
    elif on_photo:
        await message.delete()
        await message.answer(card.caption, reply_markup=card.keyboard)
    else:
        await message.edit_text(card.caption, reply_markup=card.keyboard)


def _duration(milliseconds: int | None) -> str:
    seconds = (milliseconds or 0) // 1000
    return f"{seconds // 3600}:{seconds % 3600 // 60:02}:{seconds % 60:02}"


def _short(value: str, limit: int = 64) -> str:
    return value if len(value) <= limit else f"{value[:limit - 1]}…"


def _artist_caption(payload: CataloguePayload) -> str:
    name = text.esc(payload.name)
    if payload.artist_url:
        name = f'<a href="{html.escape(payload.artist_url, quote=True)}">{name}</a>'
    fans = "Fans on Deezer" if payload.provider == "deezer" else "Listeners"
    return text.ARTIST_CAPTION.format(
        name=name, listeners=payload.listeners, albums=len(payload.albums), fans=fans
    )


def _track_rows(
    items: list[dict], card_token: str
) -> list[list[InlineKeyboardButton]]:
    """One button per track. The token carries the index inside the card, so a
    render writes nothing and a pressed track becomes a Request on demand."""
    rows: list[list[InlineKeyboardButton]] = []
    for index, raw in enumerate(items):
        item = SearchItem.from_dict(raw)
        label = f"{_duration(item.duration_ms)} | {item.artists} - {item.title}"
        rows.append(
            [
                InlineKeyboardButton(
                    text=_short(label),
                    callback_data=CatalogueCallback(
                        action="dl", token=join_token(card_token, index)
                    ).pack(),
                )
            ]
        )
    return rows


def _follow_button(token: str, following: bool) -> InlineKeyboardButton:
    return InlineKeyboardButton(
        text=text.ARTIST_FOLLOWING if following else text.ARTIST_FOLLOW,
        callback_data=CatalogueCallback(action="follow", token=token).pack(),
    )


async def artist_card(
    session: Session,
    *,
    provider: str,
    artist_id: str,
    token: str | None = None,
    following: bool = False,
) -> CatalogueCard:
    """Fetch and persist one Artist Card; its buttons own track tokens."""
    catalogue = await session.catalogue_search.artist(provider, artist_id)
    stored = session.request_store.load(token) if token else None
    payload = CataloguePayload(
        provider=provider,
        artist_id=artist_id,
        name=catalogue.artist.title,
        cover_url=catalogue.artist.cover_url,
        artist_url=catalogue.artist.external_url,
        listeners=catalogue.artist.listeners,
        top_tracks=[item.to_dict() for item in catalogue.top_tracks[:30]],
        albums=[item.to_dict() for item in catalogue.albums[:50]],
    )
    parsed = ParsedInput(source_url=catalogue.artist.external_url)
    if stored is None:
        stored = session.request_store.open(
            RequestType.CATALOGUE_BROWSE, parsed, payload=payload
        )
    else:
        stored.parsed_input = parsed
        session.request_store.update_payload(stored, payload)
    return render_artist_card(stored, following=following)


def render_artist_card(
    stored: StoredRequest,
    *,
    following: bool = False,
    albums: bool = False,
    top: bool = False,
) -> CatalogueCard:
    """Render persisted artist state without another provider request."""
    payload = stored.payload
    if not isinstance(payload, CataloguePayload):
        raise ValueError("not an Artist Card token")
    if albums or top:
        if albums:
            rows = []
            single_rows = []
            for index, raw in enumerate(payload.albums):
                album = SearchItem.from_dict(raw)
                title = album.title + (" 🅴" if album.explicit else "")
                row = [
                    InlineKeyboardButton(
                        text=_short(title),
                        callback_data=CatalogueCallback(
                            action="open", token=join_token(stored.token, index)
                        ).pack(),
                    )
                ]
                if album.album_type.lower() in {"single", "ep"}:
                    single_rows.append(row)
                else:
                    rows.append(row)
            if single_rows:
                section_callback = CatalogueCallback(
                    action="noop", token=stored.token
                ).pack()
                rows.append(
                    [
                        InlineKeyboardButton(
                            text=text.ARTIST_SECTION_LINE, callback_data=section_callback
                        ),
                        InlineKeyboardButton(
                            text=text.ARTIST_SINGLES_EPS, callback_data=section_callback
                        ),
                        InlineKeyboardButton(
                            text=text.ARTIST_SECTION_LINE, callback_data=section_callback
                        ),
                    ]
                )
                rows.extend(single_rows)
        else:
            rows = _track_rows(payload.top_tracks, stored.token)
        rows.append(
            [
                InlineKeyboardButton(
                    text=text.ARTIST_BACK,
                    callback_data=CatalogueCallback(
                        action="menu", token=stored.token
                    ).pack(),
                )
            ]
        )
    else:
        rows = [
            [
                InlineKeyboardButton(
                    text=text.ARTIST_TOP,
                    callback_data=CatalogueCallback(
                        action="top", token=stored.token
                    ).pack(),
                ),
                InlineKeyboardButton(
                    text=text.ARTIST_ALBUMS,
                    callback_data=CatalogueCallback(
                        action="albums", token=stored.token
                    ).pack(),
                ),
            ],
            [_follow_button(stored.token, following)],
        ]
    return CatalogueCard(
        stored=stored,
        caption=_artist_caption(payload),
        keyboard=InlineKeyboardMarkup(inline_keyboard=rows),
        photo_url=payload.cover_url or None,
    )


async def album_card(
    session: Session, parent: StoredRequest, album_index: int
) -> CatalogueCard:
    """Resolve one album from a persisted Artist Card into a child token."""
    parent_payload = parent.payload
    if not isinstance(parent_payload, CataloguePayload):
        raise ValueError("not an Artist Card token")
    try:
        album = SearchItem.from_dict(parent_payload.albums[album_index])
    except (IndexError, TypeError) as exc:
        raise ValueError("unknown album") from exc
    target = await session.mornye.resolve_url(album.external_url)
    tracks = [
        SearchItem(
            provider=parent_payload.provider,
            kind="track",
            item_id=track.id,
            title=track.title,
            artists=track.artists,
            external_url=track.external_url,
            duration_ms=track.duration_ms,
        ).to_dict()
        for track in target.tracks
    ]
    stored = session.request_store.open(
        RequestType.CATALOGUE_BROWSE,
        ParsedInput(source_url=album.external_url),
        payload=CataloguePayload(
            provider=parent_payload.provider,
            artist_id=parent_payload.artist_id,
            name=parent_payload.name,
            cover_url=album.cover_url or parent_payload.cover_url,
            artist_url=parent_payload.artist_url,
            listeners=parent_payload.listeners,
            album=album.to_dict(),
            album_tracks=tracks,
            parent_token=parent.token,
        ),
    )
    return render_album_card(stored)


def render_album_card(stored: StoredRequest) -> CatalogueCard:
    payload = stored.payload
    if not isinstance(payload, CataloguePayload) or not payload.album:
        raise ValueError("not an album token")
    album = SearchItem.from_dict(payload.album)
    artist = text.esc(payload.name or album.artists)
    if payload.artist_url:
        artist = f'<a href="{html.escape(payload.artist_url, quote=True)}">{artist}</a>'
    caption = text.ALBUM_CAPTION.format(
        album=text.esc(album.title),
        artist=artist,
        date=text.esc(album.release_date or "unknown"),
        tracks=album.total_tracks or len(payload.album_tracks),
    )
    rows = _track_rows(payload.album_tracks, stored.token)
    controls = []
    if len(payload.album_tracks) > 1:
        controls.append(
            InlineKeyboardButton(
                text=text.ALBUM_GET_ALL,
                callback_data=CatalogueCallback(action="dl", token=stored.token).pack(),
            )
        )
    controls.append(
        InlineKeyboardButton(
            text=text.ALBUM_ZIP,
            callback_data=CatalogueCallback(action="zip", token=stored.token).pack(),
        )
    )
    rows.append(controls)
    rows.append(
        [
            InlineKeyboardButton(
                text=text.ALBUM_BACK,
                callback_data=CatalogueCallback(
                    action="albums", token=payload.parent_token
                ).pack(),
            )
        ]
    )
    return CatalogueCard(
        stored,
        caption,
        InlineKeyboardMarkup(inline_keyboard=rows),
        photo_url=payload.cover_url or None,
    )


def track_request(
    session: Session, card: StoredRequest, index: int
) -> StoredRequest:
    """Mint the Music Request for one pressed track.

    The card only carried the index, so a render created nothing and a stale
    button fails here rather than leaking a token nobody used.
    """
    payload = card.payload
    if not isinstance(payload, CataloguePayload):
        raise ValueError("not a catalogue token")
    tracks = payload.album_tracks if payload.album else payload.top_tracks
    try:
        item = SearchItem.from_dict(tracks[index])
    except (IndexError, TypeError) as exc:
        raise ValueError("unknown track") from exc
    return session.request_store.open(
        RequestType.MORNYE,
        ParsedInput(source_url=item.external_url),
        payload=MusicPayload(kind="track", name=item.title, count=1),
    )


def album_download_request(session: Session, stored: StoredRequest) -> StoredRequest:
    payload = stored.payload
    if not isinstance(payload, CataloguePayload) or not payload.album:
        raise ValueError("not an album token")
    album = SearchItem.from_dict(payload.album)
    return session.request_store.open(
        RequestType.MORNYE,
        ParsedInput(source_url=album.external_url),
        payload=MusicPayload(
            kind="album", name=album.title, count=len(payload.album_tracks)
        ),
    )


async def download_album_zip(
    session: Session, stored: StoredRequest, user_id: int
) -> DownloadArtifact:
    """Download an album and archive the real tracks for inline delivery."""
    target = await session.mornye.resolve_url(stored.parsed_input.source_url)
    preferences = session.preferences(user_id)
    work_dir = session.request_store.work_directory(stored.token)
    artifacts, _ = await session.mornye.download_target(
        target,
        work_dir,
        audio_format=preferences.audio_format,
        audio_quality=preferences.audio_quality,
    )
    artist = ""
    if target.tracks:
        track = target.tracks[0]
        artist = track.album_artist or track.artists
    release_name = " — ".join(part for part in (artist, target.name) if part)
    archive = work_dir / "album.zip"
    await asyncio.to_thread(_write_zip, archive, artifacts)
    size = archive.stat().st_size
    if size > session.settings.max_upload_bytes:
        raise FileTooLargeError("ZIP exceeds the configured Telegram upload limit")
    return DownloadArtifact(
        path=archive,
        file_name=f"{release_name}.zip",
        send_type="document",
        caption=text.esc(release_name),
    )


def _write_zip(archive: Path, artifacts: list[DownloadArtifact]) -> None:
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_STORED) as bundle:
        for artifact in artifacts:
            bundle.write(artifact.path, arcname=artifact.path.name)



async def run_album_zip(
    session: Session,
    *,
    bot: Bot,
    user_id: int,
    inline_message_id: str,
    stored: StoredRequest,
    caption_status: bool,
) -> None:
    """Download, archive, DM-upload, then swap the inline card to the ZIP."""
    status_factory = inline_caption_status if caption_status else inline_status
    status = status_factory(bot, inline_message_id, name=cast(CataloguePayload, stored.payload).name)
    try:
        artifact = await download_album_zip(session, stored, user_id)
        await _deliver_first(
            bot=bot,
            user_id=user_id,
            token=stored.token,
            inline_message_id=inline_message_id,
            status=status,
            first=artifact,
            caption=text.esc(artifact.file_name),
            mention=session.identity.mention,
        )
    except (RuntimeError, TelegramAPIError) as exc:
        await status.fail(text.esc(str(exc)))
    finally:
        session.request_store.delete(stored.token)


async def run_chat_album_zip(
    session: Session,
    *,
    bot: Bot,
    user_id: int,
    source_message: Message,
    stored: StoredRequest,
) -> None:
    """Archive an album and send a normal bot-authored document in DM."""
    status_message = await source_message.reply(text.PROCESSING)
    status = chat_status(status_message, name=cast(CataloguePayload, stored.payload).name)
    started_at = datetime.now()
    try:
        artifact = await download_album_zip(session, stored, user_id)
        await ChatDelivery(bot, source_message).deliver(
            [artifact], status=status, started_at=started_at
        )
    except (RuntimeError, TelegramAPIError) as exc:
        with contextlib.suppress(TelegramAPIError):
            await status.fail(text.esc(str(exc)))
    finally:
        session.request_store.delete(stored.token)
