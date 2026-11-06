"""Music downloads via the mornye module (see docs/adr/0002).

Two halves:
- metadata: ``resolve_url`` → what the user can be shown before downloading.
  Single tracks go through the module's own ``get_track_async``; a dead or
  malformed id raises an upstream error (getTrack answers
  ``NotFound``/``GenericError`` instead of metadata — never "Unknown").
- download: ``download_target`` runs mornye's DownloadWorker directly
  with per-request options (output dir, provider list, quality, transcode).

The feature is enabled only when ``MORNYE_SERVICES`` is non-empty.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

import aiohttp


from config import Settings
from utils.models import DownloadArtifact, ParsedInput

logger = logging.getLogger(__name__)


class SoundCloudUnavailable(RuntimeError):
    """SoundCloud's native extension cannot supply a playable stream."""

_SESSION_MAINTENANCE_SECONDS = 300.0

_SPOTIFY_HOSTS = {
    "open.spotify.com",
    "play.spotify.com",
    "spotify.link",
}

#: Canonical mornye tiers that ask a provider for genuine Hi-Res audio.
_HIRES_QUALITIES = frozenset({"HI_RES", "HI_RES_LOSSLESS"})

#: User-facing quality preference → mornye request. "lossless" is CD-rate
#: FLAC (16/44.1), "hires" asks for the 24-bit master and falls back to
#: LOSSLESS per track when the provider has none.
_QUALITY_BY_PREFERENCE = {"lossless": "LOSSLESS", "hires": "HI_RES_LOSSLESS"}

#: Suffixes that keep cache entries for one URL apart: a Hi-Res master must
#: never answer a CD request, and an MP3 transcode is not the FLAC it came
#: from. They are different files, not different names for one.
_HIRES_CACHE_SUFFIX = "#hires"
_MP3_CACHE_SUFFIX = "#mp3"

# TrackMetadata needs no import at runtime for typing in artifacts, but the
# worker and metadata client are imported lazily so a missing/older mornye
# install degrades to "feature disabled" instead of crashing the whole bot.


@dataclass(slots=True)
class SpotifyTarget:
    kind: str  # "track" | "album" | "playlist" | "artist"
    name: str
    url: str
    tracks: list = field(default_factory=list)
    cover_url: str = ""
    info: dict = field(default_factory=dict)


def _parse_tidal_link(url: str) -> dict[str, str] | None:
    """A Tidal link's ``{"type", "id"}``, or None when this is not one.

    mornye raises for non-Tidal input, so the host check comes first."""
    from mornye.core.errors import MornyeError
    from mornye.core.tidal_metadata import is_tidal_url, parse_tidal_url

    if not is_tidal_url(url):
        return None
    try:
        return parse_tidal_url(url)
    except MornyeError:
        return None

def is_mornye_url(url: str) -> bool:
    """Any URL mornye can resolve to audio: spotify.com, every registered
    catalogue site (deezer.com, …), listen.tidal.com/tidal.com, and SoundCloud."""
    hostname = (urlparse(url).hostname or "").lower()
    if hostname in _SPOTIFY_HOSTS or (
        hostname.endswith(".spotify.com") and hostname != "support.spotify.com"
    ):
        return True
    from mornye.core.extension_metadata import parse_catalogue_url

    return (
        parse_catalogue_url(url) is not None
        or _parse_tidal_link(url) is not None
        or is_soundcloud_url(url)
    )


def is_soundcloud_url(url: str) -> bool:
    """Whether a URL belongs to SoundCloud, including mobile/short links."""
    hostname = (urlparse(url).hostname or "").lower()
    return hostname == "soundcloud.com" or hostname.endswith(".soundcloud.com")


def resolve_quality(audio_format: str, audio_quality: str) -> str:
    """Mornye quality for one request. A Hi-Res master would only be thrown
    away by an MP3 transcode, so MP3 asks for CD lossless instead."""
    if audio_format == "mp3":
        return "LOSSLESS"
    return _QUALITY_BY_PREFERENCE.get(audio_quality, "LOSSLESS")


def audio_cache_key(source_url: str, audio_quality: str, audio_format: str) -> str:
    """Cache key for one requested output. Only catalogue audio has formats
    and tiers, so every other URL keeps one plain key; an MP3 transcode and
    the FLAC master of the same URL must never answer each other, and a
    Hi-Res request is a third file again."""
    if not is_mornye_url(source_url):
        return source_url
    if audio_format == "mp3":
        return f"{source_url}{_MP3_CACHE_SUFFIX}"
    if audio_quality == "hires":
        return f"{source_url}{_HIRES_CACHE_SUFFIX}"
    return source_url


async def _resolve_catalogue_url(url: str) -> SpotifyTarget | None:
    """Resolve a non-Spotify catalogue link (deezer.com, …) through the
    matching extension. ``ExtensionMetadataClient.get_url_async`` answers
    ``(name, tracks, cover)`` — no ``extra`` dict, unlike the Spotify
    metadata client."""
    from mornye.core.extension_metadata import ExtensionMetadataClient
    from mornye.core.extension_metadata import parse_catalogue_url

    client = ExtensionMetadataClient.for_url(url)
    name, tracks, cover = await client.get_url_async(url)
    if not tracks:
        return None
    kind = parse_catalogue_url(url).kind
    return SpotifyTarget(
        kind=kind,
        name=name or "Unknown",
        url=url,
        tracks=tracks,
        cover_url=cover or "",
    )


async def _soundcloud_handle_url(url: str) -> dict:
    """Resolve track/collection metadata with SoundCloud's own extension."""
    from mornye.extensions.provider import JSExtensionProvider

    provider = await asyncio.to_thread(JSExtensionProvider, ext_id="soundcloud")
    try:
        return await asyncio.to_thread(provider.handle_url, url)
    finally:
        provider.close()


def _soundcloud_track(raw: dict, cover_url: str = "", album: str = ""):
    """Keep SoundCloud's native numeric ID for its direct availability API."""
    from mornye.core.models import TrackMetadata

    artists = str(raw.get("artists") or raw.get("artist") or "Unknown Artist")
    return TrackMetadata(
        id=str(raw.get("id") or "").rsplit(":", maxsplit=1)[-1],
        title=str(raw.get("name") or raw.get("title") or "Unknown"),
        artists=artists,
        album=album or str(raw.get("album_name") or ""),
        album_artist=str(raw.get("album_artist") or artists),
        track_number=int(raw.get("track_number") or 0),
        duration_ms=int(raw.get("duration_ms") or 0),
        release_date=str(raw.get("release_date") or ""),
        cover_url=str(raw.get("cover_url") or raw.get("images") or cover_url),
        external_url=str(raw.get("external_urls") or raw.get("external_url") or ""),
        genre=str(raw.get("genre") or ""),
        composer=str(raw.get("composer") or ""),
    )


def _soundcloud_provider(raw_track: dict):
    """Build a provider that checks the exact native ID, not a text match."""
    from mornye.extensions.provider import JSExtensionProvider

    class DirectSoundCloudProvider(JSExtensionProvider):
        async def _provider_match_options(self, metadata):
            return {
                "spotify_id": str(raw_track.get("id") or "").rsplit(":", maxsplit=1)[-1],
                "duration_ms": metadata.duration_ms,
                "track": raw_track,
            }

    return DirectSoundCloudProvider(ext_id="soundcloud")


def _providers(settings: Settings) -> list[str]:
    return [
        chunk.strip()
        for chunk in settings.mornye_services.split(",")
        if chunk.strip()
    ]


class MornyeService:
    """Long-lived music metadata + download facade for one bot session."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._metadata = None  # SpotifyMetadataClient, created on first use
        self._tidal = None  # TidalMetadataClient, created on first Tidal link

    @property
    def enabled(self) -> bool:
        return bool(_providers(self._settings))

    async def warm_up(self) -> None:
        """Sync extensions and authenticate enabled providers before polling."""
        from mornye.extensions.manager import ExtensionManager

        manager = await asyncio.to_thread(ExtensionManager, auto_install_downloads=True)
        await self.renew_signed_sessions_once(manager, log_ready=True)

    @staticmethod
    async def _maintain_signed_client(extension: str, client, *, log_ready=False) -> None:
        from mornye.core.signed_session_mobile import auth_backoff_remaining

        if client.authenticated:
            try:
                await client.ensure_session()
            except RuntimeError as exc:
                logger.info(
                    "Signed session replacement required | provider=%s error=%s",
                    extension,
                    exc,
                )
        if not client.authenticated:
            remaining = auth_backoff_remaining(client)
            if remaining > 0:
                logger.info(
                    "Signed session warm-up paused | provider=%s remaining=%.0fs",
                    extension,
                    remaining,
                )
                return
            logger.info("Signed session warm-up starting | provider=%s", extension)
            await client.authenticate_with_turnstile(timeout=90)
        if not client.authenticated:
            raise RuntimeError("authentication finished without a usable session")
        log = logger.info if log_ready else logger.debug
        log(
            "Signed session ready | provider=%s expires=%s",
            extension,
            client.expires_at,
        )

    async def renew_signed_sessions_once(self, manager=None, *, log_ready=False) -> None:
        """Refresh each enabled provider using fresh clients and current disk state."""
        from mornye.extensions import extension_id
        from mornye.extensions.manager import ExtensionManager
        from mornye.extensions.runtime_features import signed_session_client

        if manager is None:
            manager = await asyncio.to_thread(ExtensionManager)
        seen = set()
        for service in _providers(self._settings):
            extension = service
            client = None
            try:
                extension = extension_id(service, manager)
                if extension in seen:
                    continue
                seen.add(extension)
                installed = manager.get_installed(extension)
                if installed is None:
                    logger.warning("Signed session extension missing | provider=%s", extension)
                    continue
                client = signed_session_client(installed.manifest)
                if client is not None:
                    await self._maintain_signed_client(extension, client, log_ready=log_ready)
            except Exception as exc:  # pylint: disable=broad-exception-caught
                logger.warning(
                    "Signed session maintenance failed | provider=%s error=%r retry=300s",
                    extension,
                    exc,
                )
            finally:
                if client is not None:
                    with contextlib.suppress(Exception):
                        await client.aclose()

    async def run_signed_session_maintenance(self) -> None:
        """Re-read manifests and renew sessions without waiting for a user request."""
        while True:
            await asyncio.sleep(_SESSION_MAINTENANCE_SECONDS)
            try:
                await self.renew_signed_sessions_once()
            except Exception:  # pylint: disable=broad-exception-caught
                logger.exception("Signed session maintenance pass failed | retry=300s")

    async def _metadata_client(self):
        if self._metadata is None:
            from mornye.core.spotify_metadata import SpotifyMetadataClient

            self._metadata = SpotifyMetadataClient()
        return self._metadata

    async def close(self) -> None:
        self._metadata = None
        self._tidal = None

    # ------------------------------------------------------------------
    # Metadata
    # ------------------------------------------------------------------


    @staticmethod
    async def _resolve_soundcloud(url: str) -> SpotifyTarget:
        payload = await _soundcloud_handle_url(url)
        if not payload.get("success"):
            raise RuntimeError(
                "SoundCloud could not resolve this link: "
                f"{payload.get('error') or 'unknown error'}"
            )
        kind = str(payload.get("type") or "track")
        artist = payload.get("artist") or {}
        if kind == "track":
            raw_tracks = [payload.get("track") or {}]
            name = str(raw_tracks[0].get("name") or "SoundCloud track")
            cover = str(raw_tracks[0].get("cover_url") or "")
            album = ""
        elif kind == "artist":
            raw_tracks = list(artist.get("top_tracks") or [])
            name = str(artist.get("name") or "SoundCloud artist")
            cover = str(artist.get("cover_url") or artist.get("image_url") or "")
            album = ""
        elif kind in {"album", "playlist"}:
            raw_tracks = list(payload.get("tracks") or [])
            name = str(payload.get("name") or f"SoundCloud {kind}")
            cover = str(payload.get("cover_url") or "")
            album = name if kind == "album" else ""
        else:
            raise RuntimeError(f"Unsupported SoundCloud resource type: {kind}")
        raw_tracks = [raw for raw in raw_tracks if raw.get("id")]
        tracks = [
            _soundcloud_track(raw, cover_url=cover, album=album)
            for raw in raw_tracks
        ]
        if not tracks:
            raise RuntimeError("SoundCloud returned no downloadable tracks")
        # A collection keeps its own link: a track URL in its place loses
        # which album/playlist the download belongs to, so every retry (and
        # every per-track rescue) would start over from the first track.
        canonical_url = url
        if kind == "track":
            canonical_url = tracks[0].external_url or url
        return SpotifyTarget(
            kind=kind,
            name=name,
            url=canonical_url,
            tracks=tracks,
            cover_url=cover or tracks[0].cover_url,
            info={
                "provider": "soundcloud",
                "raw_tracks": raw_tracks,
                "original_url": url,
            },
        )

    async def _resolve_soundcloud_ytdlp(self, url: str) -> SpotifyTarget:
        """Build a normal music target when the extension itself is unavailable."""
        from services.ytdlp import probe_url

        parsed = ParsedInput(source_url=url)
        info = await probe_url(parsed, self._settings)
        title = str(info.get("track") or info.get("title") or "SoundCloud track")
        artists = str(info.get("artist") or info.get("uploader") or "Unknown Artist")
        raw = {
            "id": str(info.get("id") or "soundcloud"),
            "name": title,
            "artists": artists,
            "duration_ms": int(float(info.get("duration") or 0) * 1000),
            "cover_url": str(info.get("thumbnail") or ""),
            "external_urls": str(info.get("webpage_url") or url),
        }
        track = _soundcloud_track(raw)
        return SpotifyTarget(
            kind="track",
            name=f"{artists} — {title}",
            url=track.external_url or url,
            tracks=[track],
            cover_url=track.cover_url,
            info={
                "provider": "soundcloud_ytdlp",
                "original_url": url,
                "ytdlp_info": info,
            },
        )

    async def resolve_url(self, url: str) -> SpotifyTarget:
        """Resolve a Spotify, catalogue, Tidal, or SoundCloud URL without downloading."""
        tidal = _parse_tidal_link(url)
        if is_soundcloud_url(url):
            try:
                return await self._resolve_soundcloud(url)
            except Exception as exc:  # pylint: disable=broad-exception-caught
                logger.warning(
                    "SoundCloud metadata unavailable, using yt-dlp fallback | error=%s",
                    exc,
                )
                return await self._resolve_soundcloud_ytdlp(url)
        if tidal:
            return await self._resolve_tidal(url, tidal)
        # Non-Spotify catalogue link (deezer.com, …): route through the
        # matching installed extension's metadata client.
        catalogue = await asyncio.to_thread(self._catalogue_link, url)
        if catalogue is not None:
            target = await _resolve_catalogue_url(url)
            if target is None or not target.tracks:
                raise RuntimeError("MORNYE_TRACK_UNRESOLVED")
            return target

        from mornye.core.spotify_metadata import parse_spotify_url

        info = await asyncio.to_thread(parse_spotify_url, url)
        kind = info["type"]
        if kind == "track":
            client = await self._metadata_client()
            track = await client.get_track_async(info["id"])
            artist = track.artist_names[0] if track.artist_names else "Unknown Artist"
            return SpotifyTarget(
                kind="track",
                name=f"{artist} — {track.title}",
                url=url,
                tracks=[track],
                cover_url=track.cover_url or "",
                info={"album": track.album, "album_id": track.album_id},
            )
        target = await self._resolve_collection(url, kind)
        if target is None or not target.tracks:
            raise RuntimeError("MORNYE_TRACK_UNRESOLVED")
        return target

    @staticmethod
    def _catalogue_link(url: str):
        from mornye.core.extension_metadata import parse_catalogue_url

        return parse_catalogue_url(url)

    async def _resolve_tidal(self, url: str, info: dict[str, str]) -> SpotifyTarget:
        """Resolve a Tidal link through mornye's public Tidal metadata API."""
        kind = info["type"]
        if kind not in {"track", "album", "playlist"}:
            raise RuntimeError(
                "Tidal artist and mix links are not supported here; "
                "send a track, album or playlist link."
            )
        if self._tidal is None:
            from mornye.core.tidal_metadata import TidalMetadataClient

            self._tidal = TidalMetadataClient()
        name, tracks, cover, extra = await self._tidal.get_url(url)
        return SpotifyTarget(
            kind=kind, name=name, url=url, tracks=tracks, cover_url=cover, info=extra
        )

    async def _resolve_collection(self, url: str, kind: str) -> SpotifyTarget | None:
        client = await self._metadata_client()
        name, tracks, cover, extra = await client.get_url_async(url)
        effective = "artist" if kind.startswith("artist") else kind
        return SpotifyTarget(
            kind=effective,
            name=name or "Unknown",
            url=url,
            tracks=tracks,
            cover_url=cover or "",
            info=extra or {},
        )

    async def download_target(
        self,
        target: SpotifyTarget,
        work_dir: Path,
        *,
        audio_format: str | None = None,
        audio_quality: str | None = None,
    ) -> tuple[list[DownloadArtifact], Path | None]:
        """Download every track of ``target`` into ``work_dir``.

        Returns artifacts in track order plus the album cover image
        downloaded to ``work_dir`` (None when the target has no cover or
        the fetch failed). Tracks that fail on every provider are
        skipped (failed-tolerance); when nothing succeeds, RuntimeError
        tells the caller to report it to the user.
        """
        tracks = list(target.tracks)
        cap = self._settings.spotify_collection_cap
        if len(tracks) > cap:
            logger.warning("Track cap applied | requested=%s cap=%s", len(tracks), cap)
            tracks = tracks[:cap]
        if audio_format is None:
            audio_format = (
                "mp3" if self._settings.mornye_transcode == "mp3" else "flac"
            )
        if target.info.get("provider") in {"soundcloud", "soundcloud_ytdlp"}:
            return await self._download_soundcloud_target(
                target, tracks, work_dir, audio_format=audio_format
            )
        worker = await self._run_worker(
            target,
            tracks,
            work_dir,
            transcode="mp3" if audio_format == "mp3" else "",
            quality=resolve_quality(audio_format, audio_quality or "lossless"),
        )
        artifacts = await asyncio.to_thread(_artifacts_from_results, worker.results, target)
        return artifacts, await self._fetch_cover(target, work_dir)

    async def _download_soundcloud_target(
        self,
        target: SpotifyTarget,
        tracks: list,
        work_dir: Path,
        *,
        audio_format: str,
    ) -> tuple[list[DownloadArtifact], Path | None]:
        """Prefer SoundCloud's native AAC; retain yt-dlp for blocked streams."""
        if target.info.get("provider") == "soundcloud_ytdlp":
            return await self._download_soundcloud_ytdlp(target, work_dir, tracks)
        try:
            return await self._download_soundcloud_native(
                target, tracks, work_dir, audio_format=audio_format
            )
        except SoundCloudUnavailable as exc:
            logger.warning(
                "SoundCloud extension unavailable, using yt-dlp fallback | error=%s",
                exc,
            )
            shutil.rmtree(work_dir / "soundcloud-native", ignore_errors=True)
            return await self._download_soundcloud_ytdlp(target, work_dir, tracks)

    @staticmethod
    async def _download_soundcloud_native(
        target: SpotifyTarget,
        tracks: list,
        work_dir: Path,
        *,
        audio_format: str,
    ) -> tuple[list[DownloadArtifact], Path | None]:
        from mornye.application.download_engine import DownloadOptions
        from mornye.application.post_processing import apply_post_processing

        native_dir = work_dir / "soundcloud-native"
        native_dir.mkdir(parents=True, exist_ok=True)
        opts = DownloadOptions(
            output_dir=str(native_dir),
            services=["ext:soundcloud"],
            quality="aac_160",
            transcode_to="mp3" if audio_format == "mp3" else None,
            filename_format="{title} - {artist}",
            create_playlist_subfolders=False,
            enrich_providers=[],
        )
        semaphore = asyncio.Semaphore(2)

        async def download_one(position: int, raw: dict, track):
            async with semaphore:
                provider = _soundcloud_provider(raw)
                try:
                    result = await provider.download_track_async(
                        track,
                        str(native_dir),
                        filename_format=opts.filename_format,
                        position=position,
                        is_album=target.kind == "album",
                        quality="aac_160",
                        transcode_to=opts.transcode_to,
                        enrich_metadata=False,
                        enrich_providers=[],
                    )
                    if not result.success:
                        raise SoundCloudUnavailable(
                            result.error or "native SoundCloud download failed"
                        )
                    result = await apply_post_processing(result, track, opts)
                    if not result.success:
                        raise SoundCloudUnavailable(
                            result.error or "SoundCloud post-processing failed"
                        )
                    return track.id, result
                finally:
                    provider.close()

        try:
            raw_tracks = list(target.info.get("raw_tracks") or [])[: len(tracks)]
            outcomes = await asyncio.gather(
                *(
                    download_one(position, raw, track)
                    for position, (raw, track) in enumerate(
                        zip(raw_tracks, tracks, strict=True), start=1
                    )
                ),
                return_exceptions=True,
            )
            failure = next(
                (outcome for outcome in outcomes if isinstance(outcome, Exception)),
                None,
            )
            if failure is not None:
                raise failure
            results = dict(outcomes)
            artifacts = await asyncio.to_thread(
                _artifacts_from_results, results, target
            )
        except Exception as exc:
            if isinstance(exc, SoundCloudUnavailable):
                raise
            raise SoundCloudUnavailable(str(exc)) from exc
        return artifacts, await MornyeService._fetch_cover(target, work_dir)

    async def _download_soundcloud_ytdlp(
        self, target: SpotifyTarget, work_dir: Path, tracks: list
    ) -> tuple[list[DownloadArtifact], Path | None]:
        """Last resort for SoundCloud streams the extension calls unplayable.

        A collection link only yields its first item through yt-dlp, so each
        track is probed and fetched by its own URL: one blocked stream must
        neither restart the playlist nor land under another track's name."""
        from services.ytdlp import download_best_quality, probe_url

        fallback_url = str(target.info.get("original_url") or target.url)
        if len(tracks) == 1:
            parsed = ParsedInput(source_url=fallback_url)
            info = target.info.get("ytdlp_info") or await probe_url(
                parsed, self._settings
            )
            artifact = await download_best_quality(
                parsed_input=parsed,
                settings=self._settings,
                work_dir=work_dir,
                info=info,
            )
            return [artifact], await self._fetch_cover(target, work_dir)
        artifacts: list[DownloadArtifact] = []
        for track in tracks:
            # The collection link would fetch its first track again, so a
            # track without its own URL is skipped rather than mislabelled.
            track_url = str(track.external_url or "")
            if not track_url:
                logger.warning(
                    "SoundCloud track without its own URL | id=%s", track.id
                )
                continue
            parsed = ParsedInput(source_url=track_url)
            try:
                info = await probe_url(parsed, self._settings)
                artifacts.append(
                    await download_best_quality(
                        parsed_input=parsed,
                        settings=self._settings,
                        work_dir=work_dir,
                        info=info,
                    )
                )
            except Exception as exc:  # pylint: disable=broad-exception-caught
                logger.warning(
                    "SoundCloud track rescue failed | source=%s error=%s",
                    track_url,
                    exc,
                )
        if not artifacts:
            raise RuntimeError("MORNYE_NO_RESULTS")
        return artifacts, await self._fetch_cover(target, work_dir)

    @staticmethod
    async def _fetch_cover(target: SpotifyTarget, work_dir: Path) -> Path | None:
        """Download the album cover into ``work_dir`` for the pre-album
        photo message; None without a cover URL or on any fetch error."""
        if not target.cover_url:
            return None
        try:
            dest = work_dir / "cover.jpg"
            timeout = aiohttp.ClientTimeout(total=15)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(target.cover_url) as resp:
                    resp.raise_for_status()
                    data = await resp.read()
            if not data:
                return None
            dest.write_bytes(data)
            return dest
        except Exception:  # pylint: disable=broad-exception-caught
            logger.debug("Cover fetch failed | url=%s", target.cover_url)
            return None

    async def _run_worker(
        self,
        target: SpotifyTarget,
        tracks: list,
        work_dir: Path,
        *,
        transcode: str | None = None,
        quality: str | None = None,
    ):
        _install_aiohttp_cover_fetch()
        from mornye.application.download_engine import (
            DownloadOptions,
            DownloadWorker,
            _close_shared_browser_sessions,
        )

        output_format = transcode
        if output_format is None:
            output_format = self._settings.mornye_transcode
        quality_is_hires = (quality or "").upper() in _HIRES_QUALITIES

        opts = DownloadOptions(
            output_dir=str(work_dir),
            services=_providers(self._settings),
            quality=quality or self._settings.mornye_quality,
            transcode_to=output_format or None,
            filename_format="{title} - {artist}",
            create_playlist_subfolders=False,
            enrich_providers=["deezer", "apple"],
            # api.zarz.moe /dl/dzr resolver flakes (ConnectError/HTTP 503 on
            # ~1 in 3 calls) — retries absorb it. See ADR 0002.
            track_max_retries=2,
            # A Hi-Res request answered with upsampled CD audio is a bigger
            # file with no more music in it; re-fetch it at honest LOSSLESS.
            verify_hires=quality_is_hires,
            redownload_fake_hires=quality_is_hires,
        )
        worker = DownloadWorker(
            tracks=tracks,
            opts=opts,
            collection_name=target.name,
            is_album=target.kind in {"album", "artist"},
            is_playlist=target.kind in {"playlist", "artist"},
        )
        try:
            await worker.run_async()
        finally:
            await _close_shared_browser_sessions()
        return worker


async def _fetch_cover_aiohttp(url: str) -> bytes | None:
    """Fetch embedded artwork without mornye's intermittently timing-out httpx path."""
    if not url:
        return None
    timeout = aiohttp.ClientTimeout(total=15)
    async with aiohttp.ClientSession(trust_env=True, timeout=timeout) as client:
        for attempt in range(3):
            try:
                async with client.get(url) as response:
                    response.raise_for_status()
                    return await response.read()
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
                logger.warning(
                    "Cover fetch failed | attempt=%s error=%r", attempt + 1, exc
                )
                if attempt < 2:
                    await asyncio.sleep(min(3.0, (attempt + 1) * 1.5))
    return None


def _install_aiohttp_cover_fetch() -> None:
    """Patch mornye's private cover hook until it offers a transport option."""
    from mornye.core import tagger

    original = tagger._fetch_cover_async
    if getattr(original, "_tgmd_aiohttp", False):
        return

    async def fetch_cover(url: str, session=None) -> bytes | None:
        if session is not None:
            return await original(url, session)
        return await _fetch_cover_aiohttp(url)

    fetch_cover._tgmd_aiohttp = True  # type: ignore[attr-defined]
    tagger._fetch_cover_async = fetch_cover  # pylint: disable=protected-access


def _artifacts_from_results(
    results: dict, target: SpotifyTarget
) -> list[DownloadArtifact]:
    """Turn ``{track_id: DownloadResult}`` into artifacts in track order."""
    artifacts: list[DownloadArtifact] = []
    for track in target.tracks:
        result = results.get(track.id)
        if result is None or not result.success:
            continue
        path = Path(result.file_path)
        if not path.exists():
            logger.warning("mornye missing file on disk | track=%s", track.id)
            continue
        artist_names = track.artist_names or (
            [track.first_artist] if getattr(track, "first_artist", "") else []
        )
        artists = ", ".join(artist_names) if artist_names else "Unknown Artist"
        duration = None
        if track.duration_ms:
            duration = max(1, int(track.duration_ms / 1000))
        artifacts.append(
            DownloadArtifact(
                path=path,
                file_name=path.name,
                send_type="audio",
                caption=f"{artists} — {track.title}",
                duration=duration,
                audio_title=track.title,
                audio_performer=artists if artists != "Unknown Artist" else None,
            )
        )
    if not artifacts:
        raise RuntimeError("MORNYE_NO_RESULTS")
    return artifacts
