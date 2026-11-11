from __future__ import annotations

from dataclasses import asdict, dataclass
from dataclasses import fields as dataclass_fields
from pathlib import Path
from typing import Any

from services.json_store import JsonStore
from utils.models import SEARCH_PROVIDERS, validate_user_id

_VALID_AUDIO_FORMATS = frozenset({"flac", "mp3"})
_VALID_AUDIO_QUALITIES = frozenset({"lossless", "hires"})
_DEFAULT_PROVIDER = "deezer"
_DEFAULT_AUDIO_QUALITY = "lossless"
_DEFAULT_AUDIO_FORMAT = "flac"
_DEFAULT_TEXT_SEARCH = True


@dataclass
class UserPreferences:
    text_search: bool = _DEFAULT_TEXT_SEARCH
    provider: str = _DEFAULT_PROVIDER
    audio_format: str = _DEFAULT_AUDIO_FORMAT
    audio_quality: str = _DEFAULT_AUDIO_QUALITY


def _validated_prefs(
    *,
    text_search: Any,
    provider: Any,
    audio_format: Any,
    audio_quality: Any = _DEFAULT_AUDIO_QUALITY,
) -> UserPreferences:
    if not isinstance(text_search, bool):
        raise ValueError(f"text_search must be a bool, got {text_search!r}")
    if provider not in SEARCH_PROVIDERS:
        raise ValueError(
            f"unsupported provider {provider!r}; expected one of {sorted(SEARCH_PROVIDERS)}"
        )
    if audio_format not in _VALID_AUDIO_FORMATS:
        raise ValueError(
            f"unsupported audio_format {audio_format!r}; expected one of {sorted(_VALID_AUDIO_FORMATS)}"
        )
    if audio_quality not in _VALID_AUDIO_QUALITIES:
        raise ValueError(
            f"unsupported audio_quality {audio_quality!r}; expected one of {sorted(_VALID_AUDIO_QUALITIES)}"
        )
    return UserPreferences(
        text_search=text_search,
        provider=provider,
        audio_format=audio_format,
        audio_quality=audio_quality,
    )


class UserSettingsStore:
    """Per-user text-search/audio preferences persisted as JSON.

    A store file that exists but cannot be understood raises instead of being
    discarded; the atomic write is owned by :class:`JsonStore`.
    """

    def __init__(self, path: Path) -> None:
        self._path = path.resolve()
        self._store = JsonStore(self._path)
        self._entries: dict[int, UserPreferences] = self._load()

    @property
    def path(self) -> Path:
        return self._path

    def _load(self) -> dict[int, UserPreferences]:
        payload = self._store.load()
        if not payload:
            return {}
        try:
            if payload.get("version") != 1:
                raise ValueError(f"unsupported version {payload.get('version')!r}")
            unexpected = set(payload) - {"version", "entries"}
            if unexpected:
                raise ValueError(f"unexpected top-level keys {sorted(unexpected)}")
            raw_entries = payload["entries"]
            if not isinstance(raw_entries, dict):
                raise ValueError("'entries' is not an object")
            entries: dict[int, UserPreferences] = {}
            for raw_user_id, raw_record in raw_entries.items():
                user_id = validate_user_id(int(raw_user_id))
                entries[user_id] = self._record_to_prefs(raw_record)
            return entries
        except (TypeError, ValueError, KeyError) as exc:
            raise RuntimeError(
                f"User settings store at {self._path} is unreadable: {exc}"
            ) from exc

    @staticmethod
    def _record_to_prefs(record: Any) -> UserPreferences:
        if not isinstance(record, dict):
            raise ValueError("preference record is not an object")
        known = {field.name for field in dataclass_fields(UserPreferences)}
        unknown = set(record) - known
        if unknown:
            raise ValueError(f"unknown preference keys {sorted(unknown)}")
        return _validated_prefs(
            text_search=record["text_search"],
            provider=record["provider"],
            audio_format=record["audio_format"],
            audio_quality=record.get("audio_quality", _DEFAULT_AUDIO_QUALITY),
        )

    def get(
        self,
        user_id: int,
        default_provider: str = _DEFAULT_PROVIDER,
        default_audio_format: str = _DEFAULT_AUDIO_FORMAT,
        default_audio_quality: str = _DEFAULT_AUDIO_QUALITY,
    ) -> UserPreferences:
        validate_user_id(user_id)
        stored = self._entries.get(user_id)
        if stored is not None:
            return stored
        return _validated_prefs(
            text_search=_DEFAULT_TEXT_SEARCH,
            provider=default_provider,
            audio_format=default_audio_format,
            audio_quality=default_audio_quality,
        )

    def update(
        self,
        user_id: int,
        *,
        default_provider: str = _DEFAULT_PROVIDER,
        default_audio_format: str = _DEFAULT_AUDIO_FORMAT,
        default_audio_quality: str = _DEFAULT_AUDIO_QUALITY,
        **values: Any,
    ) -> UserPreferences:
        validate_user_id(user_id)
        allowed = {field.name for field in dataclass_fields(UserPreferences)}
        unknown = set(values) - allowed
        if unknown:
            raise ValueError(f"unknown preference fields {sorted(unknown)}")
        current = self.get(
            user_id,
            default_provider=default_provider,
            default_audio_format=default_audio_format,
            default_audio_quality=default_audio_quality,
        )
        merged = {
            "text_search": current.text_search,
            "provider": current.provider,
            "audio_format": current.audio_format,
            "audio_quality": current.audio_quality,
        }
        merged.update(values)
        updated = _validated_prefs(**merged)
        self._entries[user_id] = updated
        self._persist()
        return updated

    def _persist(self) -> None:
        self._store.save(
            {
                "version": 1,
                "entries": {
                    str(user_id): asdict(prefs)
                    for user_id, prefs in sorted(self._entries.items())
                },
            }
        )