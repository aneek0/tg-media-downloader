from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Union

class RequestType(StrEnum):
    GALLERY_MEDIA = "gallery_media"
    MORNYE = "mornye"
    YOUTUBE_QUICK = "youtube_quick"
    YTDLP_SELECTION = "ytdlp_selection"
    YTDLP_AUTO = "ytdlp_auto"
    DIRECT_DOWNLOAD = "direct_download"
    INLINE_MEDIA = "inline_media"
    INLINE_GALLERY = "inline_gallery"
    CATALOGUE_BROWSE = "catalogue_browse"


@dataclass(slots=True)
class GalleryPayload:
    """Telegram-CDN send list: one dict per media item with url/filename/
    send_type/caption."""

    url_media: list[dict[str, Any]] = field(default_factory=list)

    def to_info(self) -> dict[str, Any]:
        return {"url_media": self.url_media}


@dataclass(slots=True)
class InlineGalleryPayload:
    photos: list[str] = field(default_factory=list)
    caption: str | None = None

    def to_info(self) -> dict[str, Any]:
        return {"photos": self.photos, "caption": self.caption}


@dataclass(slots=True)
class InlineMediaPayload:
    preferred_name: str | None = None
    preferred_index: int | None = None
    poster_url: str | None = None
    # editMessageMedia on an inline message cannot upload a new file, so the
    # big-video edit reuses the CDN URL (or the DM-uploaded file_id).
    video_url: str | None = None

    def to_info(self) -> dict[str, Any]:
        return {
            "preferred_name": self.preferred_name,
            "preferred_index": self.preferred_index,
            "poster_url": self.poster_url,
            "video_url": self.video_url,
        }


@dataclass(slots=True)
class MusicPayload:
    kind: str
    name: str
    count: int

    def to_info(self) -> dict[str, Any]:
        return {"kind": self.kind, "name": self.name, "count": self.count}

@dataclass(slots=True)
class CataloguePayload:  # pylint: disable=too-many-instance-attributes
    """Persisted Artist Card and album browsing state."""

    provider: str = ""
    artist_id: str = ""
    name: str = ""
    cover_url: str = ""
    artist_url: str = ""
    listeners: int = 0
    top_tracks: list[dict[str, Any]] = field(default_factory=list)
    albums: list[dict[str, Any]] = field(default_factory=list)
    media_message: bool = True
    album: dict[str, Any] = field(default_factory=dict)
    album_tracks: list[dict[str, Any]] = field(default_factory=list)
    parent_token: str = ""

    def to_info(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ProbePayload:
    """Raw yt-dlp probe metadata for the format/auto routes."""

    info: dict[str, Any] = field(default_factory=dict)

    def to_info(self) -> dict[str, Any]:
        return self.info


@dataclass(slots=True)
class DirectPayload:
    """Direct-download hints; may carry yt-dlp metadata when the probe ran."""

    info: dict[str, Any] = field(default_factory=dict)

    @property
    def ext(self) -> str | None:
        ext = self.info.get("ext")
        return str(ext).lower() if ext else None

    def to_info(self) -> dict[str, Any]:
        return self.info


RequestPayload = Union[
    GalleryPayload,
    InlineGalleryPayload,
    InlineMediaPayload,
    MusicPayload,
    CataloguePayload,
    ProbePayload,
    DirectPayload,
]


def request_payload(request_type: RequestType, info: dict[str, Any]) -> RequestPayload:
    """Decode the legacy ``info`` dict into the variant's payload. Unknown
    keys are ignored so stale request files keep resolving."""
    if request_type is RequestType.GALLERY_MEDIA:
        return GalleryPayload(url_media=list(info.get("url_media") or []))
    if request_type is RequestType.INLINE_GALLERY:
        return InlineGalleryPayload(
            photos=list(info.get("photos") or []),
            caption=info.get("caption"),
        )
    if request_type is RequestType.INLINE_MEDIA:
        return InlineMediaPayload(
            preferred_name=info.get("preferred_name"),
            preferred_index=info.get("preferred_index"),
            poster_url=info.get("poster_url"),
            video_url=info.get("video_url"),
        )
    if request_type is RequestType.MORNYE:
        return MusicPayload(
            kind=info.get("kind", ""),
            name=info.get("name", ""),
            count=int(info.get("count") or 0),
        )
    if request_type is RequestType.CATALOGUE_BROWSE:
        return CataloguePayload(
            provider=info.get("provider", ""),
            artist_id=info.get("artist_id", ""),
            name=info.get("name", ""),
            cover_url=info.get("cover_url", ""),
            artist_url=info.get("artist_url", ""),
            listeners=int(info.get("listeners") or 0),
            top_tracks=list(info.get("top_tracks") or []),
            albums=list(info.get("albums") or []),
            media_message=bool(info.get("media_message", True)),
            album=dict(info.get("album") or {}),
            album_tracks=list(info.get("album_tracks") or []),
            parent_token=info.get("parent_token", ""),
        )
    if request_type is RequestType.DIRECT_DOWNLOAD:
        return DirectPayload(dict(info))
    return ProbePayload(dict(info))


@dataclass(slots=True)
class ParsedInput:
    source_url: str
    custom_file_name: str | None = None
    username: str | None = None
    password: str | None = None


@dataclass(slots=True)
class DownloadOption:
    option_id: str
    label: str
    send_type: str
    format_id: str | None = None
    file_ext: str | None = None
    audio_quality: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "DownloadOption":
        # Request files on disk may predate the removal of `mode`; unknown
        # keys are dropped so a stale token resolves instead of raising.
        known = {"option_id", "label", "send_type", "format_id", "file_ext", "audio_quality"}
        return cls(**{key: payload[key] for key in known if key in payload})


@dataclass(slots=True)
class StoredRequest:
    token: str
    request_type: RequestType | str
    parsed_input: ParsedInput
    options: list[DownloadOption] = field(default_factory=list)
    info: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.request_type = RequestType(self.request_type)

    @property
    def payload(self) -> RequestPayload:
        return request_payload(self.request_type, self.info)

    def to_dict(self) -> dict[str, Any]:
        return {
            "token": self.token,
            "request_type": str(self.request_type),
            "parsed_input": asdict(self.parsed_input),
            "options": [option.to_dict() for option in self.options],
            "info": self.payload.to_info(),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "StoredRequest":
        return cls(
            token=payload["token"],
            request_type=RequestType(payload["request_type"]),
            parsed_input=ParsedInput(**payload["parsed_input"]),
            options=[DownloadOption.from_dict(item) for item in payload["options"]],
            info=payload.get("info", {}),
        )


@dataclass(slots=True)
class DownloadArtifact:
    path: Path
    file_name: str
    send_type: str
    caption: str
    duration: int | None = None
    width: int | None = None
    height: int | None = None
    # Audio (FLAC/MP3) display metadata for Telegram's player, plus an
    # embedded-cover extract used as the audio thumbnail.
    audio_title: str | None = None
    audio_performer: str | None = None
    cover_path: Path | None = None


@dataclass(slots=True)
class CachedMedia:
    file_id: str
    send_type: str
    file_name: str
    caption: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "CachedMedia":
        return cls(**payload)


class FileTooLargeError(RuntimeError):
    """Raised when a download exceeds Telegram's upload size limit."""
