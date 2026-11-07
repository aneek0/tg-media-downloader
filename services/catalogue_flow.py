"""Artist Card and album browsing for catalogue inline results."""

from __future__ import annotations

import asyncio
import zipfile
from dataclasses import dataclass
from pathlib import Path

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from services.catalogue_search import SearchItem
from services.requests import catalogue_browse_request, music_request
from services.inline_flow import _deliver_first
from services.status import inline_caption_status, inline_status
from services.session import Session
from utils import text
from utils.callbacks import CatalogueCallback
from utils.models import (
    CataloguePayload,
    DownloadArtifact,
    FileTooLargeError,
    ParsedInput,
    StoredRequest,
)


@dataclass(slots=True)
class CatalogueCard:
    stored: StoredRequest
    caption: str
    keyboard: InlineKeyboardMarkup


def _duration(milliseconds: int | None) -> str:
    seconds = (milliseconds or 0) // 1000
    return f"{seconds // 3600}:{seconds % 3600 // 60:02}:{seconds % 60:02}"


def _short(value: str, limit: int = 64) -> str:
    return value if len(value) <= limit else f"{value[:limit - 1]}…"


def _artist_caption(payload: CataloguePayload) -> str:
    name = text.esc(payload.name)
    if payload.artist_url:
        name = f'<a href="{text.esc(payload.artist_url)}">{name}</a>'
    fans = "Fans on Deezer" if payload.provider == "deezer" else "Listeners"
    return text.ARTIST_CAPTION.format(
        name=name, listeners=payload.listeners, albums=len(payload.albums), fans=fans
    )


def _track_rows(
    session: Session, items: list[dict], card_token: str
) -> list[list[InlineKeyboardButton]]:
    rows: list[list[InlineKeyboardButton]] = []
    for raw in items:
        item = SearchItem.from_dict(raw)
        stored = music_request(
            session.request_store,
            ParsedInput(source_url=item.external_url),
            kind="track",
            name=item.title,
            count=1,
        )
        label = f"{_duration(item.duration_ms)} | {item.artists} - {item.title}"
        rows.append(
            [
                InlineKeyboardButton(
                    text=_short(label),
                    callback_data=CatalogueCallback(
                        action="dl", token=f"{card_token}.{stored.token}"
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
    if stored is None:
        stored = catalogue_browse_request(
            session.request_store, ParsedInput(source_url=catalogue.artist.external_url)
        )
    stored.parsed_input = ParsedInput(source_url=catalogue.artist.external_url)
    stored.info = CataloguePayload(
        provider=provider,
        artist_id=artist_id,
        name=catalogue.artist.title,
        cover_url=catalogue.artist.cover_url,
        artist_url=catalogue.artist.external_url,
        listeners=catalogue.artist.listeners,
        top_tracks=[item.to_dict() for item in catalogue.top_tracks[:30]],
        albums=[item.to_dict() for item in catalogue.albums[:50]],
    ).to_info()
    session.request_store.save(stored)
    return render_artist_card(session, stored, following=following)


def render_artist_card(
    session: Session,
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
                            action="open", token=f"{stored.token}.{index}"
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
            rows = _track_rows(session, payload.top_tracks, stored.token)
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
    stored = catalogue_browse_request(
        session.request_store, ParsedInput(source_url=album.external_url)
    )
    stored.info = CataloguePayload(
        provider=parent_payload.provider,
        artist_id=parent_payload.artist_id,
        name=parent_payload.name,
        cover_url=album.cover_url or parent_payload.cover_url,
        artist_url=parent_payload.artist_url,
        listeners=parent_payload.listeners,
        album=album.to_dict(),
        album_tracks=tracks,
        parent_token=parent.token,
    ).to_info()
    session.request_store.save(stored)
    return render_album_card(session, stored)


def render_album_card(session: Session, stored: StoredRequest) -> CatalogueCard:
    payload = stored.payload
    if not isinstance(payload, CataloguePayload) or not payload.album:
        raise ValueError("not an album token")
    album = SearchItem.from_dict(payload.album)
    artist = text.esc(payload.name or album.artists)
    if payload.artist_url:
        artist = f'<a href="{text.esc(payload.artist_url)}">{artist}</a>'
    caption = (
        f"💽 Album: {text.esc(album.title)}\n"
        f"👤 Artist: {artist}\n"
        f"📅 Date: {text.esc(album.release_date or 'unknown')}\n"
        f"🎧 Total tracks: {album.total_tracks or len(payload.album_tracks)}"
    )
    rows = _track_rows(session, payload.album_tracks, stored.token)
    rows.append(
        [
            InlineKeyboardButton(
                text=text.ALBUM_GET_ALL,
                callback_data=CatalogueCallback(action="dl", token=stored.token).pack(),
            ),
            InlineKeyboardButton(
                text=text.ALBUM_ZIP,
                callback_data=CatalogueCallback(action="zip", token=stored.token).pack(),
            ),
        ]
    )
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
    return CatalogueCard(stored, caption, InlineKeyboardMarkup(inline_keyboard=rows))


def album_download_request(session: Session, stored: StoredRequest) -> StoredRequest:
    payload = stored.payload
    if not isinstance(payload, CataloguePayload) or not payload.album:
        raise ValueError("not an album token")
    album = SearchItem.from_dict(payload.album)
    return music_request(
        session.request_store,
        ParsedInput(source_url=album.external_url),
        kind="album",
        name=album.title,
        count=len(payload.album_tracks),
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
    archive = work_dir / "album.zip"
    await asyncio.to_thread(_write_zip, archive, artifacts)
    size = archive.stat().st_size
    if size > session.settings.max_upload_bytes:
        raise FileTooLargeError("ZIP exceeds the configured Telegram upload limit")
    return DownloadArtifact(
        path=archive,
        file_name=f"{target.name}.zip",
        send_type="document",
        caption=target.name,
    )


def _write_zip(archive: Path, artifacts: list[DownloadArtifact]) -> None:
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_STORED) as bundle:
        for artifact in artifacts:
            bundle.write(artifact.path, arcname=artifact.path.name)


def new_release_caption(artist_name: str, titles: list[str]) -> str:
    shown = "\n".join(f"• {text.esc(title)}" for title in titles[:5])
    return f"🎤 {text.esc(artist_name)} released:\n{shown}"


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
    status = status_factory(bot, inline_message_id, name=stored.payload.name)
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
            dm_upload=False,
            dm_audio=True,
            mention=session.identity.mention,
        )
    except RuntimeError as exc:
        await status.fail(text.esc(str(exc)))
    finally:
        session.request_store.delete(stored.token)
