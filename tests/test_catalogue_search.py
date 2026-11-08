"""Tests for catalogue search over mornye extensions (docs/adr/0003)."""

from __future__ import annotations

import asyncio
import threading
from unittest.mock import AsyncMock

import pytest
from mornye.core.errors import NetworkError

from services.catalogue_search import (
    CatalogueNetworkError,
    CatalogueSearch,
    SearchItem,
    SearchQuery,
    artist_source,
)


class _FakeExtension:
    """Sync ``_call`` double; to_thread-compatible like the real provider."""

    def __init__(self, payload=None, error: Exception | None = None) -> None:
        self.payload = payload
        self.error = error
        self.calls: list[tuple[str, tuple]] = []
        self.closed = False
        self.thread_id: int | None = None

    def _call(self, method: str, *args):
        self.calls.append((method, args))
        self.thread_id = threading.get_ident()
        if self.error is not None:
            raise self.error
        return self.payload

    def close(self) -> None:
        self.closed = True


def _patch(monkeypatch, fake: _FakeExtension, opened: list[str] | None = None):
    def _open(provider: str) -> _FakeExtension:
        if opened is not None:
            opened.append(provider)
        return fake

    monkeypatch.setattr("services.catalogue_search._open_provider", _open)
    return fake


# --- Query parsing ------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "kind", "text"),
    [
        (".trk never gonna give you up", "track", "never gonna give you up"),
        (".a dark side of the moon", "album", "dark side of the moon"),
        (".art radiohead", "artist", "radiohead"),
        (".pla chill mix", "playlist", "chill mix"),
        ("radiohead", None, "radiohead"),
        (".artist without prefix", None, ".artist without prefix"),
        (".trk", "track", ""),
    ],
)
def test_search_query_parse(raw: str, kind: str | None, text: str):
    query = SearchQuery.parse(raw)
    assert (query.kind, query.text) == (kind, text)


# --- Filter mapping -----------------------------------------------------


@pytest.mark.parametrize(
    ("provider", "kind", "mapped"),
    [
        ("deezer", None, None),
        ("deezer", "track", "track"),
        ("deezer", "album", "album"),
        ("deezer", "artist", "artist"),
        ("deezer", "playlist", "playlist"),
        ("spotify-web", None, None),
        ("spotify-web", "track", "tracks"),
        ("spotify-web", "album", "albums"),
        ("spotify-web", "artist", "artists"),
        ("spotify-web", "playlist", "playlists"),
        ("tidal-web", None, None),
        ("tidal-web", "track", "track"),
        ("tidal-web", "album", "album"),
        ("tidal-web", "artist", "artist"),
        ("tidal-web", "playlist", "playlist"),
    ],
)
@pytest.mark.asyncio
async def test_search_maps_provider_filter(monkeypatch, provider, kind, mapped):
    main_thread = threading.get_ident()
    fake = _patch(monkeypatch, _FakeExtension(payload=[]))
    service = CatalogueSearch(provider=provider)

    results = await service.search(SearchQuery(text="query", kind=kind), per_kind=5)

    assert results.items == []
    assert fake.calls == [("customSearch", ("query", {"filter": mapped, "limit": 5}))]
    assert fake.closed is True
    assert fake.thread_id is not None and fake.thread_id != main_thread


@pytest.mark.asyncio
async def test_search_empty_result(monkeypatch):
    _patch(monkeypatch, _FakeExtension(payload=[]))

    results = await CatalogueSearch().search(SearchQuery(text="nothing here"))

    assert results.items == []
    assert results.by_kind("track") == []


# --- Normalization ------------------------------------------------------


@pytest.mark.asyncio
async def test_search_normalizes_provider_items(monkeypatch):
    payload = [
        {
            "item_type": "track",
            "id": "3135556",
            "name": "Song",
            "artists": "Artist",
            "album_name": "Album",
            "cover_url": "https://img.example/cover.jpg",
            "external_urls": "https://open.spotify.com/track/3135556",
            "duration_ms": 185000,
            "release_date": "2020-01-01",
        },
        {
            "item_type": "artist",
            "id": "band1",
            "name": "Band",
            "images": [{"url": "https://img.example/band.jpg"}],
            "listeners": 1234,
        },
        {
            "item_type": "album",
            "id": "album1",
            "name": "Record",
            "artists": [{"name": "One"}, {"name": "Two"}],
            "external_urls": {"spotify": "https://open.spotify.com/album/album1"},
            "album_type": "album",
            "explicit": True,
            "total_tracks": "11",
            "release_date": "2019-05-04",
        },
        {"item_type": "video", "id": "v1", "name": "Clip"},
    ]
    _patch(monkeypatch, _FakeExtension(payload=payload))

    results = await CatalogueSearch(provider="spotify-web").search(SearchQuery(text="x"))

    assert [item.kind for item in results.items] == ["track", "artist", "album"]
    track, artist, album = results.items
    assert (track.provider, track.item_id, track.title) == (
        "spotify-web",
        "3135556",
        "Song",
    )
    assert track.artists == "Artist"
    assert track.album == "Album"
    assert track.cover_url == "https://img.example/cover.jpg"
    assert track.external_url == "https://open.spotify.com/track/3135556"
    assert track.duration_ms == 185000
    assert track.release_date == "2020-01-01"
    assert artist.cover_url == "https://img.example/band.jpg"
    assert artist.external_url == "https://open.spotify.com/artist/band1"
    assert artist.listeners == 1234
    assert album.artists == "One, Two"
    assert album.external_url == "https://open.spotify.com/album/album1"
    assert album.total_tracks == 11
    assert album.explicit is True
    assert [item.item_id for item in results.by_kind("album")] == ["album1"]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["track", "album", "artist", "playlist"])
async def test_tidal_results_never_fall_back_to_deezer_urls(monkeypatch, kind):
    """Provider-prefixed Tidal IDs must produce native Tidal source URLs."""
    _patch(
        monkeypatch,
        _FakeExtension(
            payload=[{"id": "tidal:42", "name": "Item", "item_type": kind}]
        ),
    )

    results = await CatalogueSearch().search(
        SearchQuery(text="query", kind=kind), provider="tidal-web"
    )

    assert results.items[0].external_url == f"https://www.tidal.com/{kind}/42"


@pytest.mark.asyncio
async def test_search_falls_back_to_deezer_url_and_kind(monkeypatch):
    payload = [
        {"item_type": "track", "id": "deezer:777", "name": "T"},
        {"id": "deezer:778", "name": "No type"},
    ]
    _patch(monkeypatch, _FakeExtension(payload=payload))

    results = await CatalogueSearch(provider="deezer").search(
        SearchQuery(text="q", kind="track")
    )

    assert [item.item_id for item in results.items] == ["deezer:777", "deezer:778"]
    first = results.items[0]
    assert first.external_url == "https://www.deezer.com/track/777"
    assert first.cover_url == ""
    assert first.duration_ms is None


# --- Upstream failures --------------------------------------------------


@pytest.mark.asyncio
async def test_search_upstream_error_raises(monkeypatch):
    fake = _patch(monkeypatch, _FakeExtension(error=RuntimeError("node crashed")))

    with pytest.raises(RuntimeError, match="deezer customSearch failed: node crashed"):
        await CatalogueSearch().search(SearchQuery(text="q"))

    assert fake.closed is True


@pytest.mark.asyncio
async def test_search_classifies_provider_network_failures(monkeypatch):
    fake = _patch(
        monkeypatch,
        _FakeExtension(error=NetworkError("tidal-web", "connection failed")),
    )

    with pytest.raises(CatalogueNetworkError, match="connection failed"):
        await CatalogueSearch().search(
            SearchQuery(text="query"), provider="tidal-web"
        )

    assert fake.closed is True


@pytest.mark.asyncio
async def test_search_malformed_payload_raises(monkeypatch):
    _patch(monkeypatch, _FakeExtension(payload={"items": []}))

    with pytest.raises(RuntimeError, match="malformed"):
        await CatalogueSearch().search(SearchQuery(text="q"))


@pytest.mark.asyncio
async def test_artist_empty_payload_raises(monkeypatch):
    _patch(monkeypatch, _FakeExtension(payload=None))

    with pytest.raises(RuntimeError, match="empty payload"):
        await CatalogueSearch().artist("deezer", "27")


# --- Artist catalogue ---------------------------------------------------


@pytest.mark.parametrize(
    ("provider", "payload"),
    [
        (
            "deezer",
            {
                "id": "deezer:27",
                "name": "Daft Punk",
                "listeners": 5000,
                "external_urls": "https://www.deezer.com/artist/27",
                "image_url": "https://img.example/dp.jpg",
                "item_type": "artist",
                "albums": [
                    {
                        "item_type": "album",
                        "id": "deezer:1",
                        "name": "Discovery",
                        "artists": "Daft Punk",
                        "external_urls": "https://www.deezer.com/album/1",
                        "cover_url": "https://img.example/disc.jpg",
                        "release_date": "2001-03-12",
                        "total_tracks": 14,
                        "album_type": "album",
                    }
                ],
                "top_tracks": [
                    {
                        "item_type": "track",
                        "id": "deezer:2",
                        "name": "One More Time",
                        "artists": "Daft Punk",
                        "album_name": "Discovery",
                        "external_urls": "https://www.deezer.com/track/2",
                        "duration_ms": 320000,
                    }
                ],
            },
        ),
        (
            "spotify-web",
            {
                "type": "artist",
                "artist": {
                    "id": "band1",
                    "name": "Band",
                    "image_url": "https://img.example/band.jpg",
                    "listeners": 900,
                    "provider_id": "spotify-web",
                    "albums": [
                        {
                            "id": "album1",
                            "name": "LP",
                            "artists": "Band",
                            "album_type": "album",
                            "release_date": "2024-01-01",
                            "total_tracks": 9,
                            "cover_url": "https://img.example/lp.jpg",
                            "external_urls": "https://open.spotify.com/album/album1",
                        }
                    ],
                    "top_tracks": [
                        {
                            "id": "track1",
                            "name": "Hit",
                            "artists": "Band",
                            "album_name": "LP",
                            "cover_url": "https://img.example/hit.jpg",
                            "duration_ms": 200000,
                            "external_urls": "https://open.spotify.com/track/track1",
                        }
                    ],
                },
            },
        ),
    ],
)
@pytest.mark.asyncio
async def test_artist_maps_payload(monkeypatch, provider, payload):
    _patch(monkeypatch, _FakeExtension(payload=payload))

    monkeypatch.setattr(CatalogueSearch, "_deezer_top", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        CatalogueSearch, "_deezer_discography_tracks", AsyncMock(return_value=[])
    )
    catalogue = await CatalogueSearch().artist(provider, "artist-id")

    assert catalogue.artist.kind == "artist"
    assert catalogue.artist.provider == provider
    assert [album.kind for album in catalogue.albums] == ["album"]
    assert [track.kind for track in catalogue.top_tracks] == ["track"]
    if provider == "deezer":
        assert catalogue.artist.title == "Daft Punk"
        assert catalogue.artist.listeners == 5000
        assert catalogue.artist.cover_url == "https://img.example/dp.jpg"
        assert catalogue.albums[0].item_id == "deezer:1"
        assert catalogue.top_tracks[0].title == "One More Time"
        assert catalogue.top_tracks[0].duration_ms == 320000
    else:
        assert catalogue.artist.title == "Band"
        assert catalogue.artist.listeners == 900
        assert catalogue.artist.cover_url == "https://img.example/band.jpg"
        assert catalogue.artist.external_url == "https://open.spotify.com/artist/band1"
        assert catalogue.albums[0].item_id == "album1"
        assert catalogue.albums[0].total_tracks == 9
        assert catalogue.top_tracks[0].duration_ms == 200000


# --- Provider selection -------------------------------------------------


@pytest.mark.asyncio
async def test_search_provider_override(monkeypatch):
    fake = _FakeExtension(payload=[])
    opened: list[str] = []
    _patch(monkeypatch, fake, opened)
    service = CatalogueSearch(provider="deezer")

    await service.search(SearchQuery(text="q", kind="track"), provider="spotify-web")

    assert opened == ["spotify-web"]
    assert fake.calls[0][1][1]["filter"] == "tracks"


def test_unsupported_provider_rejected():
    with pytest.raises(ValueError, match="unsupported"):
        CatalogueSearch(provider="ytmusic-spotiflac")


@pytest.mark.asyncio
async def test_search_rejects_unknown_kind():
    with pytest.raises(ValueError, match="unknown search kind"):
        await CatalogueSearch().search(SearchQuery(text="q", kind="podcast"))


# --- Persistence helpers ------------------------------------------------


def test_search_item_roundtrip():
    item = SearchItem(
        provider="deezer",
        kind="track",
        item_id="deezer:1",
        title="Song",
        artists="Artist",
        album="Album",
        cover_url="https://img.example/1.jpg",
        external_url="https://www.deezer.com/track/1",
        duration_ms=1000,
        release_date="2020-01-01",
        album_type="single",
        total_tracks=1,
        explicit=True,
        listeners=0,
    )

    data = item.to_dict()

    assert data["title"] == "Song"
    assert data["kind"] == "track"
    assert SearchItem.from_dict(data) == item
    assert SearchItem.from_dict({**data, "stale_field": "x"}) == item


# --- Artist card transport failures -------------------------------------


_ARTIST_PAYLOAD = {
    "artist": {
        "id": "deezer:27",
        "name": "Daft Punk",
        "link": "https://www.deezer.com/artist/27",
    },
    "albums": [{"item_type": "album", "id": "deezer:1", "name": "Discovery"}],
    "top_tracks": [],
}

@pytest.mark.asyncio
async def test_artist_top_deduplicates_numeric_and_prefixed_deezer_ids(monkeypatch):
    """The public and extension TOP copies must not double an artist's track list."""
    payload = {
        "artist": {"id": "deezer:27", "name": "Daft Punk"},
        "albums": [],
        "top_tracks": [{"item_type": "track", "id": "deezer:2", "name": "One More Time"}],
    }
    _patch(monkeypatch, _FakeExtension(payload=payload))
    direct = SearchItem(
        provider="deezer", kind="track", item_id="2", title="One More Time"
    )
    monkeypatch.setattr(CatalogueSearch, "_deezer_top", AsyncMock(return_value=[direct]))

    catalogue = await CatalogueSearch().artist("deezer", "27")

    assert [track.item_id for track in catalogue.top_tracks] == ["2"]

@pytest.mark.asyncio
async def test_artist_top_fills_short_list_with_unique_album_tracks(monkeypatch):
    """DeezLoad-compatible TOP 30 uses unique album tracks after the short Deezer TOP."""
    payload = {
        "artist": {
            "id": "deezer:27",
            "name": "Daft Punk",
            "albums": [{"item_type": "album", "id": "deezer:1", "name": "Discovery"}],
            "top_tracks": [],
        }
    }
    _patch(monkeypatch, _FakeExtension(payload=payload))
    direct = SearchItem(
        provider="deezer", kind="track", item_id="2", title="One More Time"
    )
    extra = [
        SearchItem(
            provider="deezer",
            kind="track",
            item_id=str(index),
            title=f"Track {index}",
        )
        for index in range(3, 32)
    ]
    monkeypatch.setattr(CatalogueSearch, "_deezer_top", AsyncMock(return_value=[direct]))
    fill = AsyncMock(return_value=extra)
    monkeypatch.setattr(CatalogueSearch, "_deezer_discography_tracks", fill)

    catalogue = await CatalogueSearch().artist("deezer", "27")

    ids = [track.item_id for track in catalogue.top_tracks]
    assert len(ids) == 30
    assert len(set(ids)) == 30
    assert fill.await_args.kwargs["exclude"] == {"2"}
    assert fill.await_args.kwargs["limit"] == 29


@pytest.mark.asyncio
async def test_artist_deezer_top_timeout_surfaces_retryable_error(monkeypatch):
    """A hung Deezer top-tracks call must surface the retryable provider error."""
    _patch(monkeypatch, _FakeExtension(payload=_ARTIST_PAYLOAD))
    monkeypatch.setattr(
        CatalogueSearch,
        "_deezer_top",
        AsyncMock(side_effect=asyncio.TimeoutError("simulated timeout")),
    )

    with pytest.raises(
        CatalogueNetworkError, match="deezer getArtist top tracks: simulated timeout"
    ):
        await CatalogueSearch().artist("deezer", "deezer:27")


@pytest.mark.asyncio
async def test_artist_deezer_top_programming_error_is_not_hidden(monkeypatch):
    """Non-transport defects stay loud instead of becoming retryable errors."""
    _patch(monkeypatch, _FakeExtension(payload=_ARTIST_PAYLOAD))
    monkeypatch.setattr(
        CatalogueSearch, "_deezer_top", AsyncMock(side_effect=KeyError("bug"))
    )

    with pytest.raises(KeyError):
        await CatalogueSearch().artist("deezer", "deezer:27")

@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://www.deezer.com/artist/123", ("deezer", "deezer:123")),
        ("https://open.spotify.com/artist/abc", ("spotify-web", "abc")),
        ("https://www.tidal.com/artist/456", ("tidal-web", "456")),
        ("https://www.deezer.com/track/123", None),
        ("https://example.com/artist/123", None),
    ],
)
def test_artist_source_recognizes_only_supported_artist_urls(url, expected):
    assert artist_source(url) == expected
