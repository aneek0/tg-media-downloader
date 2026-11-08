from __future__ import annotations

import asyncio
import html
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import cast
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import aiohttp

from config import Settings
from services.direct_downloads import download_http_file
from services.parsing import is_twitter_status_url
from services.proc import run_command
from services.sources import (
    VIDEO_EXTENSIONS,
    gallery_supports_url,
    is_tiktok_url,
    send_type_for_extension,
)
from services.status import StatusSurface
from services.ytdlp import _prepare_cookies_file
from utils import text
from utils.logging_config import safe_url_label
from utils.models import DownloadArtifact, FileTooLargeError, ParsedInput

logger = logging.getLogger(__name__)

GALLERY_FILENAME_FORMAT = "{tweet_id}_{num}.{extension}"
GENERIC_GALLERY_FILENAME_FORMAT = "{filename}.{extension}"

MAX_GALLERY_PHOTOS = 50  # local ceiling; twitter itself never exceeds ~4 per status

GALLERY_DOWNLOADS_PER_REQUEST = 3
GALLERY_DOWNLOADS_PER_BOT = 6


def split_tweet_media(file_dicts: list[dict]) -> tuple[list[dict], list[dict]]:
    """Split probe file_dicts into (photos, videos) by VIDEO_EXTENSIONS -
    the same rule _inline_media_results uses."""
    photos = [m for m in file_dicts if (m.get("extension") or "").lower() not in VIDEO_EXTENSIONS]
    videos = [m for m in file_dicts if (m.get("extension") or "").lower() in VIDEO_EXTENSIONS]
    return photos[:MAX_GALLERY_PHOTOS], videos


def _gallery_flags(settings: Settings, source_url: str) -> list[str]:
    flags: list[str] = []
    if settings.http_proxy:
        flags.extend(["--proxy", settings.http_proxy])
    cookies_file = _prepare_cookies_file(settings)
    if cookies_file:
        flags.extend(["-C", str(cookies_file)])
        flags.extend(["-o", "extractor.twitter.cookies-update=false"])
    if is_twitter_status_url(source_url):
        flags.extend(["-o", "extractor.twitter.text-tweets=true"])
    if is_tiktok_url(source_url):
        # Keep imagePost photos/audio here; videos use yt-dlp's H.264 path.
        flags.extend(["-o", "videos=false"])
    return flags


def _gallery_probe_command(parsed_input: ParsedInput, settings: Settings) -> list[str]:
    return [
        "gallery-dl",
        "-j",
        *_gallery_flags(settings, parsed_input.source_url),
        parsed_input.source_url,
    ]


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
        *_gallery_flags(settings, parsed_input.source_url),
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
_INLINE_SPACE_RE = re.compile(r"[^\S\n]+")
_BLANK_LINES_RE = re.compile(r"\n{3,}")


def plain_caption(content: str | None) -> str | None:
    """Strip HTML tags (pawchive returns <p>...</p>), collapse runs of spaces
    and keep the author's line breaks."""
    if not content:
        return None
    cleaned = _INLINE_SPACE_RE.sub(" ", _TAG_RE.sub(" ", content))
    cleaned = _BLANK_LINES_RE.sub("\n\n", cleaned)
    cleaned = "\n".join(line.strip() for line in cleaned.split("\n")).strip()
    return cleaned or None


def _linked_caption(body: str, source_url: str) -> str:
    """Render a post name as a single minimalist link to its source."""
    return f'<b><a href="{html.escape(source_url, quote=True)}">{text.esc(body)}</a></b>'


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


def gallery_url_items(
    file_dicts: list[dict], content: str | None, parsed_input: ParsedInput
) -> list[dict]:
    """URL-send items for the Telegram-CDN path (stored.info['url_media']):
    Telegram fetches each raw CDN URL itself."""
    fallback_name = next(
        (str(media["filename"]).strip() for media in file_dicts if media.get("filename")), ""
    )
    body = parsed_input.custom_file_name or plain_caption(content) or fallback_name or parsed_input.source_url
    caption = _linked_caption(body, parsed_input.source_url)
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
                "send_type": send_type_for_extension(ext),
                "caption": caption if not items else None,
            }
        )
    return items


def _gallery_local_name(media: dict, index: int, is_twitter: bool) -> tuple[str, str | None]:
    extension = str(media.get("extension") or "").lower().lstrip(".")
    if is_twitter:
        identifier = media.get("tweet_id") or media.get("id") or "media"
        number = media.get("num") or index
        name = f"{identifier}_{number}"
    else:
        name = str(media.get("filename") or media.get("id") or f"media_{index}")
    name = Path(name).name
    if name in {"", ".", ".."}:
        name = f"media_{index}"
    if extension and not name.lower().endswith(f".{extension}"):
        name = f"{name}.{extension}"
    return name, extension or None


async def _download_gallery_files(
    *,
    parsed_input: ParsedInput,
    settings: Settings,
    work_dir: Path,
    file_dicts: list[dict],
    status: StatusSurface,
    download_limiter: asyncio.Semaphore,
) -> list[Path]:
    """Download an album concurrently, retaining its probe order."""
    targets = []
    used_names = set()
    is_twitter = is_twitter_status_url(parsed_input.source_url)
    for index, media in enumerate(file_dicts, 1):
        url = media.get("_url")
        if not isinstance(url, str) or urlparse(url).scheme not in {"http", "https"}:
            raise RuntimeError("Gallery item has no directly downloadable URL")
        name, extension = _gallery_local_name(media, index, is_twitter)
        if name in used_names:
            path = Path(name)
            name = f"{path.stem}_{index}{path.suffix}"
        used_names.add(name)
        raw_headers = media.get("_http_headers")
        headers = (
            {str(key): str(value) for key, value in raw_headers.items()}
            if isinstance(raw_headers, dict)
            else None
        )
        targets.append((url, headers, name, extension))

    request_limiter = asyncio.Semaphore(GALLERY_DOWNLOADS_PER_REQUEST)
    timeout = aiohttp.ClientTimeout(total=settings.process_max_timeout)
    connector = aiohttp.TCPConnector(
        ssl=cast(bool, None if settings.verify_ssl else False)
    )
    async with aiohttp.ClientSession(timeout=timeout, connector=connector) as client:

        async def download(target) -> Path:
            url, headers, name, extension = target
            async with request_limiter, download_limiter:
                return await download_http_file(
                    session=client,
                    url=url,
                    headers=headers,
                    proxy=settings.http_proxy or None,
                    work_dir=work_dir,
                    file_name=name,
                    file_ext=extension,
                    send_type=send_type_for_extension(extension),
                    status=status,
                    settings=settings,
                )

        tasks = [asyncio.create_task(download(target)) for target in targets]
        try:
            return list(await asyncio.gather(*tasks))
        except BaseException:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise


async def _download_gallery_album(
    *,
    parsed_input: ParsedInput,
    settings: Settings,
    work_dir: Path,
    file_dicts: list[dict],
    status: StatusSurface,
    download_limiter: asyncio.Semaphore,
) -> list[Path]:
    """Try the parallel CDN path; return an empty list for gallery-dl fallback."""
    try:
        return await _download_gallery_files(
            parsed_input=parsed_input,
            settings=settings,
            work_dir=work_dir,
            file_dicts=file_dicts,
            status=status,
            download_limiter=download_limiter,
        )
    except FileTooLargeError:
        raise
    except (aiohttp.ClientError, OSError, RuntimeError, ValueError) as exc:
        logger.warning(
            "Parallel gallery download failed; using gallery-dl | source=%s error=%s",
            safe_url_label(parsed_input.source_url),
            exc,
        )
        for path in work_dir.iterdir():
            if path.is_file():
                path.unlink(missing_ok=True)
        return []


async def download_gallery_media(
    *,
    parsed_input: ParsedInput,
    settings: Settings,
    work_dir: Path,
    status: StatusSurface,
    download_limiter: asyncio.Semaphore,
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
    logger.info(
        "Gallery download starting | source=%s files=%s work_dir=%s",
        safe_url_label(parsed_input.source_url),
        len(file_dicts),
        work_dir,
    )
    files = await _download_gallery_album(
        parsed_input=parsed_input,
        settings=settings,
        work_dir=work_dir,
        file_dicts=file_dicts,
        status=status,
        download_limiter=download_limiter,
    )
    if not files:
        command = _gallery_download_command(parsed_input, settings, work_dir)
        await run_command(command, cwd=work_dir, timeout=settings.process_max_timeout)
        files = sorted(path for path in work_dir.iterdir() if path.is_file())
        if not files:
            raise RuntimeError("gallery-dl downloaded no files")

    body = parsed_input.custom_file_name or plain_caption(content) or files[0].stem
    caption = _linked_caption(body, parsed_input.source_url)
    artifacts = [
        DownloadArtifact(
            path=path,
            file_name=path.name,
            send_type=send_type_for_extension(path.suffix.lstrip(".").lower()),
            caption=caption,
        )
        for path in files
    ]
    logger.info(
        "Gallery download complete | files=%s send_types=%s",
        [a.file_name for a in artifacts],
        [a.send_type for a in artifacts],
    )
    return artifacts
