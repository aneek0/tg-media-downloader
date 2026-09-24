from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from config import Settings


def make_settings(tmp_path: Path) -> Settings:
    return Settings(
        bot_token="token",
        owner_id=99,
        auth_users={99},
        download_location=tmp_path / "downloads",
        chunk_size=1024,
        http_proxy="",
        process_max_timeout=120,
        auto_best_quality=False,
        max_video_height=1080,
        twitter_cookies="",
        telegram_api_url="",
        telegram_proxy="",
    )


def make_message():
    user = SimpleNamespace(id=99, first_name="Kalan")
    chat = SimpleNamespace(id=500, type="private")
    return SimpleNamespace(
        from_user=user,
        chat=chat,
        text=None,
        entities=None,
        answer=AsyncMock(),
        reply=AsyncMock(),
        answer_photo=AsyncMock(),
        reply_media_group=AsyncMock(),
        reply_photo=AsyncMock(),
    )


