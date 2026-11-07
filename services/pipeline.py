from __future__ import annotations

import logging
from dataclasses import dataclass

from config import Settings
from services.direct_downloads import download_direct_file
from services.gallery import (
    GalleryProbe,
    download_gallery_media,
    gallery_url_items,
    probe_gallery,
)
from services.parsing import is_probable_youtube_url
from services.requests import (
    direct_request,
    gallery_request,
    music_request,
    probe_request,
)
from services.session import Session
from services.sources import (
    SourceKind,
    SourceUrl,
    build_direct_options,
    classify,
)
from services.status import StatusSurface
from services.ytdlp import (
    build_quick_youtube_options,
    build_ytdlp_options,
    download_best_quality,
    download_quick_youtube,
    download_selected_format,
    probe_url,
)
from utils.logging_config import safe_url_label
from utils.models import (
    DirectPayload,
    DownloadArtifact,
    DownloadOption,
    ParsedInput,
    ProbePayload,
    RequestType,
    StoredRequest,
)

logger = logging.getLogger(__name__)

GALLERY_OPTIONS = [
    DownloadOption(option_id="gallery_all", label="Media", send_type="photo")
]
AUTO_BEST_OPTIONS = [
    DownloadOption(option_id="auto_best", label="Best quality", send_type="video")
]
MUSIC_OPTIONS = [
    DownloadOption(option_id="spotify_flac", label="FLAC", send_type="audio")
]

_IMMEDIATE_TYPES = frozenset(
    {
        RequestType.MORNYE,
        RequestType.GALLERY_MEDIA,
    }
)


@dataclass(frozen=True)
class PreparedRequest:
    """Outcome of the intake ladder: which downloader will run, or the
    gallery error that makes the URL undownloadable."""

    stored: StoredRequest
    source: SourceUrl
    gallery: GalleryProbe

    @property
    def immediate(self) -> bool:
        """True when intake should download now instead of showing a
        format keyboard."""
        return self.stored.request_type in _IMMEDIATE_TYPES


async def _probe_ytdlp(
    parsed: ParsedInput, settings: Settings
) -> dict | None:
    try:
        return await probe_url(parsed, settings)
    except RuntimeError as exc:  # pragma: no cover - network/tool error path
        logger.warning(
            "yt-dlp probe failed | source=%s error=%s",
            safe_url_label(parsed.source_url),
            exc,
        )
        return None


async def prepare_request(session: Session, parsed: ParsedInput) -> PreparedRequest:
    """Run the source ladder once: music, gallery, YouTube, yt-dlp, direct.

    Constructs and persists the matching request; raises only when the music
    service cannot resolve a track."""
    store = session.request_store
    settings = session.settings
    source = classify(parsed)
    if session.mornye.enabled and source.kind is SourceKind.MUSIC:
        target = await session.mornye.resolve_url(parsed.source_url)
        stored = music_request(
            store,
            parsed,
            kind=target.kind,
            name=target.name,
            count=len(target.tracks),
            options=MUSIC_OPTIONS,
        )
        return PreparedRequest(stored, source, GalleryProbe([], None, None))

    gallery = await probe_gallery(parsed, settings)
    if gallery.file_dicts:
        stored = gallery_request(
            store,
            parsed,
            GALLERY_OPTIONS,
            gallery_url_items(gallery.file_dicts, gallery.content, parsed),
        )
        return PreparedRequest(stored, source, gallery)

    if settings.auto_best_quality:
        info = await _probe_ytdlp(parsed, settings)
        if info:
            stored = probe_request(
                store,
                RequestType.YTDLP_AUTO,
                parsed,
                AUTO_BEST_OPTIONS,
                info,
            )
        else:
            stored = direct_request(
                store, parsed, build_direct_options(source)
            )
        return PreparedRequest(stored, source, gallery)

    if is_probable_youtube_url(parsed.source_url):
        stored = probe_request(
            store,
            RequestType.YOUTUBE_QUICK,
            parsed,
            build_quick_youtube_options(),
            {},
        )
        return PreparedRequest(stored, source, gallery)

    info = await _probe_ytdlp(parsed, settings)
    if info:
        options = build_ytdlp_options(info)
        if options:
            stored = probe_request(
                store, RequestType.YTDLP_SELECTION, parsed, options, info
            )
            return PreparedRequest(stored, source, gallery)
        stored = direct_request(
            store, parsed, build_direct_options(source, info=info), info
        )
        return PreparedRequest(stored, source, gallery)

    stored = direct_request(store, parsed, build_direct_options(source))
    return PreparedRequest(stored, source, gallery)


async def download_artifacts(
    session: Session,
    stored: StoredRequest,
    option: DownloadOption,
    *,
    status: StatusSurface,
) -> list[DownloadArtifact]:
    """Download one prepared request; the caller owns delivery."""
    settings = session.settings
    work_dir = session.request_store.work_directory(stored.token)
    request_type = stored.request_type
    payload = stored.payload
    if request_type == RequestType.GALLERY_MEDIA:
        return await download_gallery_media(
            parsed_input=stored.parsed_input,
            settings=settings,
            work_dir=work_dir,
            status=status,
            download_limiter=session.gallery_download_limiter,
        )
    if request_type == RequestType.DIRECT_DOWNLOAD:
        artifact = await download_direct_file(
            status=status,
            parsed_input=stored.parsed_input,
            option=option,
            settings=settings,
            work_dir=work_dir,
            suggested_ext=(
                payload.ext if isinstance(payload, DirectPayload) else None
            ),
        )
        return [artifact]
    if request_type == RequestType.YOUTUBE_QUICK:
        artifact = await download_quick_youtube(
            parsed_input=stored.parsed_input,
            option=option,
            settings=settings,
            work_dir=work_dir,
            status=status,
        )
        return [artifact]
    info = payload.info if isinstance(payload, ProbePayload) else {}
    if request_type == RequestType.YTDLP_AUTO:
        artifact = await download_best_quality(
            parsed_input=stored.parsed_input,
            settings=settings,
            work_dir=work_dir,
            info=info,
            status=status,
        )
        return [artifact]
    if request_type == RequestType.YTDLP_SELECTION:
        artifact = await download_selected_format(
            parsed_input=stored.parsed_input,
            option=option,
            info=info,
            settings=settings,
            work_dir=work_dir,
            status=status,
        )
        return [artifact]
    raise RuntimeError(
        f"request type {request_type!r} has no downloader"
    )


async def download_inline(
    session: Session,
    stored: StoredRequest,
    *,
    status: StatusSurface,
    audio_format: str | None = None,
    audio_quality: str | None = None,
) -> list[DownloadArtifact]:
    """Inline path: run the same dispatch table on one stored URL request.
    No PreparedRequest is needed here; the StoredRequest was saved by the
    inline handler and already carries its ParsedInput and kind."""
    settings = session.settings
    work_dir = session.request_store.work_directory(stored.token)
    parsed = stored.parsed_input
    gallery = await probe_gallery(parsed, settings)
    if gallery.file_dicts:
        return await download_gallery_media(
            parsed_input=parsed,
            settings=settings,
            work_dir=work_dir,
            file_dicts=gallery.file_dicts,
            content=gallery.content,
            status=status,
            download_limiter=session.gallery_download_limiter,
        )

    source = classify(parsed)
    if source.kind is SourceKind.MUSIC:
        target = await session.mornye.resolve_url(parsed.source_url)
        artifacts, _ = await session.mornye.download_target(
            target,
            work_dir,
            audio_format=audio_format,
            audio_quality=audio_quality,
        )
        return artifacts

    info = await _probe_ytdlp(parsed, settings)
    if info is not None:
        artifact = await download_best_quality(
            parsed_input=parsed,
            settings=settings,
            work_dir=work_dir,
            info=info,
            status=status,
        )
        return [artifact]

    option = build_direct_options(source)[0]
    artifact = await download_direct_file(
        status=status,
        parsed_input=parsed,
        option=option,
        settings=settings,
        work_dir=work_dir,
    )
    return [artifact]