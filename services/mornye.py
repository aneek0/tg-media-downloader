"""Spotify music downloads via the mornye module (see docs/adr/0002).

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
import logging
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from config import Settings
from utils.models import DownloadArtifact

logger = logging.getLogger(__name__)

_SPOTIFY_HOSTS = {
    "open.spotify.com",
    "play.spotify.com",
    "spotify.link",
}

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


def is_mornye_url(url: str) -> bool:
    """Any URL an installed mornye metadata extension can resolve:
    spotify.com plus every registered catalogue site (deezer.com, …)."""
    hostname = (urlparse(url).hostname or "").lower()
    if hostname in _SPOTIFY_HOSTS or (
        hostname.endswith(".spotify.com") and hostname != "support.spotify.com"
    ):
        return True
    from mornye.core.extension_metadata import parse_catalogue_url

    return parse_catalogue_url(url) is not None


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


def _providers(settings: Settings) -> list[str]:
    return [
        chunk.strip()
        for chunk in settings.mornye_services.split(",")
        if chunk.strip()
    ]


class MornyeService:
    """Long-lived Spotify metadata + download facade for one bot session."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._metadata = None  # SpotifyMetadataClient, created on first use

    @property
    def enabled(self) -> bool:
        return bool(_providers(self._settings))

    async def warm_up(self) -> None:
        """Sync the extension registry (~/.mornye) before first download."""
        from mornye.extensions.manager import ExtensionManager

        await asyncio.to_thread(ExtensionManager, auto_install_downloads=True)

    async def _metadata_client(self):
        if self._metadata is None:
            from mornye.core.spotify_metadata import SpotifyMetadataClient

            self._metadata = SpotifyMetadataClient()
        return self._metadata

    async def close(self) -> None:
        self._metadata = None

    # ------------------------------------------------------------------
    # Metadata
    # ------------------------------------------------------------------

    async def resolve_url(self, url: str) -> SpotifyTarget:
        """Resolve a Spotify or catalogue URL to a target without downloading.

        Track URLs go straight through the module's ``get_track_async`` —
        a dead or malformed id raises an upstream error from upstream
        (NotFound/GenericError detection lives in the module now), so this
        needs no fallback path of its own.
        """
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
        worker = await self._run_worker(target, tracks, work_dir)
        artifacts = await asyncio.to_thread(_artifacts_from_results, worker.results, target)
        return artifacts, await self._fetch_cover(target, work_dir)

    @staticmethod
    async def _fetch_cover(target: SpotifyTarget, work_dir: Path) -> Path | None:
        """Download the album cover into ``work_dir`` for the pre-album
        photo message; None without a cover URL or on any fetch error."""
        if not target.cover_url:
            return None
        try:
            import aiohttp

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
        self, target: SpotifyTarget, tracks: list, work_dir: Path
    ):
        # Compat-module re-exports: real classes live in mornye.application.
        # pylint: disable-next=no-name-in-module
        from mornye.downloader import DownloadWorker
        # pylint: disable-next=no-name-in-module
        from mornye.downloader import _close_shared_browser_sessions
        from mornye.client import DownloadOptions


        opts = DownloadOptions(
            output_dir=str(work_dir),
            services=_providers(self._settings),
            quality=self._settings.mornye_quality,
            transcode_to=self._settings.mornye_transcode or None,
            filename_format="{title} - {artist}",
            create_playlist_subfolders=False,
            enrich_providers=["deezer", "apple"],
            # api.zarz.moe /dl/dzr resolver flakes (ConnectError/HTTP 503 on
            # ~1 in 3 calls) — retries absorb it. See ADR 0002.
            track_max_retries=2,
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
