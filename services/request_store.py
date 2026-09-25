from __future__ import annotations

import json
import logging
import shutil
import time
import uuid
from pathlib import Path

from utils.models import StoredRequest

logger = logging.getLogger(__name__)


class RequestStore:
    def __init__(self, requests_dir: Path, work_root: Path) -> None:
        self.requests_dir = requests_dir.resolve()
        self.work_root = work_root.resolve()
        self.requests_dir.mkdir(parents=True, exist_ok=True)
        self.work_root.mkdir(parents=True, exist_ok=True)

    def create_token(self) -> str:
        return uuid.uuid4().hex[:10]

    def _request_path(self, token: str) -> Path:
        return self.requests_dir / f"{token}.json"

    def work_directory(self, token: str) -> Path:
        path = self.work_root / token
        path.mkdir(parents=True, exist_ok=True)
        return path

    def save(self, stored_request: StoredRequest) -> None:
        self._request_path(stored_request.token).write_text(
            json.dumps(stored_request.to_dict()),
            encoding="utf-8",
        )

    def load(self, token: str) -> StoredRequest | None:
        path = self._request_path(token)
        if not path.exists():
            return None
        return StoredRequest.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def delete(self, token: str) -> None:
        path = self._request_path(token)
        if path.exists():
            path.unlink()
        work_dir = self.work_root / token
        if work_dir.exists():
            shutil.rmtree(work_dir, ignore_errors=True)

    def sweep_stale(self, max_age_seconds: float = 24 * 3600) -> int:
        """Delete request JSONs older than max_age (and their work dirs).

        Runs once at startup, before polling starts, so plain sync I/O is
        fine here. Returns the number of requests removed.
        """
        removed = 0
        if not self.requests_dir.is_dir():
            return removed
        now = time.time()
        for path in self.requests_dir.glob("*.json"):
            try:
                age = now - path.stat().st_mtime
            except OSError:
                logger.warning("Skipping unreadable request file | path=%s", path)
                continue
            if age <= max_age_seconds:
                continue
            token = path.stem
            work_dir = self.work_root / token
            if work_dir.is_dir():
                shutil.rmtree(work_dir, ignore_errors=True)
            try:
                path.unlink()
            except OSError as exc:
                logger.warning(
                    "Failed to delete stale request | token=%s error=%s", token, exc
                )
                continue
            removed += 1
        if removed:
            logger.info(
                "Swept stale requests | count=%s max_age_seconds=%s",
                removed,
                max_age_seconds,
            )
        return removed
