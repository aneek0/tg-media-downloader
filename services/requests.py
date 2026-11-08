from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from typing import Any

from services.request_store import RequestStore
from utils.models import (
    DirectPayload,
    DownloadOption,
    GalleryPayload,
    InlineGalleryPayload,
    InlineMediaPayload,
    MusicPayload,
    CataloguePayload,
    ParsedInput,
    ProbePayload,
    RequestType,
    StoredRequest,
)

_PROBE_TYPES = {
    RequestType.YOUTUBE_QUICK,
    RequestType.YTDLP_SELECTION,
    RequestType.YTDLP_AUTO,
}


def _save(store: RequestStore, stored: StoredRequest) -> StoredRequest:
    store.save(stored)
    return stored


def gallery_request(
    store: RequestStore,
    parsed_input: ParsedInput,
    options: Sequence[DownloadOption],
    url_media: list[dict[str, Any]],
    *,
    content: str | None = None,
) -> StoredRequest:
    """Gallery/CDN request, including a text-only post when no media exists."""
    return _save(
        store,
        StoredRequest(
            token=store.create_token(),
            request_type=RequestType.GALLERY_MEDIA,
            parsed_input=parsed_input,
            options=list(options),
            info=GalleryPayload(url_media=url_media, content=content).to_info(),
        ),
    )


def probe_request(
    store: RequestStore,
    request_type: RequestType,
    parsed_input: ParsedInput,
    options: Sequence[DownloadOption],
    info: dict[str, Any],
) -> StoredRequest:
    """Probe-backed request (YouTube quick, format selection, auto best)."""
    if request_type not in _PROBE_TYPES:
        raise ValueError(f"not a probe request type: {request_type}")
    return _save(
        store,
        StoredRequest(
            token=store.create_token(),
            request_type=request_type,
            parsed_input=parsed_input,
            options=list(options),
            info=ProbePayload(info=dict(info)).to_info(),
        ),
    )


def direct_request(
    store: RequestStore,
    parsed_input: ParsedInput,
    options: Sequence[DownloadOption],
    info: dict[str, Any] | None = None,
) -> StoredRequest:
    """Direct-download request; ``info`` may carry yt-dlp hints (``ext``)."""
    return _save(
        store,
        StoredRequest(
            token=store.create_token(),
            request_type=RequestType.DIRECT_DOWNLOAD,
            parsed_input=parsed_input,
            options=list(options),
            info=DirectPayload(info=dict(info or {})).to_info(),
        ),
    )


def music_request(
    store: RequestStore,
    parsed_input: ParsedInput,
    *,
    kind: str,
    name: str,
    count: int,
    options: Sequence[DownloadOption] = (),
) -> StoredRequest:
    return _save(
        store,
        StoredRequest(
            token=store.create_token(),
            request_type=RequestType.MORNYE,
            parsed_input=parsed_input,
            options=list(options),
            info=MusicPayload(kind=kind, name=name, count=count).to_info(),
        ),
    )


def inline_media_request(
    store: RequestStore,
    parsed_input: ParsedInput,
    *,
    preferred_name: str | None = None,
    preferred_index: int | None = None,
    poster_url: str | None = None,
    video_url: str | None = None,
) -> StoredRequest:
    return _save(
        store,
        StoredRequest(
            token=store.create_token(),
            request_type=RequestType.INLINE_MEDIA,
            parsed_input=parsed_input,
            info=InlineMediaPayload(
                preferred_name=preferred_name,
                preferred_index=preferred_index,
                poster_url=poster_url,
                video_url=video_url,
            ).to_info(),
        ),
    )


def inline_gallery_request(
    store: RequestStore,
    parsed_input: ParsedInput,
    photos: list[str],
    caption: str | None = None,
) -> StoredRequest:
    return _save(
        store,
        StoredRequest(
            token=store.create_token(),
            request_type=RequestType.INLINE_GALLERY,
            parsed_input=parsed_input,
            info=InlineGalleryPayload(photos=photos, caption=caption).to_info(),
        ),
    )

def catalogue_browse_request(
    store: RequestStore,
    parsed_input: ParsedInput,
    payload: CataloguePayload | None = None,
) -> StoredRequest:
    """Persist an Artist Card or one of its album views."""
    return _save(
        store,
        StoredRequest(
            token=store.create_token(),
            request_type=RequestType.CATALOGUE_BROWSE,
            parsed_input=parsed_input,
            info=(payload or CataloguePayload()).to_info(),
        ),
    )

@contextmanager
def lifecycle(store: RequestStore, token: str) -> Iterator[str]:
    """Guarantee request-token and work-directory cleanup on every terminal
    path (success, failure, cancellation)."""
    try:
        yield token
    finally:
        store.delete(token)