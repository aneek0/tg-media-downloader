from __future__ import annotations

import logging
import re
import shutil
import time
import uuid
from collections.abc import Generator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from services.json_store import JsonStore
from utils.models import (
    CataloguePayload,
    DirectPayload,
    DownloadOption,
    GalleryPayload,
    InlineGalleryPayload,
    InlineMediaPayload,
    MusicPayload,
    ParsedInput,
    PixivProfilePayload,
    ProbePayload,
    RequestPayload,
    RequestType,
    StoredRequest,
)

logger = logging.getLogger(__name__)

#: Every Request type is persisted by exactly one payload shape, so a caller
#: cannot store an artist card where a probe result is decoded later.
_PAYLOAD_TYPES: dict[RequestType, type] = {
    RequestType.GALLERY_MEDIA: GalleryPayload,
    RequestType.MORNYE: MusicPayload,
    RequestType.YOUTUBE_QUICK: ProbePayload,
    RequestType.YTDLP_SELECTION: ProbePayload,
    RequestType.YTDLP_AUTO: ProbePayload,
    RequestType.DIRECT_DOWNLOAD: DirectPayload,
    RequestType.INLINE_MEDIA: InlineMediaPayload,
    RequestType.INLINE_GALLERY: InlineGalleryPayload,
    RequestType.CATALOGUE_BROWSE: CataloguePayload,
    RequestType.PIXIV_PROFILE: PixivProfilePayload,
}


def _checked_payload(
    request_type: RequestType, payload: RequestPayload
) -> dict[str, Any]:
    expected = _PAYLOAD_TYPES[request_type]
    if not isinstance(payload, expected):
        raise ValueError(
            f"{request_type} needs {expected.__name__}, got {type(payload).__name__}"
        )
    return payload.to_info()


class RequestStore:
    """Token -> Request on disk, plus the lifecycle every flow must run.

    A Request is opened with the payload shape its type requires and closed on
    every terminal path; the work directory belongs to the token.
    """

    def __init__(self, requests_dir: Path, work_root: Path) -> None:
        self.requests_dir = requests_dir.resolve()
        self.work_root = work_root.resolve()
        self.requests_dir.mkdir(parents=True, exist_ok=True)
        self.work_root.mkdir(parents=True, exist_ok=True)

    def open(
        self,
        request_type: RequestType,
        parsed_input: ParsedInput,
        *,
        payload: RequestPayload,
        options: Sequence[DownloadOption] = (),
    ) -> StoredRequest:
        """Persist one new Request; the token is the returned handle."""
        while True:
            token = self.create_token()
            # Regenerate on collision: save() overwrites blindly, so a reused
            # token would deliver one user's media under another user's tap.
            if not self._request_path(token).exists():
                break
        stored = StoredRequest(
            token=token,
            request_type=request_type,
            parsed_input=parsed_input,
            options=list(options),
            info=_checked_payload(request_type, payload),
        )
        self.save(stored)
        return stored

    def update_payload(
        self, stored: StoredRequest, payload: RequestPayload
    ) -> None:
        """Replace a persisted payload under the same token.

        Artist Cards and Pixiv profile browsers keep one token across pages and
        refreshes; their buttons already point at it, so a new Request would
        orphan them.
        """
        stored.info = _checked_payload(RequestType(stored.request_type), payload)
        self.save(stored)

    @contextmanager
    def lifecycle(self, token: str) -> Generator[str, None, None]:
        """Guarantee token and work-directory cleanup on every terminal path."""
        try:
            yield token
        finally:
            self.delete(token)

    def create_token(self) -> str:
        return uuid.uuid4().hex[:10]

    def _valid_token(self, token: str) -> bool:
        # callback_data is client-controlled: without this guard a forged
        # "icxl:../../.." token would rmtree outside the work root.
        return re.fullmatch(r"[0-9a-f]{10}", token or "") is not None

    def _request_path(self, token: str) -> Path:
        if not self._valid_token(token):
            raise ValueError("malformed request token")
        return self.requests_dir / f"{token}.json"

    def work_directory(self, token: str) -> Path:
        if not self._valid_token(token):
            raise ValueError("malformed request token")
        path = self.work_root / token
        path.mkdir(parents=True, exist_ok=True)
        return path

    def save(self, stored_request: StoredRequest) -> None:
        JsonStore(self._request_path(stored_request.token), strict=False).save(
            stored_request.to_dict()
        )

    def load(self, token: str) -> StoredRequest | None:
        path = self._request_path(token)
        if not path.exists():
            return None
        data = JsonStore(path, strict=False).load()
        # A present-but-corrupt JSON must not KeyError the callback handler.
        return StoredRequest.from_dict(data) if data else None

    def delete(self, token: str) -> None:
        if not self._valid_token(token):
            return  # forged callback data: nothing of ours to clean up
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