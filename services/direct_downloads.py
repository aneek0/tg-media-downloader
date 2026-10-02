from __future__ import annotations

import asyncio
import logging
import mimetypes
import os
import re
import time
from pathlib import Path
from urllib.parse import urlparse

import aiohttp
from aiogram.types import Message

from config import Settings
from services.progress import format_download_progress
from utils.logging_config import safe_url_label
from utils.models import (
    DownloadArtifact,
    DownloadOption,
    FileTooLargeError,
    ParsedInput,
)


# Files smaller than this are not split: extra requests cost more than they save.
_SEGMENT_MIN_BYTES = 1024 * 1024
_PROGRESS_INTERVAL_SECONDS = 2
# A parallel segment that delivers no socket data for this long is treated as dead: the
# whole attempt is abandoned and retried as a single stream (see download_direct_file).
# The timeout applies per socket read, not per chunk, so a live connection needs only
# ~10 KiB/s to stay under it; it fires only when a server parks a range request.
_SEGMENT_STALL_SECONDS = 15

# Попыток на сегмент; каждый ретрай возобновляется с последнего записанного
# смещения, поэтому флаки-соединение стоит остаток одного сегмента, а не весь файл.
_SEGMENT_MAX_ATTEMPTS = 3


logger = logging.getLogger(__name__)


def _filename_from_url(url: str) -> str:
    parsed = urlparse(url)
    name = Path(parsed.path).name
    return name or "downloaded-file"


def _normalize_file_name(file_name: str, ext: str | None) -> str:
    if not ext:
        return file_name
    if file_name.lower().endswith(f".{ext.lower()}"):
        return file_name
    return f"{file_name}.{ext}"


def _progress_milestone(downloaded: int, total: int) -> int | None:
    if not total:
        return None
    milestone = int((downloaded / total) * 4) * 25
    return milestone if milestone in {25, 50, 75, 100} else None


def _parse_content_range(value: str) -> tuple[int, int, int] | None:
    match = re.match(r"bytes (\d+)-(\d+)/(\d+)", value.strip())
    if match is None:
        return None
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def _segment_ranges(total: int, threads: int) -> list[tuple[int, int]]:
    """Inclusive byte ranges covering ``[0, total)`` split over at most ``threads`` parts."""
    count = max(1, min(threads, total // _SEGMENT_MIN_BYTES))
    size = total // count
    ranges = [(index * size, (index + 1) * size - 1) for index in range(count)]
    ranges[-1] = (ranges[-1][0], total - 1)
    return ranges


def _resolve_target(
    work_dir: Path,
    file_name: str,
    option: DownloadOption,
    suggested_ext: str | None,
    content_type: str,
) -> tuple[str, Path]:
    ext = option.file_ext or suggested_ext
    if not ext:
        guessed_ext = mimetypes.guess_extension(content_type.split(";")[0].strip())
        ext = guessed_ext.lstrip(".") if guessed_ext else None
    file_name = _normalize_file_name(file_name, ext)
    return file_name, work_dir / file_name


def _reject_html(content_type: str) -> None:
    if content_type.split(";")[0].strip().lower() == "text/html":
        raise RuntimeError(
            "This link returns a web page, not a media file. "
            "It is not directly downloadable."
        )


def _write_all(fd: int, data: bytes, offset: int) -> None:
    """``os.pwrite`` may write fewer bytes than asked; loop until the buffer is out."""
    view = memoryview(data)
    while view:
        written = os.pwrite(fd, view, offset)
        if written <= 0:  # pragma: no cover - regular files always make progress
            raise OSError(f"pwrite returned {written}")
        view = view[written:]
        offset += written



async def _probe_range(
    session: aiohttp.ClientSession, url: str, proxy: str | None
) -> tuple[int, str] | None:
    """Return ``(total_bytes, content_type)`` when the server honors byte ranges."""
    try:
        async with session.get(
            url,
            proxy=proxy,
            headers={"Range": "bytes=0-0", "Accept-Encoding": "identity"},
            timeout=aiohttp.ClientTimeout(sock_read=_SEGMENT_STALL_SECONDS),
        ) as response:
            if response.status != 206:
                return None
            parsed = _parse_content_range(response.headers.get("Content-Range", ""))
            if parsed is None or parsed[0] != 0 or parsed[2] <= 0:
                return None
            # Drain the one-byte probe body so the connection can be reused.
            await response.read()
            return parsed[2], response.headers.get("Content-Type", "")
    except aiohttp.ClientError:
        return None


async def _fetch_segment_range(
    *,
    session: aiohttp.ClientSession,
    url: str,
    proxy: str | None,
    position: list[int],
    end: int,
    handle,
    downloaded: list[int],
    settings: Settings,
) -> None:
    """Скачать байты ``position[0]..end`` включительно, продвигая ``position[0]`` по мере записи.

    ``position`` разделяется с вызывающим циклом ретраев, поэтому при обрыве
    посреди потока уже записанный прогресс не теряется.
    """
    headers = {"Range": f"bytes={position[0]}-{end}", "Accept-Encoding": "identity"}
    # sock_read turns a stalled connection into ServerTimeoutError instead of hanging
    # until process_max_timeout; the caller resumes the segment from ``position[0]``.
    timeout = aiohttp.ClientTimeout(
        total=settings.process_max_timeout, sock_read=_SEGMENT_STALL_SECONDS
    )
    async with session.get(url, proxy=proxy, headers=headers, timeout=timeout) as response:
        if response.status != 206:
            raise RuntimeError(
                f"Server stopped honoring byte ranges (status {response.status})"
            )
        parsed = _parse_content_range(response.headers.get("Content-Range", ""))
        if parsed is None or parsed[0] != position[0]:
            raise RuntimeError("Server returned an unexpected byte range")
        async for chunk in response.content.iter_chunked(settings.chunk_size):
            _write_all(handle.fileno(), chunk, position[0])
            position[0] += len(chunk)
            # No await between read and write, so the shared counter needs no lock.
            downloaded[0] += len(chunk)
            if downloaded[0] > settings.max_upload_bytes:
                raise FileTooLargeError(
                    f"Downloaded {downloaded[0]} bytes exceeds upload limit "
                    f"of {settings.max_upload_bytes} bytes"
                )


async def _download_segment(
    *,
    session: aiohttp.ClientSession,
    url: str,
    proxy: str | None,
    start: int,
    end: int,
    handle,
    downloaded: list[int],
    settings: Settings,
) -> None:
    position = [start]
    attempts = 0
    while position[0] <= end:
        if attempts >= _SEGMENT_MAX_ATTEMPTS:
            raise RuntimeError(
                f"Segment {start}-{end} delivered {position[0] - start} of "
                f"{end - start + 1} bytes after {_SEGMENT_MAX_ATTEMPTS} attempts"
            )
        attempts += 1
        try:
            await _fetch_segment_range(
                session=session,
                url=url,
                proxy=proxy,
                position=position,
                end=end,
                handle=handle,
                downloaded=downloaded,
                settings=settings,
            )
        except (aiohttp.ClientError, RuntimeError) as exc:
            logger.warning(
                "Segment attempt failed, resuming | start=%s end=%s offset=%s attempt=%s error=%r",
                start,
                end,
                position[0],
                attempts,
                exc,
            )


async def _progress_monitor(
    *,
    status_message: Message,
    file_name: str,
    downloaded: list[int],
    total: int,
    started_at: float,
    logged_milestones: set[int],
) -> None:
    while True:
        await asyncio.sleep(_PROGRESS_INTERVAL_SECONDS)
        current = downloaded[0]
        milestone = _progress_milestone(current, total)
        if milestone and milestone not in logged_milestones:
            logged_milestones.add(milestone)
            logger.info(
                "Direct download progress | file=%s progress=%s%% downloaded=%s total=%s",
                file_name,
                milestone,
                current,
                total,
            )
        try:
            await status_message.edit_text(
                format_download_progress(
                    file_name=file_name,
                    downloaded=current,
                    total=total,
                    started_at=started_at,
                )
            )
        except Exception:  # pylint: disable=broad-exception-caught
            # Progress reporting must never fail the download.
            pass


async def _report_completion(
    *,
    status_message: Message,
    file_name: str,
    total: int,
    started: float,
    logged_milestones: set[int],
) -> None:
    if 100 not in logged_milestones:
        logged_milestones.add(100)
        logger.info(
            "Direct download progress | file=%s progress=100%% downloaded=%s total=%s",
            file_name,
            total,
            total,
        )
    try:
        await status_message.edit_text(
            format_download_progress(
                file_name=file_name,
                downloaded=total,
                total=total,
                started_at=started,
            )
        )
    except Exception:  # pylint: disable=broad-exception-caught
        # Progress reporting must never fail a completed download.
        pass


async def _run_segments(
    *,
    tasks: list[asyncio.Task[None]],
    monitor: asyncio.Task[None],
    handle,
) -> BaseException | None:
    """Await ``tasks``; on failure cancel and drain them, then stop ``monitor``."""
    failure: BaseException | None = None
    try:
        await asyncio.gather(*tasks)
    except BaseException as exc:
        failure = exc
    finally:
        if failure is not None:
            for task in tasks:
                task.cancel()
        monitor.cancel()
        try:
            await asyncio.gather(*tasks, monitor, return_exceptions=True)
        except BaseException:
            # Outer cancellation delivered here; the handle must still be closed.
            pass
        handle.close()
    return failure


async def _download_parallel(
    *,
    session: aiohttp.ClientSession,
    url: str,
    proxy: str | None,
    total: int,
    destination: Path,
    file_name: str,
    status_message: Message,
    settings: Settings,
    started: float,
) -> tuple[Path, str] | None:
    """Download ``total`` bytes over parallel range requests; ``None`` means retry single stream."""
    ranges = _segment_ranges(total, settings.download_threads)
    logger.info(
        "Parallel direct download | file=%s total=%s segments=%s threads=%s",
        file_name,
        total,
        len(ranges),
        settings.download_threads,
    )

    downloaded = [0]
    logged_milestones: set[int] = set()
    monitor = asyncio.create_task(
        _progress_monitor(
            status_message=status_message,
            file_name=file_name,
            downloaded=downloaded,
            total=total,
            started_at=started,
            logged_milestones=logged_milestones,
        )
    )
    handle = destination.open("wb")
    os.ftruncate(handle.fileno(), total)
    tasks = [
        asyncio.create_task(
            _download_segment(
                session=session,
                url=url,
                proxy=proxy,
                start=start,
                end=end,
                handle=handle,
                downloaded=downloaded,
                settings=settings,
            )
        )
        for start, end in ranges
    ]
    failure = await _run_segments(tasks=tasks, monitor=monitor, handle=handle)

    if failure is not None:
        if isinstance(failure, (asyncio.CancelledError, FileTooLargeError)):
            raise failure
        logger.warning(
            "Parallel download failed, retrying single stream | file=%s error=%r",
            file_name,
            failure,
        )
        destination.unlink(missing_ok=True)
        return None
    if downloaded[0] != total:
        logger.warning(
            "Parallel download incomplete, retrying single stream | file=%s expected=%s actual=%s",
            file_name,
            total,
            downloaded[0],
        )
        destination.unlink(missing_ok=True)
        return None

    await _report_completion(
        status_message=status_message,
        file_name=file_name,
        total=total,
        started=started,
        logged_milestones=logged_milestones,
    )
    return destination, file_name


async def _download_single(
    *,
    session: aiohttp.ClientSession,
    url: str,
    proxy: str | None,
    work_dir: Path,
    file_name: str,
    option: DownloadOption,
    suggested_ext: str | None,
    status_message: Message,
    settings: Settings,
    started: float,
) -> tuple[Path, str]:
    async with session.get(url, proxy=proxy) as response:
        response.raise_for_status()
        total = int(response.headers.get("Content-Length", "0") or "0")
        if total > settings.max_upload_bytes:
            raise FileTooLargeError(
                f"{total} bytes exceeds upload limit of {settings.max_upload_bytes} bytes"
            )
        content_type = response.headers.get("Content-Type", "")
        _reject_html(content_type)

        file_name, destination = _resolve_target(
            work_dir, file_name, option, suggested_ext, content_type
        )

        downloaded = 0
        last_update = 0.0
        logged_milestones: set[int] = set()

        with destination.open("wb") as handle:
            async for chunk in response.content.iter_chunked(settings.chunk_size):
                handle.write(chunk)
                downloaded += len(chunk)
                if downloaded > settings.max_upload_bytes:
                    raise FileTooLargeError(
                        f"Downloaded {downloaded} bytes exceeds upload limit "
                        f"of {settings.max_upload_bytes} bytes"
                    )
                now = time.time()
                milestone = _progress_milestone(downloaded, total)
                if milestone and milestone not in logged_milestones:
                    logged_milestones.add(milestone)
                    logger.info(
                        "Direct download progress | file=%s progress=%s%% downloaded=%s total=%s",
                        file_name,
                        milestone,
                        downloaded,
                        total,
                    )
                if total and (now - last_update >= 2 or downloaded >= total):
                    await status_message.edit_text(
                        format_download_progress(
                            file_name=file_name,
                            downloaded=downloaded,
                            total=total,
                            started_at=started,
                        )
                    )
                    last_update = now
    return destination, file_name


async def download_direct_file(
    *,
    status_message: Message,
    parsed_input: ParsedInput,
    option: DownloadOption,
    settings: Settings,
    work_dir: Path,
    suggested_ext: str | None = None,
) -> DownloadArtifact:
    work_dir.mkdir(parents=True, exist_ok=True)
    file_name = parsed_input.custom_file_name or _filename_from_url(
        parsed_input.source_url
    )
    logger.info(
        "Direct download starting | source=%s send_type=%s work_dir=%s",
        safe_url_label(parsed_input.source_url),
        option.send_type,
        work_dir,
    )

    timeout = aiohttp.ClientTimeout(total=settings.process_max_timeout)
    connector = aiohttp.TCPConnector(ssl=None if settings.verify_ssl else False)
    started = time.time()

    async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
        url = parsed_input.source_url
        proxy = settings.http_proxy or None
        downloaded: tuple[Path, str] | None = None

        if settings.download_threads > 1:
            probe = await _probe_range(session, url, proxy)
            if probe is not None:
                total, content_type = probe
                if total > settings.max_upload_bytes:
                    raise FileTooLargeError(
                        f"{total} bytes exceeds upload limit of {settings.max_upload_bytes} bytes"
                    )
                _reject_html(content_type)
                resolved_name, destination = _resolve_target(
                    work_dir, file_name, option, suggested_ext, content_type
                )
                downloaded = await _download_parallel(
                    session=session,
                    url=url,
                    proxy=proxy,
                    total=total,
                    destination=destination,
                    file_name=resolved_name,
                    status_message=status_message,
                    settings=settings,
                    started=started,
                )

        if downloaded is None:
            downloaded = await _download_single(
                session=session,
                url=url,
                proxy=proxy,
                work_dir=work_dir,
                file_name=file_name,
                option=option,
                suggested_ext=suggested_ext,
                status_message=status_message,
                settings=settings,
                started=started,
            )

    destination, file_name = downloaded
    caption = parsed_input.custom_file_name or file_name
    logger.info(
        "Direct download complete | file=%s bytes=%s destination=%s elapsed=%.1fs",
        file_name,
        destination.stat().st_size if destination.exists() else 0,
        destination,
        time.time() - started,
    )
    return DownloadArtifact(
        path=destination,
        file_name=file_name,
        send_type=option.send_type,
        caption=caption,
    )