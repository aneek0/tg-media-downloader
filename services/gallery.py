from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from gallery_dl import extractor as _gdl_extractor

from config import Settings
from services.parsing import is_twitter_status_url
from services.proc import run_command
from services.ytdlp import VIDEO_EXTENSIONS, _prepare_cookies_file
from utils.logging_config import safe_url_label
from utils.models import DownloadArtifact, ParsedInput

logger = logging.getLogger(__name__)

IMAGE_EXTENSIONS = {"jpg", "jpeg", "png", "webp"}
GALLERY_FILENAME_FORMAT = "{tweet_id}_{num}.{extension}"
GENERIC_GALLERY_FILENAME_FORMAT = "{filename}.{extension}"

MAX_GALLERY_PHOTOS = 50  # local ceiling; twitter itself never exceeds ~4 per status


def gallery_supports_url(url: str) -> bool:
    """In-process extractor gate: URLs gallery-dl cannot handle (YouTube,
    unknown hosts) are rejected without spawning a subprocess. The first
    call warms the extractor table once; later calls cost microseconds."""
    try:
        return _gdl_extractor.find(url) is not None
    except Exception:  # pylint: disable=broad-exception-caught  # gallery-dl API, not our contract
        return False


def split_tweet_media(file_dicts: list[dict]) -> tuple[list[dict], list[dict]]:
    """Split probe file_dicts into (photos, videos) by VIDEO_EXTENSIONS -
    the same rule _inline_media_results uses."""
    photos = [m for m in file_dicts if (m.get("extension") or "").lower() not in VIDEO_EXTENSIONS]
    videos = [m for m in file_dicts if (m.get("extension") or "").lower() in VIDEO_EXTENSIONS]
    return photos[:MAX_GALLERY_PHOTOS], videos


def _gallery_flags(settings: Settings) -> list[str]:
    flags: list[str] = []
    if settings.http_proxy:
        flags.extend(["--proxy", settings.http_proxy])
    cookies_file = _prepare_cookies_file(settings)
    if cookies_file:
        flags.extend(["-C", str(cookies_file)])
        flags.extend(["-o", "extractor.twitter.cookies-update=false"])
    return flags


def _gallery_probe_command(parsed_input: ParsedInput, settings: Settings) -> list[str]:
    return ["gallery-dl", "-j", *_gallery_flags(settings), parsed_input.source_url]


def _gallery_download_command(
    parsed_input: ParsedInput, settings: Settings, work_dir: Path
) -> list[str]:
    filename_format = (
        GALLERY_FILENAME_FORMAT
        if is_twitter_status_url(parsed_input.source_url)
        else GENERIC_GALLERY_FILENAME_FORMAT
    )
    return [
        "gallery-dl",
        "-D",
        str(work_dir),
        "-f",
        filename_format,
        "--no-part",
        "--no-mtime",
        *_gallery_flags(settings),
        parsed_input.source_url,
    ]


def _parse_gallery_probe(stdout: str) -> tuple[list[dict], str | None, str | None]:
    entries = json.loads(stdout or "[]")
    if not isinstance(entries, list):
        return [], None, "gallery-dl probe returned unexpected data"
    file_dicts: list[dict] = []
    content: str | None = None
    error: str | None = None
    for entry in entries:
        if not isinstance(entry, list) or not entry:
            continue
        kind = entry[0]
        if kind == -1:
            payload = entry[1] if len(entry) > 1 and isinstance(entry[1], dict) else {}
            error = str(payload.get("message") or payload.get("error") or "gallery-dl error")
        elif kind == 3:
            payload = entry[2] if len(entry) > 2 and isinstance(entry[2], dict) else {}
            if len(entry) > 1 and isinstance(entry[1], str):
                payload["_url"] = entry[1]
            file_dicts.append(payload)
            if content is None:
                content = payload.get("content")
        elif kind == 2:
            payload = entry[1] if len(entry) > 1 and isinstance(entry[1], dict) else {}
            if content is None:
                content = payload.get("content")
    return file_dicts, content, error


@dataclass(slots=True)
class GalleryProbe:
    file_dicts: list[dict]
    content: str | None
    error: str | None


async def probe_gallery(parsed_input: ParsedInput, settings: Settings) -> GalleryProbe:
    """Probe a URL with `gallery-dl -j` without raising: unsupported sites
    never reach the subprocess (gallery_supports_url gate)."""
    if not gallery_supports_url(parsed_input.source_url):
        return GalleryProbe([], None, f"Unsupported URL: {parsed_input.source_url}")
    try:
        stdout, _ = await run_command(
            _gallery_probe_command(parsed_input, settings),
            timeout=settings.gallery_probe_timeout,
        )
    except RuntimeError as exc:
        return GalleryProbe([], None, str(exc))
    try:
        file_dicts, content, error = _parse_gallery_probe(stdout)
    except ValueError:  # json.JSONDecodeError
        return GalleryProbe([], None, "gallery-dl probe returned invalid JSON")
    return GalleryProbe(file_dicts, content, error)


def small_thumbnail_url(url: str) -> str | None:
    """Known-CDN thumbnail variants: twitter `?name=small`, pawchive
    `img.pawchive.pw/thumbnail`. Unknown hosts -> None (caller uses the
    full URL)."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if host == "pbs.twimg.com":
        query = [(key, value) for key, value in parse_qsl(parsed.query) if key != "name"]
        query.append(("name", "small"))
        return urlunparse(parsed._replace(query=urlencode(query)))
    if host == "file.pawchive.pw":
        return urlunparse(
            parsed._replace(netloc="img.pawchive.pw", path="/thumbnail" + parsed.path)
        )
    return None


_TAG_RE = re.compile(r"<[^>]+>")


def plain_caption(content: str | None) -> str | None:
    """Strip HTML tags (pawchive returns <p>...</p>) and collapse spaces."""
    if not content:
        return None
    cleaned = re.sub(r"\s+", " ", _TAG_RE.sub(" ", content)).strip()
    return cleaned or None


def _gallery_friendly_error(error: str, cookies_configured: bool) -> str:
    if "unavailable" in error.lower():
        if cookies_configured:
            return (
                f"Twitter: media unavailable ({error}). The tweet may be deleted, "
                "protected, or the cookies account may need its age confirmed."
            )
        return (
            "Twitter 18+ post: login cookies required. "
            "Set TWITTER_COOKIES_FILE (path) or TWITTER_COOKIES (file content) "
            "to a logged-in X account's cookies — see "
            "https://github.com/yt-dlp/yt-dlp/wiki/FAQ#how-do-i-pass-cookies-to-yt-dlp"
        )
    return error


def _send_type_for_extension(ext: str) -> str:
    if ext in IMAGE_EXTENSIONS:
        return "photo"
    if ext in VIDEO_EXTENSIONS:
        return "video"
    return "document"


def gallery_url_items(
    file_dicts: list[dict], content: str | None, parsed_input: ParsedInput
) -> list[dict]:
    """URL-send items for the Telegram-CDN path (stored.info['url_media']):
    Telegram fetches each raw CDN URL itself."""
    caption = parsed_input.custom_file_name or plain_caption(content)
    items: list[dict] = []
    for media in file_dicts:
        url = media.get("_url")
        if not url:
            continue
        ext = (media.get("extension") or "").lower()
        name = media.get("filename") or "media"
        if ext and not name.lower().endswith(f".{ext}"):
            name = f"{name}.{ext}"
        items.append(
            {
                "url": url,
                "filename": name,
                "send_type": _send_type_for_extension(ext),
                "caption": caption[:1024] if (caption and not items) else None,
            }
        )
    return items


async def download_gallery_media(
    *,
    parsed_input: ParsedInput,
    settings: Settings,
    work_dir: Path,
    file_dicts: list[dict] | None = None,
    content: str | None = None,
) -> list[DownloadArtifact]:
    if file_dicts is None:
        logger.info(
            "Gallery probe starting | source=%s work_dir=%s",
            safe_url_label(parsed_input.source_url),
            work_dir,
        )
        stdout, _ = await run_command(
            _gallery_probe_command(parsed_input, settings),
            timeout=settings.process_max_timeout,
        )
        file_dicts, content, error = _parse_gallery_probe(stdout)
        if error:
            raise RuntimeError(
                _gallery_friendly_error(error, bool(settings.twitter_cookies))
            )
        if not file_dicts:
            raise RuntimeError("No media found in this tweet")

    work_dir.mkdir(parents=True, exist_ok=True)
    command = _gallery_download_command(parsed_input, settings, work_dir)
    logger.info(
        "Gallery download starting | source=%s files=%s work_dir=%s",
        safe_url_label(parsed_input.source_url),
        len(file_dicts),
        work_dir,
    )
    await run_command(command, cwd=work_dir, timeout=settings.process_max_timeout)

    files = sorted(path for path in work_dir.iterdir() if path.is_file())
    if not files:
        raise RuntimeError("gallery-dl downloaded no files")

    body = parsed_input.custom_file_name or plain_caption(content) or files[0].stem
    caption = f"{parsed_input.source_url}\n{body}"
    artifacts = [
        DownloadArtifact(
            path=path,
            file_name=path.name,
            send_type=_send_type_for_extension(path.suffix.lstrip(".").lower()),
            caption=caption[:1000],
        )
        for path in files
    ]
    logger.info(
        "Gallery download complete | files=%s send_types=%s",
        [a.file_name for a in artifacts],
        [a.send_type for a in artifacts],
    )
    return artifacts
