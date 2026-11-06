"""Tests for the Spotify/mornye integration (docs/adr/0002).

Mocking follows house style: patch consumer-module names as strings with
AsyncMock, keep domain objects real, never touch the network. The heavy
mornye imports live behind lazy imports inside services.mornye, so
these tests subclass the service or stub the worker instead of importing
the package.
"""

from __future__ import annotations
import asyncio
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
    audio_cache_key,
    _artifacts_from_results,
    is_mornye_url,
    _fetch_cover_aiohttp,
)
from services.delivery import ChatDelivery
from services.status import StatusSurface
from tests.conftest import make_media_cache, make_session, make_settings
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

    async def _fake_worker(_target, tracks, _work_dir, **_kwargs):
        captured["tracks"] = tracks
        return SimpleNamespace(results={})

    monkeypatch.setattr(service, "_run_worker", _fake_worker)
    target = _make_target(kind="album", count=5)
    with pytest.raises(RuntimeError, match="MORNYE_NO_RESULTS"):
        await service.download_target(target, tmp_path)
    assert len(captured["tracks"]) == 2


@pytest.mark.asyncio
async def test_download_passes_hi_res_quality(monkeypatch, tmp_path):
    """A Hi-Res preference reaches the worker as HI_RES_LOSSLESS, untranscoded."""
    service = MornyeService(_make_spotify_settings(tmp_path))
    captured: dict = {}

    async def _fake_worker(_target, _tracks, _work_dir, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(results={})

    monkeypatch.setattr(service, "_run_worker", _fake_worker)
    with pytest.raises(RuntimeError, match="MORNYE_NO_RESULTS"):
        await service.download_target(
            _make_target(kind="track", count=1),
            tmp_path,
            audio_format="flac",
            audio_quality="hires",
        )
    assert captured["quality"] == "HI_RES_LOSSLESS"
    assert captured["transcode"] == ""


@pytest.mark.asyncio
async def test_hi_res_worker_verifies_and_redownloads_fake_hires(
    monkeypatch, tmp_path
):
    """A Hi-Res request must not keep upsampled CD audio: the worker enables
    mornye's spectral check and the re-download at honest LOSSLESS."""
    service = MornyeService(_make_spotify_settings(tmp_path))
    captured: dict = {}

    class _FakeWorker:
        def __init__(self, **kwargs):
            captured["opts"] = kwargs["opts"]

        async def run_async(self) -> None:
            return None

    monkeypatch.setattr(
        "mornye.application.download_engine.DownloadWorker", _FakeWorker
    )
    await service._run_worker(
        _make_target(kind="track", count=1),
        [],
        tmp_path,
        quality="HI_RES_LOSSLESS",
    )
    assert captured["opts"].verify_hires is True
    assert captured["opts"].redownload_fake_hires is True


@pytest.mark.asyncio
async def test_lossless_worker_skips_hires_verification(monkeypatch, tmp_path):
    """CD lossless is exactly what was asked for — no spectral check needed."""
    service = MornyeService(_make_spotify_settings(tmp_path))
    captured: dict = {}

    class _FakeWorker:
        def __init__(self, **kwargs):
            captured["opts"] = kwargs["opts"]

        async def run_async(self) -> None:
            return None

    monkeypatch.setattr(
        "mornye.application.download_engine.DownloadWorker", _FakeWorker
    )
    await service._run_worker(
        _make_target(kind="track", count=1),
        [],
        tmp_path,
        quality="LOSSLESS",
    )
    assert captured["opts"].verify_hires is False


@pytest.mark.asyncio
async def test_download_mp3_asks_for_lossless_not_hi_res(monkeypatch, tmp_path):
    """An MP3 transcode would discard Hi-Res, so it requests CD lossless."""
    service = MornyeService(_make_spotify_settings(tmp_path))
    captured: dict = {}

    async def _fake_worker(_target, _tracks, _work_dir, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(results={})

    monkeypatch.setattr(service, "_run_worker", _fake_worker)
    with pytest.raises(RuntimeError, match="MORNYE_NO_RESULTS"):
        await service.download_target(
            _make_target(kind="track", count=1),
            tmp_path,
            audio_format="mp3",
            audio_quality="hires",
        )
    assert captured["quality"] == "LOSSLESS"
    assert captured["transcode"] == "mp3"


@pytest.mark.parametrize(
    "url, quality, audio_format, expected",
    [
        (
            "https://open.spotify.com/track/abc",
            "lossless",
            "flac",
            "https://open.spotify.com/track/abc",
        ),
        (
            "https://open.spotify.com/track/abc",
            "hires",
            "flac",
            "https://open.spotify.com/track/abc#hires",
        ),
        # MP3 is its own file; a Hi-Res master only gets transcoded away,
        # so an MP3 request must never mint a meaningless #hires entry.
        (
            "https://open.spotify.com/track/abc",
            "lossless",
            "mp3",
            "https://open.spotify.com/track/abc#mp3",
        ),
        (
            "https://open.spotify.com/track/abc",
            "hires",
            "mp3",
            "https://open.spotify.com/track/abc#mp3",
        ),
        (
            "https://example.com/clip.mp4",
            "hires",
            "flac",
            "https://example.com/clip.mp4",
        ),
        (
            "https://example.com/clip.mp4",
            "lossless",
            "mp3",
            "https://example.com/clip.mp4",
        ),
    ],
)
def test_audio_cache_key_separates_format_and_tier(
    url: str, quality: str, audio_format: str, expected: str
):
    """FLAC and MP3 of one URL must never answer each other, a Hi-Res file
    never answers a CD request, and non-audio URLs keep one plain key."""
    assert audio_cache_key(url, quality, audio_format) == expected


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


def test_artifacts_from_results_restores_album_order_after_parallel_downloads(tmp_path):
    """Completion order must not decide which album track becomes the inline result."""
    tracks = [_make_track(str(index), title) for index, title in enumerate(("Вы", "Мы", "Я"))]
    paths = {}
    for track in tracks:
        path = tmp_path / f"{track.title}.flac"
        path.write_bytes(b"F")
        paths[track.id] = path
    results = {
        track_id: SimpleNamespace(success=True, file_path=str(paths[track_id]))
        for track_id in (tracks[2].id, tracks[0].id, tracks[1].id)
    }

    artifacts = _artifacts_from_results(
        results, SpotifyTarget("album", "Album", "url", tracks)
    )

    assert [artifact.audio_title for artifact in artifacts] == ["Вы", "Мы", "Я"]


# --- Executor -----------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_request_mornye_dispatch(monkeypatch, tmp_path):
    """Whole mornye collection request: download → album media-group upload."""
    settings = _make_spotify_settings(tmp_path)
    settings.ensure_directories()
    store = RequestStore(settings.requests_dir, settings.work_dir)
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
    session = make_session(settings, store, cache, mornye=service)
    await execute_request(
        session,
        stored,
        stored.options[0],
        bot=None,
        status=StatusSurface(status_message.edit_text),
        source_message=source_message,
        user_id=99,
    )
    service.resolve_url.assert_awaited_once()
    service.download_target.assert_awaited_once()
    assert service.download_target.await_args.kwargs["audio_quality"] == "lossless"
    group_mock.assert_awaited_once()
    assert group_mock.await_args.kwargs["artifacts"] == artifacts
    # Collections are not cached, only single tracks.
    assert cache.get("https://open.spotify.com/album/x") is None


@pytest.mark.asyncio
async def test_execute_request_mornye_single_track_cached(tmp_path):
    settings = _make_spotify_settings(tmp_path)
    settings.ensure_directories()
    store = RequestStore(settings.requests_dir, settings.work_dir)
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
        session = make_session(settings, store, cache, mornye=service)
        await execute_request(
            session,
            stored,
            stored.options[0],
            bot=None,
            status=StatusSurface(status_message.edit_text),
            source_message=source_message,
            user_id=99,
        )
    finally:
        monkeypatch.undo()
    assert cache.get(url) is not None
    assert upload_mock.await_count == 1


@pytest.mark.asyncio
async def test_execute_request_uses_the_users_hi_res_preference(
    monkeypatch, tmp_path
):
    """The /settings tier must reach the downloader, not the env default."""
    settings = _make_spotify_settings(tmp_path)
    settings.ensure_directories()
    store = RequestStore(settings.requests_dir, settings.work_dir)
    stored = StoredRequest(
        token="tok-h",
        request_type="mornye",
        parsed_input=ParsedInput(source_url="https://open.spotify.com/track/x"),
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
    monkeypatch.setattr(
        "services.executor.ChatDelivery.deliver",
        AsyncMock(
            return_value=[
                CachedMedia(
                    file_id="A1", send_type="audio", file_name="one.flac", caption="c"
                )
            ]
        ),
    )
    session = make_session(settings, store, make_media_cache(tmp_path), mornye=service)
    session.update_preferences(99, audio_quality="hires")

    await execute_request(
        session,
        stored,
        stored.options[0],
        bot=None,
        status=StatusSurface(AsyncMock()),
        source_message=SimpleNamespace(chat=SimpleNamespace(id=500)),
        user_id=99,
    )

    assert service.download_target.await_args.kwargs["audio_quality"] == "hires"


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


@pytest.mark.asyncio
async def test_cover_fetch_retries_timeout_with_aiohttp(monkeypatch):
    """Regression: mornye's httpx cover timeout must not strip artwork or stall three times."""
    class FakeResponse:
        raise_for_status = Mock()
        read = AsyncMock(return_value=b"cover-bytes")

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

    response = FakeResponse()

    class FakeSession:
        instance = None

        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.calls = 0
            FakeSession.instance = self

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        def get(self, _url):
            self.calls += 1
            if self.calls == 1:
                raise asyncio.TimeoutError
            return response

    sleep = AsyncMock()
    monkeypatch.setattr("services.mornye.aiohttp.ClientSession", FakeSession)
    monkeypatch.setattr("services.mornye.asyncio.sleep", sleep)

    result = await _fetch_cover_aiohttp("https://example.test/cover.jpg")

    assert result == b"cover-bytes"
    assert FakeSession.instance.calls == 2
    assert FakeSession.instance.kwargs["trust_env"] is True
    assert FakeSession.instance.kwargs["timeout"].total == 15
    sleep.assert_awaited_once_with(1.5)


@pytest.mark.asyncio
async def test_signed_session_passes_use_fresh_clients_and_close(monkeypatch, tmp_path):
    settings = _make_spotify_settings(tmp_path)
    settings.mornye_services = "tidal-web,ext:tidal-web"
    service = MornyeService(settings)
    clients = [
        SimpleNamespace(
            authenticated=True,
            expires_at="2099-01-01T00:00:00Z",
            ensure_session=AsyncMock(),
            aclose=AsyncMock(),
        )
        for _ in range(2)
    ]
    factory = Mock(side_effect=clients)
    manager = SimpleNamespace(
        get_installed=Mock(return_value=SimpleNamespace(manifest={"signedSession": {}}))
    )
    monkeypatch.setattr("mornye.extensions.extension_id", lambda _service, _manager: "tidal-web")
    monkeypatch.setattr("mornye.extensions.runtime_features.signed_session_client", factory)

    await service.renew_signed_sessions_once(manager)
    await service.renew_signed_sessions_once(manager)

    assert factory.call_count == 2
    for client in clients:
        client.ensure_session.assert_awaited_once()
        client.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_missing_signed_session_authenticates_without_download(monkeypatch):
    client = SimpleNamespace(
        authenticated=False,
        expires_at=None,
        ensure_session=AsyncMock(),
        authenticate_with_turnstile=AsyncMock(),
    )

    async def authenticate(timeout):
        assert timeout == 90
        client.authenticated = True
        client.expires_at = "2099-01-01T00:00:00Z"

    client.authenticate_with_turnstile.side_effect = authenticate
    monkeypatch.setattr("mornye.core.signed_session_mobile.auth_backoff_remaining", lambda _client: 0)

    await MornyeService._maintain_signed_client("tidal-web", client)

    client.ensure_session.assert_not_awaited()
    client.authenticate_with_turnstile.assert_awaited_once_with(timeout=90)


@pytest.mark.asyncio
async def test_signed_session_maintenance_renews_without_user_request(monkeypatch, tmp_path):
    service = MornyeService(_make_spotify_settings(tmp_path))
    renewed = asyncio.Event()
    passes = 0

    async def renew(*_args, **_kwargs):
        nonlocal passes
        passes += 1
        if passes == 2:
            renewed.set()

    monkeypatch.setattr("services.mornye.MornyeService.renew_signed_sessions_once", renew)
    monkeypatch.setattr("services.mornye._SESSION_MAINTENANCE_SECONDS", 0.001)
    task = asyncio.create_task(service.run_signed_session_maintenance())
    await asyncio.wait_for(renewed.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert passes >= 2


# --- Tidal links ---------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://listen.tidal.com/album/413114053", True),
        ("https://tidal.com/browse/track/123", True),
        ("https://www.tidal.com/track/123", True),
        ("https://tidal.com/", False),
        ("https://example.com/album/413114053", False),
    ],
)
def test_tidal_links_classify_as_music(url: str, expected: bool):
    """Tidal links must enter the mornye ladder, not yt-dlp/direct."""
    assert is_mornye_url(url) is expected


@pytest.mark.asyncio
async def test_resolve_tidal_album_uses_tidal_metadata(tmp_path):
    service = MornyeService(_make_spotify_settings(tmp_path))
    tracks = [_make_track("tidal_413114054", "Song")]
    tidal = SimpleNamespace(
        get_url=AsyncMock(return_value=("Album", tracks, "cover.jpg", {}))
    )
    service._tidal = tidal  # pylint: disable=protected-access

    target = await service.resolve_url("https://listen.tidal.com/album/413114053")

    assert target.kind == "album"
    assert target.name == "Album"
    assert target.tracks is tracks
    tidal.get_url.assert_awaited_once_with(
        "https://listen.tidal.com/album/413114053"
    )


@pytest.mark.asyncio
async def test_tidal_artist_links_fail_instead_of_falling_to_ytdlp(tmp_path):
    """A discography is not a track list; say so instead of downloading HTML."""
    service = MornyeService(_make_spotify_settings(tmp_path))

    with pytest.raises(RuntimeError, match="artist and mix"):
        await service.resolve_url("https://listen.tidal.com/artist/1566")


# --- SoundCloud links ---------------------------------------------------


def _soundcloud_payload() -> dict:
    return {
        "success": True,
        "type": "track",
        "track": {
            "id": "299651533",
            "name": "Kobaryo - Bookmaker [F/C Fionaredica]",
            "artists": "Kobaryo",
            "album_artist": "Kobaryo",
            "duration_ms": 180078,
            "cover_url": "https://i1.sndcdn.com/artworks.jpg",
            "external_urls": "https://soundcloud.com/kobaryo/bookmaker",
            "provider_id": "soundcloud",
        },
    }


def _soundcloud_target() -> SpotifyTarget:
    raw = _soundcloud_payload()["track"]
    track = _make_track(raw["id"], raw["name"])
    track.artists = raw["artists"]
    track.album = "Unknown"
    track.album_artist = raw["album_artist"]
    track.first_artist = raw["artists"]
    track.duration_ms = raw["duration_ms"]
    track.cover_url = raw["cover_url"]
    track.external_url = raw["external_urls"]
    return SpotifyTarget(
        kind="track",
        name=raw["name"],
        url=raw["external_urls"],
        tracks=[track],
        cover_url=raw["cover_url"],
        info={
            "provider": "soundcloud",
            "raw_tracks": [raw],
            "original_url": "https://on.soundcloud.com/SCQgx6vjEYw6BnWXGB",
        },
    )


@pytest.mark.parametrize(
    "url",
    [
        "https://on.soundcloud.com/SCQgx6vjEYw6BnWXGB",
        "https://soundcloud.com/kobaryo/bookmaker",
        "https://m.soundcloud.com/kobaryo/bookmaker",
    ],
)
def test_soundcloud_links_classify_as_music(url: str):
    assert is_mornye_url(url) is True


@pytest.mark.asyncio
async def test_resolve_soundcloud_uses_extension_handle_url(monkeypatch, tmp_path):
    handle_url = AsyncMock(return_value=_soundcloud_payload())
    monkeypatch.setattr("services.mornye._soundcloud_handle_url", handle_url)
    service = MornyeService(_make_spotify_settings(tmp_path))

    target = await service.resolve_url(
        "https://on.soundcloud.com/SCQgx6vjEYw6BnWXGB"
    )

    assert target.info["provider"] == "soundcloud"
    assert target.url == "https://soundcloud.com/kobaryo/bookmaker"
    assert target.tracks[0].id == "299651533"
    assert target.cover_url == "https://i1.sndcdn.com/artworks.jpg"


@pytest.mark.asyncio
async def test_soundcloud_metadata_failure_preserves_ytdlp_download(
    monkeypatch, tmp_path
):
    """A missing/broken extension must not make a working SoundCloud URL fail."""
    monkeypatch.setattr(
        "services.mornye._soundcloud_handle_url",
        AsyncMock(side_effect=RuntimeError("extension unavailable")),
    )
    info = {
        "id": "299651533",
        "title": "Bookmaker",
        "uploader": "Kobaryo",
        "duration": 180,
        "webpage_url": "https://soundcloud.com/kobaryo/bookmaker",
        "formats": [{"vcodec": "none"}],
    }
    probe = AsyncMock(return_value=info)
    monkeypatch.setattr("services.ytdlp.probe_url", probe)
    media = tmp_path / "bookmaker.mp3"
    media.write_bytes(b"mp3")
    artifact = DownloadArtifact(
        path=media, file_name=media.name, send_type="audio", caption=""
    )
    download = AsyncMock(return_value=artifact)
    monkeypatch.setattr("services.ytdlp.download_best_quality", download)
    service = MornyeService(_make_spotify_settings(tmp_path))

    target = await service.resolve_url(
        "https://on.soundcloud.com/SCQgx6vjEYw6BnWXGB"
    )
    artifacts, _ = await service.download_target(target, tmp_path)

    assert target.info["provider"] == "soundcloud_ytdlp"
    assert target.tracks[0].title == "Bookmaker"
    assert artifacts == [artifact]
    assert download.await_args.kwargs["info"] is info
    probe.assert_awaited_once()


@pytest.mark.asyncio
async def test_soundcloud_native_download_uses_exact_extension_track(
    monkeypatch, tmp_path
):
    """Native SoundCloud downloads must not search for a prefixed track ID."""
    target = _soundcloud_target()
    media = tmp_path / "soundcloud-native" / "bookmaker.m4a"
    media.parent.mkdir(parents=True)
    media.write_bytes(b"m4a")
    result = SimpleNamespace(success=True, skipped=False, file_path=str(media))
    provider = SimpleNamespace(
        download_track_async=AsyncMock(return_value=result), close=Mock()
    )
    provider_factory = Mock(return_value=provider)
    monkeypatch.setattr("services.mornye._soundcloud_provider", provider_factory)
    fallback = AsyncMock()
    service = MornyeService(_make_spotify_settings(tmp_path))
    monkeypatch.setattr(service, "_download_soundcloud_ytdlp", fallback)

    artifacts, _ = await service.download_target(target, tmp_path, audio_format="flac")

    assert [artifact.path for artifact in artifacts] == [media]
    provider_factory.assert_called_once_with(target.info["raw_tracks"][0])
    assert provider.download_track_async.await_args.kwargs["quality"] == "aac_160"
    fallback.assert_not_awaited()


@pytest.mark.asyncio
async def test_soundcloud_not_playable_falls_back_to_ytdlp(monkeypatch, tmp_path):
    """A geo/DRM-limited native stream must retain the working yt-dlp path."""
    target = _soundcloud_target()
    result = SimpleNamespace(
        success=False, skipped=False, file_path=None, error="not playable"
    )
    provider = SimpleNamespace(
        download_track_async=AsyncMock(return_value=result), close=Mock()
    )
    monkeypatch.setattr("services.mornye._soundcloud_provider", lambda _raw: provider)
    media = tmp_path / "bookmaker.mp3"
    media.write_bytes(b"mp3")
    artifact = DownloadArtifact(
        path=media, file_name=media.name, send_type="audio", caption=""
    )
    fallback = AsyncMock(return_value=([artifact], None))
    service = MornyeService(_make_spotify_settings(tmp_path))
    monkeypatch.setattr(service, "_download_soundcloud_ytdlp", fallback)

    artifacts, _ = await service.download_target(target, tmp_path, audio_format="flac")

    assert artifacts == [artifact]
    fallback.assert_awaited_once_with(target, tmp_path, target.tracks)
    assert not (tmp_path / "soundcloud-native").exists()


def _soundcloud_playlist_payload() -> dict:
    return {
        "success": True,
        "type": "playlist",
        "name": "Hardcore Syndrome",
        "cover_url": "https://i1.sndcdn.com/playlist.jpg",
        "tracks": [
            {
                "id": "1",
                "name": "First",
                "artists": "Kobaryo",
                "duration_ms": 100000,
                "external_urls": "https://soundcloud.com/kobaryo/first",
            },
            {
                "id": "2",
                "name": "Second",
                "artists": "Kobaryo",
                "duration_ms": 120000,
                "external_urls": "https://soundcloud.com/kobaryo/second",
            },
        ],
    }


@pytest.mark.asyncio
async def test_soundcloud_playlist_keeps_collection_identity(monkeypatch, tmp_path):
    """A playlist must not masquerade as its first track: the target keeps
    the collection URL, otherwise a retry forgets what it downloads."""
    playlist_url = "https://soundcloud.com/kobaryo/sets/hardcore"
    monkeypatch.setattr(
        "services.mornye._soundcloud_handle_url",
        AsyncMock(return_value=_soundcloud_playlist_payload()),
    )
    service = MornyeService(_make_spotify_settings(tmp_path))

    target = await service.resolve_url(playlist_url)

    assert target.kind == "playlist"
    assert target.url == playlist_url
    assert target.info["original_url"] == playlist_url
    assert [track.external_url for track in target.tracks] == [
        "https://soundcloud.com/kobaryo/first",
        "https://soundcloud.com/kobaryo/second",
    ]


@pytest.mark.asyncio
async def test_soundcloud_playlist_rescue_downloads_each_track(monkeypatch, tmp_path):
    """A playlist the native extension cannot play must be rescued per track
    from that track's own URL, not restarted from the collection link."""
    playlist_url = "https://soundcloud.com/kobaryo/sets/hardcore"
    monkeypatch.setattr(
        "services.mornye._soundcloud_handle_url",
        AsyncMock(return_value=_soundcloud_playlist_payload()),
    )
    failing = SimpleNamespace(
        download_track_async=AsyncMock(
            return_value=SimpleNamespace(
                success=False, skipped=False, file_path=None, error="not playable"
            )
        ),
        close=Mock(),
    )
    monkeypatch.setattr("services.mornye._soundcloud_provider", lambda _raw: failing)
    monkeypatch.setattr(
        "services.ytdlp.probe_url",
        AsyncMock(return_value={"formats": [{"vcodec": "none"}]}),
    )
    monkeypatch.setattr(
        "services.mornye.MornyeService._fetch_cover",
        AsyncMock(return_value=None),
    )

    def fake_download(*, parsed_input, settings, work_dir, info):
        del settings, info
        media = Path(work_dir) / f"{Path(parsed_input.source_url).name}.mp3"
        media.write_bytes(b"mp3")
        return DownloadArtifact(
            path=media, file_name=media.name, send_type="audio", caption=""
        )

    download = AsyncMock(side_effect=fake_download)
    monkeypatch.setattr("services.ytdlp.download_best_quality", download)
    service = MornyeService(_make_spotify_settings(tmp_path))
    target = await service.resolve_url(playlist_url)

    artifacts, _ = await service.download_target(target, tmp_path, audio_format="mp3")

    assert [artifact.file_name for artifact in artifacts] == [
        "first.mp3",
        "second.mp3",
    ]
    assert [
        call.kwargs["parsed_input"].source_url for call in download.await_args_list
    ] == [
        "https://soundcloud.com/kobaryo/first",
        "https://soundcloud.com/kobaryo/second",
    ]
