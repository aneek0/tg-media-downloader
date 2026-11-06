from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, fields as dataclass_fields
from pathlib import Path
from typing import Any

_VALID_PROVIDERS = frozenset({"deezer", "spotify-web", "tidal-web"})
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


def _validate_user_id(user_id: Any) -> int:
    if isinstance(user_id, bool) or not isinstance(user_id, int):
        raise ValueError(f"user_id must be an int, got {user_id!r}")
    return user_id


def _validated_prefs(
    *,
    text_search: Any,
    provider: Any,
    audio_format: Any,
    audio_quality: Any = _DEFAULT_AUDIO_QUALITY,
) -> UserPreferences:
    if not isinstance(text_search, bool):
        raise ValueError(f"text_search must be a bool, got {text_search!r}")
    if provider not in _VALID_PROVIDERS:
        raise ValueError(
            f"unsupported provider {provider!r}; expected one of {sorted(_VALID_PROVIDERS)}"
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

    Reads and writes are synchronous; writes go through a temp file and
    ``os.replace`` so a crash never leaves a half-written store. A store file
    that exists but cannot be understood raises instead of being discarded.
    """

    def __init__(self, path: Path) -> None:
        self._path = path.resolve()
        self._entries: dict[int, UserPreferences] = self._load()

    @property
    def path(self) -> Path:
        return self._path

    def _load(self) -> dict[int, UserPreferences]:
        if not self._path.exists():
            return {}
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("top-level value is not an object")
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
                user_id = _validate_user_id(int(raw_user_id))
                entries[user_id] = self._record_to_prefs(raw_record)
            return entries
        except (OSError, TypeError, ValueError, KeyError) as exc:
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
        _validate_user_id(user_id)
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
        _validate_user_id(user_id)
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
        payload = json.dumps(
            {
                "version": 1,
                "entries": {
                    str(user_id): asdict(prefs)
                    for user_id, prefs in sorted(self._entries.items())
                },
            },
            ensure_ascii=False,
        )
        self._write_atomic(payload)

    def _write_atomic(self, payload: str) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self._path.with_name(f"{self._path.name}.tmp")
        temp_path.write_text(payload, encoding="utf-8")
        os.replace(temp_path, self._path)