"""The status surface: the single channel download flows use to talk to
the user while work is in progress.

A :class:`StatusSurface` wraps an edit callable, so the same downloader code
drives a chat status message and an inline-mode message. Rate limiting,
line parsing and error swallowing live here; the text formatters stay pure
in :mod:`services.progress`.
"""
from __future__ import annotations

import logging
import re
import time
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

from aiogram import Bot
from aiogram.types import Message

from services.progress import render_progress
from utils import text

logger = logging.getLogger(__name__)

EditCallable = Callable[..., Awaitable[None]]


class StatusSurface:
    """Rate-limited status reporter backed by one editable Telegram message.

    ``edit`` raises Telegram errors so callers can react; progress updates
    and :meth:`fail` swallow them (a deleted message or flood limit must
    never fail the download).
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
        edit: EditCallable,
        *,
        name: str = "",
        min_interval: float = 2.5,
        muted: bool = False,
    ) -> None:
        self._edit = edit
        self._name = name
        self._min_interval = min_interval
        self._muted = muted
        self._last_update: float | None = None
        self._last_text: str | None = None

    async def edit(self, message: str, **kwargs: Any) -> None:
        """Replace the status text; Telegram errors propagate. A muted
        surface (its message already holds media) drops the edit: the media
        stays on screen. :meth:`fail` is never muted - an error must show."""
        if self._muted:
            return
        await self._edit(message, **kwargs)

    async def start_download(self, name: str) -> None:
        self._name = name
        await self.edit(text.download_caption(name))

    async def uploading(self, name: str) -> None:
        await self.edit(text.upload_caption(name))

    async def done(self, *, download_seconds: int, upload_seconds: int) -> None:
        await self.edit(
            text.DONE.format(
                download_seconds=download_seconds,
                upload_seconds=upload_seconds,
            )
        )

    async def fail(self, message: str) -> None:
        """Show a failure; never raise (already inside an error boundary).
        A muted surface still reports failures - an error must reach the
        user even when the message holds media that cannot show status."""
        await self._silent_edit(message, muted_ok=True)

    async def progress(self, message: str, *, force: bool = False) -> None:
        """Show a progress frame. Throttled unless ``force`` (callers such
        as the direct downloader already pace their own updates)."""
        now = time.monotonic()
        if (
            not force
            and self._last_update is not None
            and now - self._last_update < self._min_interval
        ):
            return
        if message == self._last_text:
            return
        self._last_update = now
        self._last_text = message
        await self._silent_edit(message)

    async def feed_line(self, line: str) -> None:
        """Consume a raw yt-dlp/aria2c output line as a progress frame."""
        parsed = self._parse_progress_line(line)
        if parsed is None:
            return
        percent, total, speed, eta = parsed
        await self.progress(
            f"Downloading <b>{self._name}</b>\n\n"
            + render_progress(percent, total, speed, eta)
        )

    @classmethod
    def _parse_progress_line(
        cls, line: str
    ) -> tuple[str, str, str | None, str | None] | None:
        match = cls._LINE_RE.search(line)
        if match:
            return match.groups()
        aria2_match = cls._ARIA2_RE.search(line)
        if not aria2_match:
            return None
        total, percent, aria2_speed, eta = aria2_match.groups()
        return percent, total, f"{aria2_speed}/s" if aria2_speed else None, eta

    async def _silent_edit(self, message: str, *, muted_ok: bool = False) -> None:
        if self._muted and not muted_ok:
            return
        try:
            await self._edit(message)
        except Exception:  # pylint: disable=broad-exception-caught
            pass


def chat_status(message: Message, *, name: str = "") -> StatusSurface:
    """Status surface over a chat message the bot can edit."""

    async def edit(message_text: str, **kwargs: Any) -> None:
        await message.edit_text(message_text, **kwargs)

    return StatusSurface(edit, name=name)


def inline_status(
    bot: Bot,
    inline_message_id: str,
    *,
    name: str = "",
    media_message: bool = False,
) -> StatusSurface:
    """Status surface over an inline-mode message (edited by id).

    ``media_message`` marks a message that already holds media (the
    big-video poster flow). A media message cannot carry status text, so
    every edit becomes a no-op and the poster stays on screen until the
    caller swaps it with ``edit_message_media``."""

    async def edit(message_text: str, **kwargs: Any) -> None:
        await bot.edit_message_text(
            inline_message_id=inline_message_id, text=message_text, **kwargs
        )

    return StatusSurface(edit, name=name, muted=media_message)

def inline_caption_status(
    bot: Bot, inline_message_id: str, *, name: str = ""
) -> StatusSurface:
    """Status surface for an Artist Card already containing a photo."""

    async def edit(message_text: str, **kwargs: Any) -> None:
        await bot.edit_message_caption(
            inline_message_id=inline_message_id, caption=message_text, **kwargs
        )

    return StatusSurface(edit, name=name)


def elapsed_seconds(started_at: datetime) -> int:
    return int((datetime.now() - started_at).total_seconds())