"""The bot's session: every long-lived service, built once at startup.

Handlers receive this one object instead of a settings object plus four
stores plus an optional downloader; flows read what they need off it.
"""
from __future__ import annotations

from dataclasses import dataclass

from config import Settings
from services.cooldown import CooldownManager
from services.media_cache import MediaCache
from services.mornye import MornyeService
from services.catalogue_search import CatalogueSearch
from services.follow_store import FollowStore
from services.user_settings import UserPreferences, UserSettingsStore
from services.request_store import RequestStore
from services.thumbnail_store import ThumbnailStore

@dataclass(frozen=True, slots=True)
class Session:
    """What one bot run owns: Settings, the three stores, Cooldown and the
    mornye client. Everything a Request needs to become an Artifact and then
    a message."""

    settings: Settings
    cooldown: CooldownManager
    request_store: RequestStore
    thumbnail_store: ThumbnailStore
    media_cache: MediaCache
    mornye: MornyeService
    catalogue_search: CatalogueSearch
    user_settings: UserSettingsStore
    follows: FollowStore

    def _preference_defaults(self) -> dict[str, str]:
        return {
            "default_provider": self.settings.search_provider,
            "default_audio_format": (
                "mp3" if self.settings.mornye_transcode == "mp3" else "flac"
            ),
            "default_audio_quality": (
                "hires"
                if self.settings.mornye_quality.strip().upper().startswith("HI_RES")
                else "lossless"
            ),
        }

    def preferences(self, user_id: int) -> UserPreferences:
        """Per-user preferences; the bot's own settings seed the defaults."""
        return self.user_settings.get(user_id, **self._preference_defaults())

    def update_preferences(self, user_id: int, **values: object) -> UserPreferences:
        """Store a preference change, seeded with the same defaults reads use."""
        return self.user_settings.update(
            user_id, **self._preference_defaults(), **values
        )


def build_session(
    settings: Settings, mornye: MornyeService | None = None
) -> Session:
    """Create the session from Settings. ``mornye`` is injectable so the
    caller can warm up its extension registry before polling starts."""
    return Session(
        settings=settings,
        cooldown=CooldownManager(
            timeout_seconds=settings.request_cooldown_seconds
        ),
        request_store=RequestStore(settings.requests_dir, settings.work_dir),
        thumbnail_store=ThumbnailStore(settings.thumbnails_dir),
        media_cache=MediaCache(settings.media_cache_file),
        mornye=mornye or MornyeService(settings),
        catalogue_search=CatalogueSearch(provider=settings.search_provider),
        user_settings=UserSettingsStore(settings.user_settings_file),
        follows=FollowStore(settings.follows_file),
    )