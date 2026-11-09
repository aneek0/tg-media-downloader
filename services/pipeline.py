from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from config import Settings
from services.direct_downloads import download_direct_file
from services.gallery import (
    GalleryProbe,
    download_gallery_media,
    gallery_url_items,
    probe_gallery,
)
from services.parsing import is_probable_youtube_url
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
from utils import text
from utils.logging_config import safe_url_label
from utils.models import (
    DirectPayload,
    DownloadArtifact,
    DownloadOption,
    GalleryPayload,
    InlineGalleryPayload,
    InlineMediaPayload,
    MusicPayload,
    ParsedInput,
    ProbePayload,
    RequestPayload,
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
        stored = store.open(
            RequestType.MORNYE,
            parsed,
            payload=MusicPayload(
                kind=target.kind,
                name=target.name,
                count=len(target.tracks),
            ),
            options=MUSIC_OPTIONS,
        )
        return PreparedRequest(stored, source, GalleryProbe([], None, None))

    gallery = await probe_gallery(parsed, settings)
    if gallery.file_dicts or (source.is_twitter and gallery.content):
        stored = store.open(
            RequestType.GALLERY_MEDIA,
            parsed,
            payload=GalleryPayload(
                url_media=gallery_url_items(
                    gallery.file_dicts, gallery.content, parsed
                ),
                content=gallery.content if not gallery.file_dicts else None,
                file_dicts=gallery.file_dicts,
            ),
            options=GALLERY_OPTIONS,
        )
        return PreparedRequest(stored, source, gallery)

    if settings.auto_best_quality:
        info = await _probe_ytdlp(parsed, settings)
        if info:
            stored = store.open(
                RequestType.YTDLP_AUTO,
                parsed,
                payload=ProbePayload(info=info),
                options=AUTO_BEST_OPTIONS,
            )
        else:
            stored = store.open(
                RequestType.DIRECT_DOWNLOAD,
                parsed,
                payload=DirectPayload(),
                options=build_direct_options(source),
            )
        return PreparedRequest(stored, source, gallery)

    if is_probable_youtube_url(parsed.source_url):
        stored = store.open(
            RequestType.YOUTUBE_QUICK,
            parsed,
            payload=ProbePayload(),
            options=build_quick_youtube_options(),
        )
        return PreparedRequest(stored, source, gallery)

    info = await _probe_ytdlp(parsed, settings)
    if info:
        options = build_ytdlp_options(info)
        if options:
            stored = store.open(
                RequestType.YTDLP_SELECTION,
                parsed,
                payload=ProbePayload(info=info),
                options=options,
            )
            return PreparedRequest(stored, source, gallery)
        stored = store.open(
            RequestType.DIRECT_DOWNLOAD,
            parsed,
            payload=DirectPayload(info=info),
            options=build_direct_options(source, info=info),
        )
        return PreparedRequest(stored, source, gallery)

    stored = store.open(
        RequestType.DIRECT_DOWNLOAD,
        parsed,
        payload=DirectPayload(),
        options=build_direct_options(source),
    )
    return PreparedRequest(stored, source, gallery)


@dataclass(frozen=True, slots=True)
class DownloadChoice:
    """What to download with: the keyboard Option plus the user's audio tier.

    Both surfaces build one of these, so a downloader has one contract instead
    of receiving a ``DownloadOption`` on the DM path and loose audio strings on
    the inline one.
    """

    option: DownloadOption
    audio_format: str | None = None
    audio_quality: str | None = None


@dataclass(frozen=True, slots=True)
class Downloaded:
    """Downloader output plus what delivery still needs from the resolution."""

    artifacts: list[DownloadArtifact]
    music_target: Any = None
    cover_path: Path | None = None


async def _download_music(
    session: Session,
    stored: StoredRequest,
    choice: DownloadChoice,
    *,
    status: StatusSurface,
) -> Downloaded:
    """Resolve and download a Music Request; the cover photo and collection
    caption are delivery's job, so the target travels back with the artifacts."""
    target = await session.mornye.resolve_url(stored.parsed_input.source_url)
    await status.edit(text.MUSIC_RESOLVING)
    await status.edit(
        text.MUSIC_DOWNLOAD.format(
            name=text.esc(target.name),
            kind=target.kind if target.kind == "track" else "collection",
            count=len(target.tracks),
        )
    )
    artifacts, cover_path = await session.mornye.download_target(
        target,
        session.request_store.work_directory(stored.token),
        audio_format=choice.audio_format,
        audio_quality=choice.audio_quality,
    )
    return Downloaded(artifacts, music_target=target, cover_path=cover_path)


def _probed_media(payload: RequestPayload) -> list[dict[str, Any]] | None:
    """The probe result a gallery-shaped payload carries, if any."""
    if isinstance(
        payload, GalleryPayload | InlineGalleryPayload | InlineMediaPayload
    ):
        return payload.file_dicts or None
    return None


async def _download_gallery(
    session: Session,
    stored: StoredRequest,
    *,
    file_dicts: list[dict[str, Any]] | None,
    status: StatusSurface,
) -> Downloaded:
    artifacts = await download_gallery_media(
        parsed_input=stored.parsed_input,
        settings=session.settings,
        work_dir=session.request_store.work_directory(stored.token),
        status=status,
        download_limiter=session.gallery_download_limiter,
        file_dicts=file_dicts,
    )
    return Downloaded(artifacts)


async def _download_from_probe(
    session: Session, stored: StoredRequest, *, status: StatusSurface
) -> Downloaded:
    """Best quality when the probe recognises the source, plain direct download
    when it does not. Used by an inline Request that could not commit earlier."""
    settings = session.settings
    work_dir = session.request_store.work_directory(stored.token)
    info = await _probe_ytdlp(stored.parsed_input, settings)
    if info is not None:
        return Downloaded(
            [
                await download_best_quality(
                    parsed_input=stored.parsed_input,
                    settings=settings,
                    work_dir=work_dir,
                    info=info,
                    status=status,
                )
            ]
        )
    return Downloaded(
        [
            await download_direct_file(
                status=status,
                parsed_input=stored.parsed_input,
                option=build_direct_options(classify(stored.parsed_input))[0],
                settings=settings,
                work_dir=work_dir,
            )
        ]
    )


async def download_artifacts(
    session: Session,
    stored: StoredRequest,
    choice: DownloadChoice,
    *,
    status: StatusSurface,
) -> Downloaded:
    """Download one prepared Request.

    The table is the only place that decides which downloader runs, for every
    surface: DM, inline, catalogue. Each row returns artifacts plus whatever
    delivery still needs, so no caller has to know the order of the rows.
    """
    settings = session.settings
    work_dir = session.request_store.work_directory(stored.token)
    request_type = stored.request_type
    payload = stored.payload
    option = choice.option
    if request_type is RequestType.MORNYE:
        return await _download_music(session, stored, choice, status=status)
    if request_type in {RequestType.GALLERY_MEDIA, RequestType.INLINE_GALLERY}:
        return await _download_gallery(
            session,
            stored,
            file_dicts=_probed_media(payload),
            status=status,
        )
    if request_type is RequestType.INLINE_MEDIA:
        plan = payload.plan if isinstance(payload, InlineMediaPayload) else ""
        if plan == "music":
            return await _download_music(session, stored, choice, status=status)
        if plan == "gallery":
            return await _download_gallery(
                session,
                stored,
                file_dicts=_probed_media(payload),
                status=status,
            )
        # No plan persisted (a stale token or a plain URL): probe once here.
        return await _download_from_probe(session, stored, status=status)
    if request_type is RequestType.DIRECT_DOWNLOAD:
        return Downloaded(
            [
                await download_direct_file(
                    status=status,
                    parsed_input=stored.parsed_input,
                    option=option,
                    settings=settings,
                    work_dir=work_dir,
                    suggested_ext=(
                        payload.ext if isinstance(payload, DirectPayload) else None
                    ),
                )
            ]
        )
    if request_type is RequestType.YOUTUBE_QUICK:
        return Downloaded(
            [
                await download_quick_youtube(
                    parsed_input=stored.parsed_input,
                    option=option,
                    settings=settings,
                    work_dir=work_dir,
                    status=status,
                )
            ]
        )
    info = payload.info if isinstance(payload, ProbePayload) else {}
    if request_type is RequestType.YTDLP_AUTO:
        return Downloaded(
            [
                await download_best_quality(
                    parsed_input=stored.parsed_input,
                    settings=settings,
                    work_dir=work_dir,
                    info=info,
                    status=status,
                )
            ]
        )
    if request_type is RequestType.YTDLP_SELECTION:
        return Downloaded(
            [
                await download_selected_format(
                    parsed_input=stored.parsed_input,
                    option=option,
                    info=info,
                    settings=settings,
                    work_dir=work_dir,
                    status=status,
                )
            ]
        )
    raise RuntimeError(f"request type {request_type!r} has no downloader")