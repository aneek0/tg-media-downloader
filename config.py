from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


def _parse_int_set(raw_value: str | None) -> set[int]:
    if not raw_value:
        return set()
    values: set[int] = set()
    for chunk in raw_value.replace(",", " ").split():
        if chunk:
            values.add(int(chunk))
    return values


def _parse_chunk_size(raw_value: str | None) -> int:
    size = int(raw_value or "128")
    return size * 1024 if size < 1024 else size


def _parse_bool(raw_value: str | None, default: bool, name: str = "AUTO_BEST_QUALITY") -> bool:
    if raw_value is None or not raw_value.strip():
        return default
    value = raw_value.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise RuntimeError(f"{name} must be a boolean value, got {raw_value!r}")


def _parse_positive_int(raw_value: str | None, *, default: int) -> int:
    if raw_value is None or not raw_value.strip():
        return default
    try:
        value = int(raw_value.strip())
    except ValueError as exc:
        raise RuntimeError(
            f"Value must be a positive integer, got {raw_value!r}"
        ) from exc
    if value <= 0:
        raise RuntimeError(f"Value must be a positive integer, got {raw_value!r}")
    return value


def _parse_twitter_cookies(raw: str | None) -> str:
    if not raw:
        return ""
    return raw.strip()


@dataclass(slots=True)
class Settings:
    bot_token: str
    owner_id: int
    auth_users: set[int]
    download_location: Path
    chunk_size: int
    http_proxy: str
    process_max_timeout: int
    auto_best_quality: bool
    max_video_height: int
    request_cooldown_seconds: int = 3600
    max_upload_bytes: int = 1900 * 1024 * 1024
    verify_ssl: bool = True
    twitter_cookies: str = ""
    telegram_api_url: str = ""
    telegram_proxy: str = ""

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv()

        bot_token = os.environ.get("BOT_TOKEN", "").strip()
        owner_id = int(os.environ.get("OWNER_ID", "0").strip() or "0")
        if not bot_token:
            raise RuntimeError("BOT_TOKEN is required")
        if not owner_id:
            raise RuntimeError("OWNER_ID is required")

        auth_users = _parse_int_set(os.environ.get("AUTH_USERS"))
        auth_users.add(owner_id)

        download_location = Path(
            os.environ.get("DOWNLOAD_LOCATION", "./DOWNLOADS").strip() or "./DOWNLOADS"
        )

        max_height_raw = os.environ.get("MAX_HEIGHT", "1080").strip() or "1080"
        try:
            max_video_height = int(max_height_raw)
        except ValueError as exc:
            raise RuntimeError(
                f"MAX_HEIGHT must be a positive integer, got {max_height_raw!r}"
            ) from exc
        if max_video_height <= 0:
            raise RuntimeError("MAX_HEIGHT must be a positive integer")

        return cls(
            bot_token=bot_token,
            owner_id=owner_id,
            auth_users=auth_users,
            download_location=download_location,
            chunk_size=_parse_chunk_size(os.environ.get("CHUNK_SIZE")),
            http_proxy=os.environ.get("HTTP_PROXY", "").strip(),
            process_max_timeout=int(os.environ.get("PROCESS_MAX_TIMEOUT", "3700")),
            auto_best_quality=_parse_bool(os.environ.get("AUTO_BEST_QUALITY"), True),
            max_video_height=max_video_height,
            request_cooldown_seconds=_parse_positive_int(
                os.environ.get("REQUEST_COOLDOWN_SECONDS"), default=3600
            ),
            max_upload_bytes=_parse_positive_int(
                os.environ.get("MAX_UPLOAD_BYTES"), default=1900 * 1024 * 1024
            ),
            verify_ssl=_parse_bool(
                os.environ.get("VERIFY_SSL"), True, name="VERIFY_SSL"
            ),
            twitter_cookies=_parse_twitter_cookies(os.environ.get("TWITTER_COOKIES")),
            telegram_api_url=os.environ.get("TELEGRAM_API_URL", "").strip(),
            telegram_proxy=(
                os.environ.get("TELEGRAM_PROXY", "").strip()
                or os.environ.get("HTTP_PROXY", "").strip()
            ),
        )

    @property
    def thumbnails_dir(self) -> Path:
        return self.download_location / "thumbnails"

    @property
    def requests_dir(self) -> Path:
        return self.download_location / "requests"

    @property
    def media_cache_file(self) -> Path:
        return self.download_location / "media_cache.json"

    @property
    def work_dir(self) -> Path:
        return self.download_location / "work"

    def ensure_directories(self) -> None:
        for path in (
            self.download_location,
            self.thumbnails_dir,
            self.requests_dir,
            self.work_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)
