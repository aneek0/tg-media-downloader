from __future__ import annotations

import json
import logging
from pathlib import Path

from config import Settings
from services.ytdlp import VIDEO_EXTENSIONS, _prepare_cookies_file, _run_command
from utils.logging_config import safe_url_label
from utils.models import DownloadArtifact, ParsedInput

logger = logging.getLogger(__name__)

IMAGE_EXTENSIONS = {"jpg", "jpeg", "png", "webp"}
GALLERY_FILENAME_FORMAT = "{tweet_id}_{num}.{extension}"


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
    return [
        "gallery-dl",
        "-D",
        str(work_dir),
        "-f",
        GALLERY_FILENAME_FORMAT,
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


async def download_gallery_media(
    *, parsed_input: ParsedInput, settings: Settings, work_dir: Path
) -> list[DownloadArtifact]:
    logger.info(
        "Gallery probe starting | source=%s work_dir=%s",
        safe_url_label(parsed_input.source_url),
        work_dir,
    )
    stdout, _ = await _run_command(_gallery_probe_command(parsed_input, settings))
    file_dicts, content, error = _parse_gallery_probe(stdout)
    if error:
        raise RuntimeError(_gallery_friendly_error(error, bool(settings.twitter_cookies)))
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
    await _run_command(command, cwd=work_dir)

    files = sorted(path for path in work_dir.iterdir() if path.is_file())
    if not files:
        raise RuntimeError("gallery-dl downloaded no files")

    caption = parsed_input.custom_file_name or (content or "").strip() or files[0].stem
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
