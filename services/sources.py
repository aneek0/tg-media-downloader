"""Source classification: one place that decides what a URL is.

Everything that branches on "is this an image / a music page / a direct
media file / a page to probe" asks this module instead of re-deriving
extension sets and host checks at each call site.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from urllib.parse import urlparse

from gallery_dl import extractor as _gdl_extractor

from services.mornye import is_mornye_url
from services.parsing import is_twitter_status_url
from utils.models import DownloadOption, ParsedInput

IMAGE_EXTENSIONS = frozenset({"jpg", "jpeg", "png", "webp"})
VIDEO_EXTENSIONS = frozenset({"mp4", "mkv", "webm", "mov"})
AUDIO_EXTENSIONS = frozenset(
    {"mp3", "m4a", "aac", "wav", "flac", "opus", "weba", "ogg"}
)


class SourceKind(StrEnum):
    IMAGE = "image"
    MUSIC = "music"
    DIRECT_MEDIA = "direct_media"
    PAGE = "page"


@dataclass(frozen=True, slots=True)
class SourceUrl:
    """What the URL alone tells us, before any network probe."""

    parsed_input: ParsedInput
    extension: str | None
    kind: SourceKind
    send_type: str
    is_twitter: bool
    gallery_capable: bool


def extension(url: str) -> str | None:
    """Lowercase extension from the URL path, if any."""
    suffix = Path(urlparse(url).path).suffix
    return suffix.lstrip(".").lower() if suffix else None


def send_type_for_extension(ext: str | None) -> str:
    if ext in IMAGE_EXTENSIONS:
        return "photo"
    if ext in VIDEO_EXTENSIONS:
        return "video"
    if ext in AUDIO_EXTENSIONS:
        return "audio"
    return "document"


def gallery_supports_url(url: str) -> bool:
    """In-process extractor gate: URLs gallery-dl cannot handle (YouTube,
    unknown hosts) are rejected without spawning a subprocess. The first
    call warms the extractor table once; later calls cost microseconds."""
    try:
        return _gdl_extractor.find(url) is not None
    except Exception:  # pylint: disable=broad-exception-caught  # gallery-dl API, not our contract
        return False


def classify(parsed_input: ParsedInput) -> SourceUrl:
    url = parsed_input.source_url
    ext = extension(url)
    if is_mornye_url(url):
        kind = SourceKind.MUSIC
    elif ext in IMAGE_EXTENSIONS:
        kind = SourceKind.IMAGE
    elif ext in VIDEO_EXTENSIONS or ext in AUDIO_EXTENSIONS:
        kind = SourceKind.DIRECT_MEDIA
    else:
        kind = SourceKind.PAGE
    return SourceUrl(
        parsed_input=parsed_input,
        extension=ext,
        kind=kind,
        send_type=send_type_for_extension(ext),
        is_twitter=is_twitter_status_url(url),
        gallery_capable=gallery_supports_url(url),
    )


def _effective_extension(source: SourceUrl, info: dict | None) -> str | None:
    if info and info.get("ext"):
        return str(info["ext"]).lower()
    return source.extension


def build_direct_options(
    source: SourceUrl, info: dict | None = None
) -> list[DownloadOption]:
    """Options for the plain-HTTP downloader: media or document flavour."""
    ext = _effective_extension(source, info)
    send_type = send_type_for_extension(ext)
    options = [
        DownloadOption(
            option_id="direct_primary",
            label="Send as media" if send_type != "document" else "Send as document",
            send_type=send_type,
            file_ext=ext,
        )
    ]
    if send_type != "document":
        options.append(
            DownloadOption(
                option_id="direct_document",
                label="Send as document",
                send_type="document",
                file_ext=ext,
            )
        )
    return options