from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

_VALID_PROVIDERS = frozenset({"deezer", "spotify-web", "tidal-web"})


@dataclass
class Follow:
    user_id: int
    provider: str
    artist_id: str
    artist_name: str
    cover_url: str = ""
    known_album_ids: list[str] = field(default_factory=list)


def _validate_user_id(user_id: Any) -> int:
    if isinstance(user_id, bool) or not isinstance(user_id, int):
        raise ValueError(f"user_id must be an int, got {user_id!r}")
    return user_id


def _validate_provider(provider: Any) -> str:
    if provider not in _VALID_PROVIDERS:
        raise ValueError(
            f"unsupported provider {provider!r}; expected one of {sorted(_VALID_PROVIDERS)}"
        )
    return provider


def _validate_artist_id(artist_id: Any) -> str:
    if not isinstance(artist_id, str) or not artist_id:
        raise ValueError(f"artist_id must be a non-empty str, got {artist_id!r}")
    return artist_id


def _validate_album_ids(album_ids: Any) -> list[str]:
    if not isinstance(album_ids, list):
        raise ValueError(f"known_album_ids must be a list of str, got {album_ids!r}")
    if any(not isinstance(album_id, str) for album_id in album_ids):
        raise ValueError(f"known_album_ids must be a list of str, got {album_ids!r}")
    return list(album_ids)


def _validated_follow(
    *,
    user_id: Any,
    provider: Any,
    artist_id: Any,
    artist_name: Any,
    cover_url: Any = "",
    known_album_ids: Any = (),
) -> Follow:
    _validate_user_id(user_id)
    _validate_provider(provider)
    _validate_artist_id(artist_id)
    if not isinstance(artist_name, str):
        raise ValueError(f"artist_name must be a str, got {artist_name!r}")
    if not isinstance(cover_url, str):
        raise ValueError(f"cover_url must be a str, got {cover_url!r}")
    return Follow(
        user_id=user_id,
        provider=provider,
        artist_id=artist_id,
        artist_name=artist_name,
        cover_url=cover_url,
        known_album_ids=_validate_album_ids(known_album_ids),
    )


class FollowStore:
    """Telegram-user -> catalogue-artist subscriptions persisted as JSON.

    Synchronous like MediaCache reads; every mutation is written through a
    temp file and ``os.replace``. A store file that exists but cannot be
    understood raises instead of being discarded, so records are never
    silently lost.
    """

    def __init__(self, path: Path) -> None:
        self._path = path.resolve()
        self._follows: list[Follow] = self._load()

    @property
    def path(self) -> Path:
        return self._path

    def _load(self) -> list[Follow]:
        if not self._path.exists():
            return []
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("top-level value is not an object")
            if payload.get("version") != 1:
                raise ValueError(f"unsupported version {payload.get('version')!r}")
            unexpected = set(payload) - {"version", "follows"}
            if unexpected:
                raise ValueError(f"unexpected top-level keys {sorted(unexpected)}")
            raw_follows = payload["follows"]
            if not isinstance(raw_follows, list):
                raise ValueError("'follows' is not a list")
            follows: list[Follow] = []
            for raw_record in raw_follows:
                if not isinstance(raw_record, dict):
                    raise ValueError("follow record is not an object")
                follows.append(
                    _validated_follow(
                        user_id=raw_record["user_id"],
                        provider=raw_record["provider"],
                        artist_id=raw_record["artist_id"],
                        artist_name=raw_record["artist_name"],
                        cover_url=raw_record.get("cover_url", ""),
                        known_album_ids=raw_record.get("known_album_ids", []),
                    )
                )
            return follows
        except (OSError, TypeError, ValueError, KeyError) as exc:
            raise RuntimeError(
                f"Follow store at {self._path} is unreadable: {exc}"
            ) from exc

    def toggle(
        self,
        user_id: int,
        provider: str,
        artist_id: str,
        artist_name: str,
        cover_url: str = "",
        known_album_ids: list[str] | None = None,
    ) -> bool:
        """Switch the subscription for (user_id, provider, artist_id).

        Returns True when the artist is followed after the call. The first
        add saves ``known_album_ids`` as the baseline; a repeat toggle
        removes the record.
        """
        record = _validated_follow(
            user_id=user_id,
            provider=provider,
            artist_id=artist_id,
            artist_name=artist_name,
            cover_url=cover_url,
            known_album_ids=[] if known_album_ids is None else known_album_ids,
        )
        existing_index = self._index_of(user_id, provider, artist_id)
        if existing_index is not None:
            del self._follows[existing_index]
            self._persist()
            return False
        self._follows.append(record)
        self._persist()
        return True

    def list_for_user(self, user_id: int) -> list[Follow]:
        _validate_user_id(user_id)
        return [follow for follow in self._follows if follow.user_id == user_id]

    def all_artists(self) -> list[tuple[str, str]]:
        seen: set[tuple[str, str]] = set()
        unique: list[tuple[str, str]] = []
        for follow in self._follows:
            key = (follow.provider, follow.artist_id)
            if key not in seen:
                seen.add(key)
                unique.append(key)
        return unique

    def get(self, provider: str, artist_id: str) -> list[Follow]:
        _validate_provider(provider)
        _validate_artist_id(artist_id)
        return [
            follow
            for follow in self._follows
            if follow.provider == provider and follow.artist_id == artist_id
        ]

    def update_albums(
        self, provider: str, artist_id: str, known_ids: list[str]
    ) -> None:
        """Replace the saved baseline album ids for every follower of the artist."""
        _validate_provider(provider)
        _validate_artist_id(artist_id)
        album_ids = _validate_album_ids(known_ids)
        changed = False
        for index, follow in enumerate(self._follows):
            if follow.provider != provider or follow.artist_id != artist_id:
                continue
            if follow.known_album_ids == album_ids:
                continue
            self._follows[index] = _validated_follow(
                user_id=follow.user_id,
                provider=follow.provider,
                artist_id=follow.artist_id,
                artist_name=follow.artist_name,
                cover_url=follow.cover_url,
                known_album_ids=album_ids,
            )
            changed = True
        if changed:
            self._persist()

    def update_user_albums(
        self,
        user_id: int,
        provider: str,
        artist_id: str,
        known_ids: list[str],
    ) -> None:
        """Advance one subscriber only after their notification was delivered."""
        _validate_user_id(user_id)
        _validate_provider(provider)
        _validate_artist_id(artist_id)
        album_ids = _validate_album_ids(known_ids)
        index = self._index_of(user_id, provider, artist_id)
        if index is None:
            return
        follow = self._follows[index]
        self._follows[index] = _validated_follow(
            user_id=user_id,
            provider=provider,
            artist_id=artist_id,
            artist_name=follow.artist_name,
            cover_url=follow.cover_url,
            known_album_ids=album_ids,
        )
        self._persist()

    def remove(self, user_id: int, provider: str, artist_id: str) -> bool:
        _validate_user_id(user_id)
        _validate_provider(provider)
        _validate_artist_id(artist_id)
        existing_index = self._index_of(user_id, provider, artist_id)
        if existing_index is None:
            return False
        del self._follows[existing_index]
        self._persist()
        return True

    def _index_of(self, user_id: int, provider: str, artist_id: str) -> int | None:
        for index, follow in enumerate(self._follows):
            if (
                follow.user_id == user_id
                and follow.provider == provider
                and follow.artist_id == artist_id
            ):
                return index
        return None

    def _persist(self) -> None:
        payload = json.dumps(
            {
                "version": 1,
                "follows": [asdict(follow) for follow in self._follows],
            },
            ensure_ascii=False,
        )
        self._write_atomic(payload)

    def _write_atomic(self, payload: str) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self._path.with_name(f"{self._path.name}.tmp")
        temp_path.write_text(payload, encoding="utf-8")
        os.replace(temp_path, self._path)