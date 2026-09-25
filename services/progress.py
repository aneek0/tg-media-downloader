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

    def __init__(
        self,
        edit_text: Callable[[str], Awaitable[None]],
        file_name: str,
        min_interval: float = 2.5,
    ) -> None:
        self._edit_text = edit_text
        self._file_name = file_name
        self._min_interval = min_interval
        self._last_update = 0.0
        self._last_text: str | None = None

    async def feed_line(self, line: str) -> None:
        match = self._LINE_RE.search(line)
        if not match:
            return
        now = time.monotonic()
        if now - self._last_update < self._min_interval:
            return
        self._last_update = now
        percent, total, speed, eta = match.groups()
        text = (
            f"Downloading <b>{self._file_name}</b>\n\n"
            f"{percent}% of {total}"
            + (f"\nSpeed: {speed}" if speed else "")
            + (f"\nETA: {eta}" if eta else "")
        )
        if text == self._last_text:
            return
        self._last_text = text
        try:
            await self._edit_text(text)
        except Exception:  # pylint: disable=broad-exception-caught
            pass
