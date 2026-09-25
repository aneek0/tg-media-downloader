import asyncio
import json
from pathlib import Path

import pytest

from services.media_cache import MediaCache
from utils.models import CachedMedia


@pytest.mark.asyncio
async def test_media_cache_roundtrip_persists(tmp_path: Path):
    cache_file = tmp_path / "media_cache.json"
    cache = MediaCache(cache_file)
    media = CachedMedia(file_id="BAAC1", send_type="video", file_name="f.mp4", caption="c")

    await cache.record("https://example.com/f.mp4", [media])

    reloaded = MediaCache(cache_file)
    got = reloaded.get("https://example.com/f.mp4")
    assert got is not None
    assert got[0].file_id == "BAAC1"
    assert got[0].send_type == "video"
    assert got[0].file_name == "f.mp4"
    assert got[0].caption == "c"


def test_media_cache_get_unknown_returns_none(tmp_path: Path):
    cache = MediaCache(tmp_path / "media_cache.json")

    assert cache.get("https://example.com/never-seen") is None


@pytest.mark.asyncio
async def test_media_cache_evicts_oldest(tmp_path: Path):
    cache_file = tmp_path / "media_cache.json"
    cache = MediaCache(cache_file, max_entries=2)
    for index in range(3):
        await cache.record(
            f"https://example.com/{index}",
            [CachedMedia(file_id=f"F{index}", send_type="photo", file_name=f"{index}.jpg", caption=None)],
        )
        await cache.record(  # re-touch to order updated_at deterministically
            f"https://example.com/{index}",
            cache.get(f"https://example.com/{index}"),
        )

    assert cache.get("https://example.com/0") is None
    assert cache.get("https://example.com/1") is not None
    assert cache.get("https://example.com/2") is not None


@pytest.mark.asyncio
async def test_media_cache_remove_deletes(tmp_path: Path):
    cache = MediaCache(tmp_path / "media_cache.json")
    await cache.record(
        "https://example.com/f.mp4",
        [CachedMedia(file_id="BAAC1", send_type="video", file_name="f.mp4", caption=None)],
    )

    await cache.remove("https://example.com/f.mp4")

    assert cache.get("https://example.com/f.mp4") is None


def test_media_cache_corrupt_file_starts_empty(tmp_path: Path):
    cache_file = tmp_path / "media_cache.json"
    cache_file.write_text("{bad json", encoding="utf-8")

    cache = MediaCache(cache_file)

    assert cache.get("https://example.com/anything") is None


@pytest.mark.asyncio
async def test_media_cache_record_writes_valid_json(tmp_path: Path):
    cache_file = tmp_path / "media_cache.json"
    cache = MediaCache(cache_file)

    await cache.record(
        "https://example.com/f.mp4",
        [CachedMedia(file_id="BAAC1", send_type="video", file_name="f.mp4", caption=None)],
    )

    payload = json.loads(cache_file.read_text(encoding="utf-8"))
    assert payload["version"] == 1
    assert "https://example.com/f.mp4" in payload["entries"]
    assert not cache_file.with_name("media_cache.json.tmp").exists()


@pytest.mark.asyncio
async def test_media_cache_remove_updates_persisted_file(tmp_path: Path):
    cache_file = tmp_path / "media_cache.json"
    cache = MediaCache(cache_file)
    await cache.record(
        "https://example.com/f.mp4",
        [CachedMedia(file_id="BAAC1", send_type="video", file_name="f.mp4", caption=None)],
    )
    await cache.record(
        "https://example.com/other.mp4",
        [CachedMedia(file_id="BAAC2", send_type="video", file_name="o.mp4", caption=None)],
    )

    await cache.remove("https://example.com/f.mp4")

    payload = json.loads(cache_file.read_text(encoding="utf-8"))
    assert "https://example.com/f.mp4" not in payload["entries"]
    assert "https://example.com/other.mp4" in payload["entries"]


@pytest.mark.asyncio
async def test_media_cache_persist_survives_concurrent_use(tmp_path: Path):
    cache = MediaCache(tmp_path / "media_cache.json")

    await asyncio.gather(
        *[
            cache.record(
                f"https://example.com/{index}",
                [CachedMedia(file_id=f"F{index}", send_type="photo", file_name=f"{index}.jpg", caption=None)],
            )
            for index in range(25)
        ]
    )

    reloaded = MediaCache(tmp_path / "media_cache.json")
    assert reloaded.get("https://example.com/24") is not None
