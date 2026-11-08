"""Catalogue text search over installed mornye extensions (docs/adr/0003).

One provider instance per operation: ``customSearch`` normalizes results into
provider-independent records, ``getArtist`` builds an artist catalogue.  The
public boundary never exposes JS/extension payload shapes.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import asdict, dataclass, field, fields
from urllib.parse import urlparse

import aiohttp


class CatalogueNetworkError(RuntimeError):
    """A catalogue provider could not be reached; the search may be retried."""


#: Transport failures a provider may surface mid-call; safe to retry later.
_TRANSPORT_ERRORS = (aiohttp.ClientError, asyncio.TimeoutError, OSError)


def _provider_error(provider: str, action: str, exc: Exception) -> RuntimeError:
    kind = getattr(exc, "kind", None)
    is_network = isinstance(exc, _TRANSPORT_ERRORS) or getattr(kind, "name", "") in {
        "NETWORK_ERROR",
        "UNAVAILABLE",
    }
    message = f"{provider} {action}: {exc}"
    return CatalogueNetworkError(message) if is_network else RuntimeError(message)


_KINDS: tuple[str, ...] = ("track", "album", "artist", "playlist")

_PREFIXES = {
    ".trk": "track",
    ".a": "album",
    ".art": "artist",
    ".pla": "playlist",
}

#: Which ``customSearch`` filter each provider expects per requested kind;
#: ``None`` is the global (mixed) search all supported extensions understand.
_FILTERS: dict[str, dict[str | None, str | None]] = {
    "deezer": {
        None: None,
        "track": "track",
        "album": "album",
        "artist": "artist",
        "playlist": "playlist",
    },
    "spotify-web": {
        None: None,
        "track": "tracks",
        "album": "albums",
        "artist": "artists",
        "playlist": "playlists",
    },
    "tidal-web": {
        None: None,
        "track": "track",
        "album": "album",
        "artist": "artist",
        "playlist": "playlist",
    },
}


@dataclass(slots=True)
class SearchItem:  # pylint: disable=too-many-instance-attributes
    """Provider-independent catalogue record."""

    provider: str
    kind: str
    item_id: str
    title: str
    artists: str = ""
    album: str = ""
    cover_url: str = ""
    external_url: str = ""
    duration_ms: int | None = None
    release_date: str = ""
    album_type: str = ""
    explicit: bool = False
    total_tracks: int = 0
    listeners: int = 0

    def to_dict(self) -> dict:
        """JSON-ready payload for persisting browse state."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> SearchItem:
        """Rebuild an item from persisted data, ignoring unknown keys."""
        allowed = {item.name for item in fields(cls)}
        return cls(**{key: value for key, value in data.items() if key in allowed})


@dataclass(slots=True)
class SearchResults:
    """Normalized search hits in provider order."""

    items: list[SearchItem] = field(default_factory=list)

    def by_kind(self, kind: str) -> list[SearchItem]:
        return [item for item in self.items if item.kind == kind]


@dataclass(slots=True)
class ArtistCatalogue:
    """Artist card data: the artist plus current albums and top tracks."""

    artist: SearchItem
    top_tracks: list[SearchItem] = field(default_factory=list)
    albums: list[SearchItem] = field(default_factory=list)


@dataclass(slots=True)
class SearchQuery:
    """Bare text searches globally; ``.trk``/``.a``/``.art``/``.pla`` filter."""

    text: str
    kind: str | None = None

    @classmethod
    def parse(cls, raw: str) -> SearchQuery:
        text = (raw or "").strip()
        parts = text.split(maxsplit=1)
        if parts and parts[0] in _PREFIXES:
            return cls(parts[1] if len(parts) > 1 else "", _PREFIXES[parts[0]])
        return cls(text)

def artist_source(url: str) -> tuple[str, str] | None:
    """Return provider/id for a public artist URL, or None for other URLs."""
    parsed = urlparse(url)
    parts = [part for part in parsed.path.split("/") if part]
    try:
        index = parts.index("artist")
        artist_id = parts[index + 1]
    except (ValueError, IndexError):
        return None
    host = (parsed.hostname or "").lower()
    if host == "deezer.com" or host.endswith(".deezer.com"):
        return "deezer", f"deezer:{artist_id}"
    if host == "open.spotify.com":
        return "spotify-web", artist_id
    if host == "tidal.com" or host.endswith(".tidal.com"):
        return "tidal-web", artist_id
    return None


def _open_provider(provider: str):
    """Fresh extension client for one operation; caller must close it."""
    from mornye.extensions.provider import JSExtensionProvider

    return JSExtensionProvider(ext_id=provider)


def _as_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _artists(value) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        value = [value]
    if isinstance(value, list):
        names = []
        for entry in value:
            name = entry.get("name") if isinstance(entry, dict) else entry
            if name:
                names.append(str(name))
        return ", ".join(names)
    return str(value) if value else ""


def _cover_url(raw: dict) -> str:
    value = raw.get("cover_url") or raw.get("images") or raw.get("image_url") or ""
    if isinstance(value, str):
        return value
    if isinstance(value, list) and value:
        first = value[0]
        if isinstance(first, str):
            return first
        if isinstance(first, dict):
            return str(first.get("url") or "")
    return ""


def _fallback_url(provider: str, kind: str, item_id: str) -> str:
    if not item_id:
        return ""
    if provider == "spotify-web":
        return f"https://open.spotify.com/{kind}/{item_id}"
    if provider == "tidal-web":
        return f"https://www.tidal.com/{kind}/{item_id.split(':')[-1]}"
    return f"https://www.deezer.com/{kind}/{item_id.removeprefix('deezer:')}"


def _external_url(raw: dict, provider: str, kind: str, item_id: str) -> str:
    value = raw.get("external_urls")
    if isinstance(value, str) and value:
        return value
    if isinstance(value, dict):
        for key in (provider, "deezer", "spotify", "spotify-web"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate:
                return candidate
        for candidate in value.values():
            if isinstance(candidate, str) and candidate:
                return candidate
    return _fallback_url(provider, kind, item_id)


def _item(raw: dict, provider: str, fallback_kind: str | None) -> SearchItem | None:
    if not isinstance(raw, dict):
        return None
    kind = raw.get("item_type") or fallback_kind
    if kind not in _KINDS:
        return None
    item_id = str(raw.get("id") or "")
    if not item_id:
        return None
    album = raw.get("album") if isinstance(raw.get("album"), dict) else {}
    duration_ms = _as_int(raw.get("duration_ms"), 0)
    if not duration_ms:
        duration_ms = _as_int(raw.get("duration"), 0) * 1000
    cover_url = _cover_url(raw) or str(album.get("cover_xl") or album.get("cover") or "")
    return SearchItem(
        provider=provider,
        kind=kind,
        item_id=item_id,
        title=str(raw.get("name") or raw.get("title") or ""),
        artists=_artists(raw.get("artists") or raw.get("artist") or raw.get("contributors")),
        album=str(raw.get("album_name") or album.get("title") or ""),
        cover_url=cover_url,
        external_url=str(raw.get("link") or "")
        or _external_url(raw, provider, kind, item_id),
        duration_ms=duration_ms or None,
        release_date=str(raw.get("release_date") or ""),
        album_type=str(raw.get("album_type") or ""),
        explicit=bool(raw.get("explicit") or raw.get("explicit_lyrics")),
        total_tracks=_as_int(raw.get("total_tracks") or raw.get("nb_tracks"), 0),
        listeners=_as_int(raw.get("listeners") or raw.get("nb_fan"), 0),
    )


def _items(value, provider: str, fallback_kind: str | None) -> list[SearchItem]:
    if not isinstance(value, list):
        return []
    normalized = [_item(entry, provider, fallback_kind) for entry in value]
    return [item for item in normalized if item is not None]


class CatalogueSearch:
    """Search one catalogue extension per operation (docs/adr/0003)."""

    def __init__(
        self,
        provider: str = "deezer",
        providers: tuple[str, ...] = ("deezer", "spotify-web", "tidal-web"),
    ) -> None:
        self.providers = tuple(providers)
        self.provider = provider
        self._require_provider(provider)

    async def search(
        self,
        query: SearchQuery,
        per_kind: int = 8,
        provider: str | None = None,
    ) -> SearchResults:
        """Run ``customSearch`` and normalize hits into domain records."""
        active = provider if provider is not None else self.provider
        self._require_provider(active)
        mapped = self._filter(active, query.kind)
        raw = await self._invoke(
            active, "customSearch", query.text, {"filter": mapped, "limit": per_kind}
        )
        if not isinstance(raw, list):
            raise RuntimeError(
                f"{active} customSearch returned malformed payload: "
                f"{type(raw).__name__}"
            )
        return SearchResults(_items(raw, active, query.kind))

    async def artist(self, provider: str, artist_id: str) -> ArtistCatalogue:
        """Fetch ``getArtist`` and normalize artist, albums and top tracks."""
        self._require_provider(provider)
        raw = await self._invoke(provider, "getArtist", artist_id)
        if not isinstance(raw, dict) or not raw:
            raise RuntimeError(f"{provider} artist {artist_id}: empty payload")
        payload = raw.get("artist")
        if not isinstance(payload, dict):
            payload = raw
        artist = _item(payload, provider, "artist")
        if artist is None:
            raise RuntimeError(f"{provider} artist {artist_id}: malformed payload")
        top_tracks = _items(payload.get("top_tracks"), provider, "track")
        albums = _items(payload.get("albums"), provider, "album")[:50]
        if provider == "deezer":
            try:
                direct_top = await self._deezer_top(artist_id)
                known = {item.item_id.removeprefix("deezer:") for item in direct_top}
                top_tracks = direct_top + [
                    item
                    for item in top_tracks
                    if item.item_id.removeprefix("deezer:") not in known
                ]
                if len(top_tracks) < 30 and albums:
                    known = {item.item_id.removeprefix("deezer:") for item in top_tracks}
                    top_tracks.extend(
                        await self._deezer_discography_tracks(
                            albums,
                            exclude=known,
                            limit=30 - len(top_tracks),
                        )
                    )
            except _TRANSPORT_ERRORS as exc:
                raise _provider_error(provider, "getArtist top tracks", exc) from exc
        return ArtistCatalogue(
            artist=artist,
            top_tracks=top_tracks[:30],
            albums=albums,
        )

    @staticmethod
    async def _deezer_top(artist_id: str) -> list[SearchItem]:
        """Deezer's extension caps top tracks at 20; its public API provides 30."""
        numeric_id = artist_id.removeprefix("deezer:")
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
            async with session.get(
                f"https://api.deezer.com/artist/{numeric_id}/top", params={"limit": 30}
            ) as response:
                response.raise_for_status()
                payload = await response.json()
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list):
            raise RuntimeError(f"deezer artist {artist_id}: malformed top tracks")
        return _items(data, "deezer", "track")

    @staticmethod
    async def _deezer_discography_tracks(
        albums: list[SearchItem], *, exclude: set[str], limit: int
    ) -> list[SearchItem]:
        """Fill missing TOP slots with unique tracks from the artist's albums."""
        tracks = []
        known = set(exclude)
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
            for album in albums:
                album_id = album.item_id.removeprefix("deezer:")
                async with session.get(
                    f"https://api.deezer.com/album/{album_id}/tracks",
                    params={"limit": 100},
                ) as response:
                    response.raise_for_status()
                    payload = await response.json()
                data = payload.get("data") if isinstance(payload, dict) else None
                if not isinstance(data, list):
                    raise RuntimeError(f"deezer album {album_id}: malformed tracks")
                for track in _items(data, "deezer", "track"):
                    track_id = track.item_id.removeprefix("deezer:")
                    if track_id in known:
                        continue
                    known.add(track_id)
                    tracks.append(track)
                    if len(tracks) >= limit:
                        return tracks
        return tracks

    async def _invoke(self, provider: str, method: str, *args) -> object:
        try:
            client = await asyncio.to_thread(_open_provider, provider)
        except Exception as exc:
            raise _provider_error(provider, "provider unavailable", exc) from exc
        try:
            return await asyncio.to_thread(client._call, method, *args)
        except Exception as exc:
            raise _provider_error(provider, f"{method} failed", exc) from exc
        finally:
            with contextlib.suppress(Exception):
                client.close()

    def _require_provider(self, provider: str) -> None:
        if provider not in self.providers:
            raise ValueError(f"unsupported catalogue provider: {provider!r}")

    @staticmethod
    def _filter(provider: str, kind: str | None) -> str | None:
        if kind is not None and kind not in _KINDS:
            raise ValueError(f"unknown search kind: {kind!r}")
        return _FILTERS[provider][kind]
