from __future__ import annotations

import zipfile
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from mornye.core.models import TrackMetadata

from services.catalogue_flow import (
    album_card,
    artist_card,
    download_album_zip,
    render_artist_card,
    run_album_zip,
)
from services.catalogue_search import ArtistCatalogue, SearchItem
from services.requests import catalogue_browse_request, music_request
from tests.conftest import make_session, make_settings
from utils.models import CataloguePayload, DownloadArtifact, ParsedInput
from utils.callbacks import CatalogueCallback

def _item(kind: str, index: int, **kwargs) -> SearchItem:
    values = {
        "provider": "deezer",
        "kind": kind,
        "item_id": f"deezer:{index}",
        "title": f"{kind} {index}",
        "artists": "Artist",
        "external_url": f"https://www.deezer.com/{kind}/{index}",
        **kwargs,
    }
    return SearchItem(**values)


@pytest.mark.asyncio
async def test_artist_card_has_menu_and_bounded_top_and_albums(monkeypatch, tmp_path):
    """Artist Card keeps TOP-30 behind its menu button and bounds both lists."""
    settings = make_settings(tmp_path)
    settings.ensure_directories()
    session = make_session(settings)
    catalogue = ArtistCatalogue(
        artist=_item("artist", 1, title="Artist", listeners=123),
        top_tracks=[_item("track", index, duration_ms=181000) for index in range(35)],
        albums=[_item("album", index) for index in range(55)],
    )
    monkeypatch.setattr(
        session.catalogue_search, "artist", AsyncMock(return_value=catalogue)
    )
    card = await artist_card(
        session, provider="deezer", artist_id="deezer:1"
    )
    assert len(card.stored.payload.top_tracks) == 30
    assert len(card.stored.payload.albums) == 50
    assert card.caption.splitlines()[1:] == [
        "💽 Albums: 50",
        "👥 Fans on Deezer: 123",
    ]
    assert [
        [button.text for button in row] for row in card.keyboard.inline_keyboard
    ] == [["TOP 30 🔝", "ALBUMS 💽"], ["FOLLOW ➕"]]
    menu_top = CatalogueCallback.unpack(
        card.keyboard.inline_keyboard[0][0].callback_data
    )
    assert menu_top.action == "top"
    assert menu_top.token == card.stored.token

    top_card = render_artist_card(session, card.stored, top=True)
    assert len(top_card.keyboard.inline_keyboard) == 31
    first_callback = CatalogueCallback.unpack(
        top_card.keyboard.inline_keyboard[0][0].callback_data
    )
    assert first_callback.action == "dl"
    assert first_callback.token.startswith(f"{card.stored.token}.")
    back_callback = CatalogueCallback.unpack(
        top_card.keyboard.inline_keyboard[-1][0].callback_data
    )
    assert back_callback.action == "menu"


@pytest.mark.asyncio
async def test_artist_albums_separate_singles_and_eps(monkeypatch, tmp_path):
    """Album buttons stay icon-free, with singles and EPs below one section divider."""
    settings = make_settings(tmp_path)
    settings.ensure_directories()
    session = make_session(settings)
    catalogue = ArtistCatalogue(
        artist=_item("artist", 1),
        albums=[
            _item("album", 0, title="Single", album_type="single"),
            _item("album", 1, title="LP", album_type="album", explicit=True),
            _item("album", 2, title="EP", album_type="EP", explicit=True),
            _item("album", 3, title="Compilation", album_type="compilation"),
            _item("album", 4, title="Untyped"),
        ],
    )
    monkeypatch.setattr(
        session.catalogue_search, "artist", AsyncMock(return_value=catalogue)
    )
    card = await artist_card(session, provider="deezer", artist_id="deezer:1")

    release_card = render_artist_card(session, card.stored, albums=True)

    assert [
        [button.text for button in row] for row in release_card.keyboard.inline_keyboard
    ] == [
        ["LP 🅴"],
        ["Compilation"],
        ["Untyped"],
        ["━", "Singles and EPs", "━"],
        ["Single"],
        ["EP 🅴"],
        ["BACK 🔙"],
    ]
    single_callback = CatalogueCallback.unpack(
        release_card.keyboard.inline_keyboard[4][0].callback_data
    )
    assert single_callback.action == "open"
    assert single_callback.token == f"{card.stored.token}.0"

@pytest.mark.parametrize(
    ("track_count", "expected_actions", "expected_controls"),
    [
        (1, ["dl", "zip", "albums"], [("Download ZIP 🗃️", "zip")]),
        (
            2,
            ["dl", "dl", "dl", "albums"],
            [("GET ALL ⬇️", "dl"), ("Download ZIP 🗃️", "zip")],
        ),
    ],
)
@pytest.mark.asyncio
async def test_album_card_get_all_requires_multiple_tracks(
    monkeypatch, tmp_path, track_count, expected_actions, expected_controls
):
    """GET ALL is omitted for one track but remains beside ZIP for collections."""
    settings = make_settings(tmp_path)
    settings.ensure_directories()
    session = make_session(settings)
    catalogue = ArtistCatalogue(
        artist=_item("artist", 1, title="Artist"),
        top_tracks=[],
        albums=[_item("album", 2, title="Album", release_date="2026-11-04")],
    )
    monkeypatch.setattr(
        session.catalogue_search, "artist", AsyncMock(return_value=catalogue)
    )
    parent = (
        await artist_card(session, provider="deezer", artist_id="deezer:1")
    ).stored
    target = SimpleNamespace(
        tracks=[
            TrackMetadata(
                id=str(index + 3),
                title=f"Song {index}",
                artists="Artist",
                album="Album",
                album_artist="Artist",
                external_url=f"https://www.deezer.com/track/{index + 3}",
                duration_ms=181000,
            )
            for index in range(track_count)
        ]
    )
    monkeypatch.setattr(session.mornye, "resolve_url", AsyncMock(return_value=target))
    card = await album_card(session, parent, 0)
    assert "💽 Album: Album" in card.caption
    assert "📅 Date: 2026-11-04" in card.caption
    assert [
        (track["title"], track["external_url"])
        for track in card.stored.payload.album_tracks
    ] == [
        (f"Song {index}", f"https://www.deezer.com/track/{index + 3}")
        for index in range(track_count)
    ]
    actions = [
        CatalogueCallback.unpack(row[0].callback_data).action
        for row in card.keyboard.inline_keyboard
    ]
    assert actions == expected_actions
    assert [
        (button.text, CatalogueCallback.unpack(button.callback_data).action)
        for button in card.keyboard.inline_keyboard[-2]
    ] == expected_controls


@pytest.mark.asyncio
async def test_album_zip_uses_artist_release_name_and_contains_tracks(monkeypatch, tmp_path):
    """ZIP exposes artist/release metadata and archives real audio files."""
    settings = make_settings(tmp_path)
    settings.ensure_directories()
    session = make_session(settings)
    stored = session.request_store

    request = music_request(
        stored,
        ParsedInput(source_url="https://www.deezer.com/album/2"),
        kind="album",
        name="Album",
        count=1,
    )
    target = SimpleNamespace(
        name="Album",
        tracks=[SimpleNamespace(album_artist="Artist", artists="Artist")],
    )
    audio = tmp_path / "song.flac"
    audio.write_bytes(b"fLaC-audio")
    artifact = DownloadArtifact(
        path=audio, file_name=audio.name, send_type="audio", caption="Song"
    )
    monkeypatch.setattr(session.mornye, "resolve_url", AsyncMock(return_value=target))
    monkeypatch.setattr(
        session.mornye,
        "download_target",
        AsyncMock(return_value=([artifact], None)),
    )
    zipped = await download_album_zip(session, request, 99)
    with zipfile.ZipFile(zipped.path) as bundle:
        assert bundle.namelist() == ["song.flac"]
        assert bundle.read("song.flac") == b"fLaC-audio"
    assert zipped.send_type == "document"
    assert zipped.file_name == "Artist — Album.zip"
    assert zipped.caption == "Artist — Album"


@pytest.mark.asyncio
async def test_run_album_zip_uses_browse_name_and_cleans_request(monkeypatch, tmp_path):
    """ZIP delivery must not crash on StoredRequest.source_name or leak its request."""
    settings = make_settings(tmp_path)
    settings.ensure_directories()
    session = make_session(settings)
    stored = catalogue_browse_request(
        session.request_store,
        ParsedInput(source_url="https://www.deezer.com/album/1"),
        CataloguePayload(provider="deezer", artist_id="deezer:1", name="Album"),
    )
    artifact = DownloadArtifact(
        path=tmp_path / "Album.zip",
        file_name="Album.zip",
        send_type="document",
        caption="Album",
    )
    status = SimpleNamespace(fail=AsyncMock())
    status_names = []

    def make_status(_bot, _inline_message_id, *, name):
        status_names.append(name)
        return status

    download = AsyncMock(return_value=artifact)
    deliver = AsyncMock()
    monkeypatch.setattr("services.catalogue_flow.inline_status", make_status)
    monkeypatch.setattr("services.catalogue_flow.download_album_zip", download)
    monkeypatch.setattr("services.catalogue_flow._deliver_first", deliver)

    await run_album_zip(
        session,
        bot=SimpleNamespace(),
        user_id=99,
        inline_message_id="inline-id",
        stored=stored,
        caption_status=False,
    )

    assert status_names == ["Album"]
    deliver.assert_awaited_once()
    assert session.request_store.load(stored.token) is None
