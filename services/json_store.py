"""One JSON document on disk, read and written in one place.

Every persisted store used to carry its own atomic write. The media cache's
copy never created the parent directory, so a missing folder turned a cache
write into a swallowed warning; the request store had no atomic write at all
while inline tasks read it concurrently. Loads stay policy here: a strict
store raises on a broken file, a tolerant one starts empty.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class JsonStore:
    """One dict-shaped JSON document: tolerant or strict load, atomic save."""

    def __init__(self, path: Path, *, strict: bool = True) -> None:
        self.path = path.resolve()
        self._strict = strict

    def load(self) -> dict[str, Any]:
        """The parsed document; ``{}`` when absent or (tolerantly) unreadable."""
        if not self.path.exists():
            return {}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("top-level value is not an object")
            return payload
        except (OSError, ValueError) as exc:
            if self._strict:
                raise RuntimeError(f"{self.path} is unreadable: {exc}") from exc
            logger.warning(
                "Store unreadable, starting empty | file=%s error=%s",
                self.path,
                exc,
            )
            return {}

    def save(self, payload: Any) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.path.with_name(f"{self.path.name}.tmp")
        temp_path.write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )
        os.replace(temp_path, self.path)