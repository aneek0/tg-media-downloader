from __future__ import annotations

import math
import re
import time
from collections.abc import Awaitable, Callable


def humanbytes(size: int | float | None) -> str:
    if not size:
        return "0 B"
    power = 2**10
    units = {0: "B", 1: "KB", 2: "MB", 3: "GB", 4: "TB"}
    n = 0
    value = float(size)
    while value >= power and n < 4:
        value /= power
        n += 1
    return f"{value:.2f} {units[n]}"


def format_duration(seconds: int) -> str:
    minutes, sec = divmod(max(seconds, 0), 60)
    hours, minutes = divmod(minutes, 60)
    parts: list[str] = []
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    parts.append(f"{sec}s")
    return " ".join(parts)


def format_download_progress(
    file_name: str,
    downloaded: int,
    total: int,
    started_at: float,
) -> str:
    elapsed = max(time.time() - started_at, 1e-6)
    percentage = min(downloaded / total, 1) if total else 0
    bars = math.floor(percentage * 20)
    speed = downloaded / elapsed
    eta = int((total - downloaded) / speed) if speed and total else 0
    return (
        f"Downloading <b>{file_name}</b>\n\n"
        f"[{'#' * bars}{'.' * (20 - bars)}] {percentage * 100:.1f}%\n"
        f"{humanbytes(downloaded)} of {humanbytes(total)}\n"
        f"Speed: {humanbytes(speed)}/s\n"
        f"ETA: {format_duration(eta)}"
    )


def _render_progress(percent: str, total: str, speed: str | None, eta: str | None) -> str:
    return (
        f"{percent}% of {total}"
        + (f"\nSpeed: {speed}" if speed else "")
        + (f"\nETA: {eta}" if eta else "")
    )


class StatusProgress:
    """Rate-limited progress reporter backed by a Telegram status message.

    Accepts raw yt-dlp progress lines, parses percent/speed/eta, and edits
    the status message at most once per min_interval seconds. Silently
    swallows edit errors (message deleted, flood limits) — progress must
    never fail the download.
    """

    _LINE_RE = re.compile(
        r"\[download\]\s+([\d.]+)%\s+of\s+~?\s*([\d.]+\w+)"
        r"(?:\s+at\s+([\d.]+[kMG]i?B/s|[\d.]+B/s))?"
        r"(?:\s+ETA\s+(\d+:\d+(?::\d+)?))?"
    )
    # aria2c external downloader: "[#7c9c4f 400.5MiB/1.9GiB(20%) CN:16 DL:44.2MiB ETA:35s]"
    _ARIA2_RE = re.compile(
        r"\[#[0-9a-fA-F]+\s+[\d.]+\w+/([\d.]+\w+)\((\d+)%\)"
        r"(?:\s+CN:\d+)?(?:\s+DL:([\d.]+\w+))?(?:\s+ETA:([\dhms]+))?\]"
    )

    def __init__(
        self,
        edit_text: Callable[[str], Awaitable[None]],
        file_name: str,
        min_interval: float = 2.5,
    ) -> None:
        self._edit_text = edit_text
        self._file_name = file_name
        self._min_interval = min_interval
        self._last_update: float | None = None
        self._last_text: str | None = None

    async def feed_line(self, line: str) -> None:
        match = self._LINE_RE.search(line)
        if match:
            percent, total, speed, eta = match.groups()
        else:
            aria2_match = self._ARIA2_RE.search(line)
            if not aria2_match:
                return
            total, percent, aria2_speed, eta = aria2_match.groups()
            speed = f"{aria2_speed}/s" if aria2_speed else None
        now = time.monotonic()
        if self._last_update is not None and now - self._last_update < self._min_interval:
            return
        self._last_update = now
        text = f"Downloading <b>{self._file_name}</b>\n\n" + _render_progress(
            percent, total, speed, eta
        )
        if text == self._last_text:
            return
        self._last_text = text
        try:
            await self._edit_text(text)
        except Exception:  # pylint: disable=broad-exception-caught
            pass
