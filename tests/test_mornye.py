"""Tests for the Spotify/mornye integration (docs/adr/0002).

Mocking follows house style: patch consumer-module names as strings with
AsyncMock, keep domain objects real, never touch the network. The heavy
mornye imports live behind lazy imports inside services.mornye, so
these tests subclass the service or stub the worker instead of importing
the package.
"""

from __future__ import annotations
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from mornye.core.errors import MornyeError

from config import Settings
from services.executor import execute_request
from services.media_cache import CachedMedia
from services.request_store import RequestStore
from services.mornye import (
    SpotifyTarget,
    MornyeService,
    _artifacts_from_results,
    is_mornye_url,
)
from services.delivery import ChatDelivery
from services.status import StatusSurface
from services.thumbnail_store import ThumbnailStore
from tests.conftest import make_media_cache, make_settings
from utils.models import DownloadArtifact, DownloadOption, ParsedInput, StoredRequest


def _make_spotify_settings(tmp_path: Path) -> Settings:
    settings = make_settings(tmp_path)
    settings.mornye_services = "ext:deezer"
    return settings


def _make_track(track_id: str = "abc", title: str = "Song") -> SimpleNamespace:
    return SimpleNamespace(
        id=track_id,
        title=title,
        artist_names=("Artist",),
        duration_ms=181000,
        cover_url="https://i.scdn.co/cover.jpg",
    )


def _make_target(kind: str = "track", count: int = 1) -> SpotifyTarget:
    tracks = [_make_track(f"id{i}", f"Title{i}") for i in range(count)]
    return SpotifyTarget(
        kind=kind,
        name="Home",
        url="https://open.spotify.com/album/x",
        tracks=tracks,
        cover_url="https://i.scdn.co/cover.jpg",
    )


def _fake_service(**overrides) -> MornyeService:
    """MornyeService with heavy internals replaced by test doubles."""
    service = MornyeService(_make_spotify_settings(Path("/tmp")))
    for name, value in overrides.items():
        setattr(service, name, value)
    return service


# --- URL detection ------------------------------------------------------


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://open.spotify.com/track/x", True),
        ("https://open.spotify.com/album/4finbgKnMb3yfGdr0vErpw", True),
        ("https://open.spotify.com/playlist/y", True),
        ("https://open.spotify.com/artist/z", True),
        ("https://open.spotify.com/album/x?si=share", True),
        ("https://www.deezer.com/ru/album/1102603792", True),
        ("https://www.deezer.com/track/4301324572", True),
        ("https://deezer.com/about", False),
    ],
)
def test_is_mornye_url(url: str, expected: bool):
    assert is_mornye_url(url) is expected


# --- Resolve ------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_track_via_get_track_async():
    """Track URLs go straight through the module's get_track_async."""
    track = _make_track("mine", "Mr. Brightside")
    track.album = "Hot Fuss"
    track.album_id = "album1"
    metadata = SimpleNamespace(get_track_async=AsyncMock(return_value=track))
    service = _fake_service(_metadata_client=AsyncMock(return_value=metadata))
    target = await service.resolve_url(
        "https://open.spotify.com/track/003vvx7Niy0yvhvHt4a68B"
    )
    assert len(target.tracks) == 1
    assert target.tracks[0].id == "mine"
    assert target.name == "Artist — Mr. Brightside"
    assert target.info["album_id"] == "album1"


@pytest.mark.asyncio
async def test_resolve_collection_via_get_url():
    metadata = SimpleNamespace(
        get_url_async=AsyncMock(
            return_value=("Home", [_make_track("a"), _make_track("b")], "cover", {})
        )
    )
    service = _fake_service(_metadata_client=AsyncMock(return_value=metadata))
    target = await service.resolve_url(
        "https://open.spotify.com/album/4finbgKnMb3yfGdr0vErpw"
    )
    assert target.kind == "album"
    assert target.name == "Home"
    assert len(target.tracks) == 2


@pytest.mark.asyncio
async def test_resolve_deezer_album_via_catalogue_extension(monkeypatch):
    """deezer.com links resolve through the matching extension client
    (3-tuple answer, no ``extra`` dict) — not through Spotify metadata."""
    track = _make_track("deezer:1", "Song")
    track.first_artist = "MYAFFQ"
    track.artist_names = ()
    ext_client = SimpleNamespace(
        get_url_async=AsyncMock(return_value=("Ушки и чулки", [track], "cover"))
    )
    monkeypatch.setattr(
        "mornye.core.extension_metadata.ExtensionMetadataClient",
        SimpleNamespace(for_url=Mock(return_value=ext_client)),
    )
    service = _fake_service()
    target = await service.resolve_url("https://www.deezer.com/ru/album/1102603792")
    assert target.kind == "album"
    assert target.name == "Ушки и чулки"
    assert target.cover_url == "cover"
    assert len(target.tracks) == 1


@pytest.mark.asyncio
async def test_resolve_deezer_empty_result_raises(monkeypatch):
    catalogue_client = SimpleNamespace(
        get_url_async=AsyncMock(return_value=("", [], ""))
    )
    monkeypatch.setattr(
        "mornye.core.extension_metadata.ExtensionMetadataClient",
        SimpleNamespace(for_url=Mock(return_value=catalogue_client)),
    )
    service = _fake_service()
    with pytest.raises(RuntimeError, match="MORNYE_TRACK_UNRESOLVED"):
        await service.resolve_url("https://www.deezer.com/album/1")


@pytest.mark.asyncio
async def test_resolve_track_propagates_upstream_error():
    """A dead or malformed id fails with the upstream error — not a silent
    "Unknown"-titled pseudo-target and not MORNYE_TRACK_UNRESOLVED.
    """
    metadata = SimpleNamespace(
        get_track_async=AsyncMock(
            side_effect=MornyeError("TRACK_NOT_FOUND", "dead id")
        )
    )
    service = _fake_service(_metadata_client=AsyncMock(return_value=metadata))
    with pytest.raises(MornyeError):
        await service.resolve_url("https://open.spotify.com/track/dead")


# --- Download: cap + artifact conversion --------------------------------


@pytest.mark.asyncio
async def test_download_applies_collection_cap(monkeypatch, tmp_path):
    service = MornyeService(_make_spotify_settings(tmp_path))
    service._settings.spotify_collection_cap = 2
    captured: dict = {}

    async def _fake_worker(_target, tracks, _work_dir):
        captured["tracks"] = tracks
        return SimpleNamespace(results={})

    monkeypatch.setattr(service, "_run_worker", _fake_worker)
    target = _make_target(kind="album", count=5)
    with pytest.raises(RuntimeError, match="MORNYE_NO_RESULTS"):
        await service.download_target(target, tmp_path)
    assert len(captured["tracks"]) == 2


def test_artifacts_from_results(tmp_path: Path):
    track = _make_track("t1", "Song")
    path = tmp_path / "Song.flac"
    path.write_bytes(b"FLAC")
    results = {track.id: SimpleNamespace(success=True, file_path=str(path))}
    artifacts = _artifacts_from_results(
        results, SpotifyTarget("track", "n", "u", [track])
    )
    assert len(artifacts) == 1
    assert artifacts[0].send_type == "audio"
    assert artifacts[0].duration == 181
    assert artifacts[0].caption == "Artist — Song"


def test_artifacts_from_results_skips_failures(tmp_path: Path):
    good = _make_track("ok", "Good")
    bad = _make_track("bad", "Bad")
    good_path = tmp_path / "Good.flac"
    good_path.write_bytes(b"F")
    results = {
        "ok": SimpleNamespace(success=True, file_path=str(good_path)),
        "bad": SimpleNamespace(success=False, file_path=""),
    }
    artifacts = _artifacts_from_results(
        results, SpotifyTarget("album", "n", "u", [bad, good])
    )
    assert [artifact.path.name for artifact in artifacts] == ["Good.flac"]


# --- Executor -----------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_request_mornye_dispatch(monkeypatch, tmp_path):
    """Whole mornye collection request: download → album media-group upload."""
    settings = _make_spotify_settings(tmp_path)
    settings.ensure_directories()
    store = RequestStore(settings.requests_dir, settings.work_dir)
    thumbnails = ThumbnailStore(settings.thumbnails_dir)
    cache = make_media_cache(tmp_path)
    stored = StoredRequest(
        token="tok-s",
        request_type="mornye",
        parsed_input=ParsedInput(source_url="https://open.spotify.com/album/x"),
        options=[
            DownloadOption(option_id="spotify_flac", label="FLAC", send_type="audio")
        ],
    )
    target = _make_target(kind="album", count=2)

    f1 = tmp_path / "a.flac"
    f1.write_bytes(b"F" * 4)
    f2 = tmp_path / "b.flac"
    f2.write_bytes(b"F" * 4)
    artifacts = [
        DownloadArtifact(path=f1, file_name="a.flac", send_type="audio", caption="c"),
        DownloadArtifact(path=f2, file_name="b.flac", send_type="audio", caption="c"),
    ]

    service = SimpleNamespace(
        resolve_url=AsyncMock(return_value=target),
        download_target=AsyncMock(
            return_value=(artifacts, tmp_path / "cover.jpg")
        ),
    )
    group_mock = AsyncMock(
        return_value=[
            CachedMedia(file_id="A1", send_type="audio", file_name="a.flac", caption="c"),
            CachedMedia(file_id="A2", send_type="audio", file_name="b.flac", caption="c"),
        ]
    )
    monkeypatch.setattr("services.executor.ChatDelivery.deliver", group_mock)

    status_message = SimpleNamespace(edit_text=AsyncMock())
    source_message = SimpleNamespace(chat=SimpleNamespace(id=500))
    request_kwargs = {
        "stored": stored,
        "option": stored.options[0],
        "bot": None,
        "status": StatusSurface(status_message.edit_text),
        "source_message": source_message,
        "user_id": 99,
        "settings": settings,
        "request_store": store,
        "thumbnail_store": thumbnails,
        "media_cache": cache,
        "mornye": service,
    }
    await execute_request(**request_kwargs)
    service.resolve_url.assert_awaited_once()
    service.download_target.assert_awaited_once()
    group_mock.assert_awaited_once()
    assert group_mock.await_args.kwargs["artifacts"] == artifacts
    # Collections are not cached, only single tracks.
    assert cache.get("https://open.spotify.com/album/x") is None


@pytest.mark.asyncio
async def test_execute_request_mornye_single_track_cached(tmp_path):
    settings = _make_spotify_settings(tmp_path)
    settings.ensure_directories()
    store = RequestStore(settings.requests_dir, settings.work_dir)
    thumbnails = ThumbnailStore(settings.thumbnails_dir)
    cache = make_media_cache(tmp_path)
    url = "https://open.spotify.com/track/x"
    stored = StoredRequest(
        token="tok-s1",
        request_type="mornye",
        parsed_input=ParsedInput(source_url=url),
        options=[
            DownloadOption(option_id="spotify_flac", label="FLAC", send_type="audio")
        ],
    )
    track_file = tmp_path / "one.flac"
    track_file.write_bytes(b"F" * 4)
    artifact = DownloadArtifact(
        path=track_file, file_name="one.flac", send_type="audio", caption="c"
    )
    service = SimpleNamespace(
        resolve_url=AsyncMock(return_value=_make_target(kind="track", count=1)),
        download_target=AsyncMock(return_value=([artifact], None)),
    )
    upload_mock = AsyncMock(
        return_value=[
            CachedMedia(
                file_id="A1", send_type="audio", file_name="one.flac", caption="c"
            )
        ]
    )
    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr("services.executor.ChatDelivery.deliver", upload_mock)

        status_message = SimpleNamespace(edit_text=AsyncMock())
        source_message = SimpleNamespace(chat=SimpleNamespace(id=500))
        request_kwargs = {
            "stored": stored,
            "option": stored.options[0],
            "bot": None,
            "status": StatusSurface(status_message.edit_text),
            "source_message": source_message,
            "user_id": 99,
            "settings": settings,
            "request_store": store,
            "thumbnail_store": thumbnails,
            "media_cache": cache,
            "mornye": service,
        }
        await execute_request(**request_kwargs)
    finally:
        monkeypatch.undo()
    assert cache.get(url) is not None
    assert upload_mock.await_count == 1


# --- Upload path for FLAC -----------------------------------------------


@pytest.mark.asyncio
async def test_upload_artifact_audio_sends_reply_audio(tmp_path, monkeypatch):
    @asynccontextmanager
    async def _ctx(**_: object):
        yield None

    class _NoAction:
        upload_document = staticmethod(_ctx)
        upload_audio = staticmethod(_ctx)
        upload_photo = staticmethod(_ctx)

    monkeypatch.setattr("services.delivery.ChatActionSender", _NoAction)
    monkeypatch.setattr(
        "services.delivery.audio_duration", lambda path: 33
    )

    path = tmp_path / "song.flac"
    path.write_bytes(b"fLaC")
    artifact = DownloadArtifact(
        path=path,
        file_name="song.flac",
        send_type="audio",
        caption="Artist — Song",
        duration=181,
    )
    message = SimpleNamespace(
        chat=SimpleNamespace(id=500),
        reply_audio=AsyncMock(
            return_value=SimpleNamespace(audio=SimpleNamespace(file_id="AgAA1"))
        ),
    )
    status_message = SimpleNamespace(edit_text=AsyncMock())
    result = await ChatDelivery(None, message).deliver(
        [artifact],
        status=StatusSurface(status_message.edit_text),
        started_at=datetime.now(),
    )
    assert len(result) == 1
    assert result[0].file_id == "AgAA1"
    assert result[0].send_type == "audio"
    kwargs = message.reply_audio.await_args.kwargs
    assert kwargs["duration"] == 181
    assert kwargs["title"] == "song.flac"
