from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any

from services.json_store import JsonStore
from utils.models import CachedMedia

logger = logging.getLogger(__name__)


class MediaCache:
    """Persistent source-URL -> sent file_id cache.

    Telegram keeps uploaded media on its servers and lets bots resend them
    by file_id; caching them makes repeat requests instant (no re-download).
    The store only holds mappings; sending them back is delivery's job.
    """

    def __init__(self, cache_file: Path, max_entries: int = 200) -> None:
        self._cache_file = cache_file.resolve()
        self._store = JsonStore(self._cache_file, strict=False)
        self._lock = asyncio.Lock()
        self._max_entries = max_entries
        entries = self._store.load().get("entries")
        self._entries: dict[str, dict[str, Any]] = (
            entries if isinstance(entries, dict) else {}
        )

    def get(self, source_url: str) -> list[CachedMedia] | None:
        entry = self._entries.get(source_url)
        if entry is None:
            return None
        media = entry.get("media")
        if not isinstance(media, list):
            return None
        try:
            return [CachedMedia.from_dict(item) for item in media]
        except (TypeError, KeyError, ValueError) as exc:
            logger.warning(
                "Media cache entry corrupt | source=%s error=%s", source_url, exc
            )
            return None

    async def record(self, source_url: str, media: list[CachedMedia]) -> None:
        async with self._lock:
            self._entries[source_url] = {
                "updated_at": time.time(),
                "media": [item.to_dict() for item in media],
            }
            if len(self._entries) > self._max_entries:
                evict = sorted(
                    self._entries.items(), key=lambda item: item[1]["updated_at"]
                )[: len(self._entries) - self._max_entries]
                for url, _ in evict:
                    del self._entries[url]
                logger.info(
                    "Media cache evicted oldest entries | count=%s", len(evict)
                )
            await self._persist()

    async def remove(self, source_url: str) -> None:
        async with self._lock:
            if self._entries.pop(source_url, None) is not None:
                await self._persist()

    async def _persist(self) -> None:
        payload = {"version": 1, "entries": self._entries}
        try:
            await asyncio.to_thread(self._store.save, payload)
        except OSError as exc:
            logger.warning(
                "Media cache persist failed | file=%s error=%s",
                self._cache_file,
                exc,
            )