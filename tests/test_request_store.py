import json
import os
import shutil
import time
from pathlib import Path

import pytest

from services.request_store import RequestStore
from utils.models import (
    CataloguePayload,
    DirectPayload,
    ParsedInput,
    RequestType,
    StoredRequest,
)


def make_store(tmp_path: Path) -> RequestStore:
    return RequestStore(tmp_path / "requests", tmp_path / "work")


def make_stored(token: str) -> StoredRequest:
    return StoredRequest(
        token=token,
        request_type="direct_download",
        parsed_input=ParsedInput(source_url="https://example.com/file.mp4"),
        options=[],
    )


def age_file(path: Path, seconds_old: float) -> None:
    stamp = time.time() - seconds_old
    os.utime(path, (stamp, stamp))


# --- roundtrip ---------------------------------------------------------------


def test_request_store_roundtrip_save_load_delete(tmp_path: Path):
    store = make_store(tmp_path)
    stored = make_stored("c456558aeb")

    store.save(stored)
    assert store.load("c456558aeb") == stored

    store.delete("c456558aeb")
    assert store.load("c456558aeb") is None


def test_open_persists_the_payload_under_a_new_token(tmp_path: Path):
    store = make_store(tmp_path)

    stored = store.open(
        RequestType.CATALOGUE_BROWSE,
        ParsedInput(source_url="https://www.deezer.com/artist/1"),
        payload=CataloguePayload(provider="deezer", artist_id="deezer:1"),
    )

    reloaded = store.load(stored.token)
    assert reloaded is not None
    assert reloaded.request_type is RequestType.CATALOGUE_BROWSE
    payload = reloaded.payload
    assert isinstance(payload, CataloguePayload)
    assert payload.provider == "deezer"


def test_open_rejects_a_payload_of_the_wrong_shape(tmp_path: Path):
    """A mismatched payload would decode into the wrong variant later."""
    store = make_store(tmp_path)

    with pytest.raises(ValueError, match="DirectPayload"):
        store.open(
            RequestType.DIRECT_DOWNLOAD,
            ParsedInput(source_url="https://example.com/a.mp4"),
            payload=CataloguePayload(provider="deezer", artist_id="deezer:1"),
        )


def test_update_payload_keeps_the_token(tmp_path: Path):
    """Artist Cards and Pixiv pages refresh state under the token their buttons carry."""
    store = make_store(tmp_path)
    stored = store.open(
        RequestType.DIRECT_DOWNLOAD,
        ParsedInput(source_url="https://example.com/a.mp4"),
        payload=DirectPayload(info={"ext": "mp4"}),
    )

    store.update_payload(stored, DirectPayload(info={"ext": "mkv"}))

    reloaded = store.load(stored.token)
    assert reloaded is not None
    assert reloaded.token == stored.token
    payload = reloaded.payload
    assert isinstance(payload, DirectPayload)
    assert payload.ext == "mkv"


def test_lifecycle_removes_the_token_and_work_directory(tmp_path: Path):
    store = make_store(tmp_path)
    stored = store.open(
        RequestType.DIRECT_DOWNLOAD,
        ParsedInput(source_url="https://example.com/a.mp4"),
        payload=DirectPayload(),
    )
    work = store.work_directory(stored.token)
    (work / "part.bin").write_text("x", encoding="utf-8")

    with store.lifecycle(stored.token) as token:
        assert token == stored.token

    assert store.load(stored.token) is None
    assert not work.exists()


def test_request_store_delete_removes_work_directory(tmp_path: Path):
    store = make_store(tmp_path)
    stored = make_stored("5312e634fe")
    store.save(stored)

    work = store.work_directory("5312e634fe")
    (work / "part.bin").write_bytes(b"x")

    store.delete("5312e634fe")

    assert not store.requests_dir.joinpath("tok2.json").exists()
    assert not work.exists()


def test_request_store_load_missing_token_returns_none(tmp_path: Path):
    store = make_store(tmp_path)

    assert store.load("0fe7e7abc1") is None


def test_request_store_delete_missing_token_is_noop(tmp_path: Path):
    store = make_store(tmp_path)

    store.delete("does-not-exist")


# --- sweep_stale -------------------------------------------------------------


def test_sweep_stale_removes_old_request_and_work_dir(tmp_path: Path):
    store = make_store(tmp_path)
    stale = make_stored("01d70c5a1b")
    store.save(stale)
    age_file(store.requests_dir / "01d70c5a1b.json", 25 * 3600)

    work = store.work_directory("01d70c5a1b")
    (work / "leftover.mp4").write_bytes(b"x")

    fresh = make_stored("0e1a70c5a2")
    store.save(fresh)

    removed = store.sweep_stale()

    assert removed == 1
    assert store.load("01d70c5a1b") is None
    assert not work.exists()
    assert store.load("0e1a70c5a2") is not None


def test_sweep_stale_keeps_fresh_requests(tmp_path: Path):
    store = make_store(tmp_path)
    store.save(make_stored("0a7e51c5a3"))
    age_file(store.requests_dir / "0a7e51c5a3.json", 3600)

    assert store.sweep_stale() == 0
    assert store.load("0a7e51c5a3") is not None


def test_sweep_stale_age_boundary_is_inclusive(tmp_path: Path):
    store = make_store(tmp_path)
    store.save(make_stored("0ed9e7c5a4"))
    age_file(store.requests_dir / "0ed9e7c5a4.json", 24 * 3600 - 5)

    assert store.sweep_stale() == 0
    assert store.load("0ed9e7c5a4") is not None


def test_sweep_stale_empty_dirs_is_noop(tmp_path: Path):
    store = make_store(tmp_path)

    assert store.sweep_stale() == 0


def test_sweep_stale_missing_dirs_is_noop(tmp_path: Path):
    store = make_store(tmp_path)
    shutil.rmtree(store.requests_dir)
    shutil.rmtree(store.work_root)

    assert store.sweep_stale() == 0


def test_sweep_stale_ignores_non_json_files(tmp_path: Path):
    store = make_store(tmp_path)
    junk = store.requests_dir / "notes.txt"
    junk.write_text("not a request", encoding="utf-8")
    age_file(junk, 48 * 3600)

    assert store.sweep_stale() == 0
    assert junk.exists()


def test_sweep_stale_custom_max_age(tmp_path: Path):
    store = make_store(tmp_path)
    store.save(make_stored("0a0b01dc5a"))
    age_file(store.requests_dir / "0a0b01dc5a.json", 2 * 3600)

    assert store.sweep_stale(max_age_seconds=3600) == 1
    assert store.load("0a0b01dc5a") is None


def test_sweep_stale_work_dir_removed_without_extra_work_dirs(tmp_path: Path):
    # A stale request with no work dir at all must still be swept cleanly.
    store = make_store(tmp_path)
    payload = make_stored("0a9b1ac5a6")
    store.requests_dir.joinpath("0a9b1ac5a6.json").write_text(
        json.dumps(payload.to_dict()), encoding="utf-8"
    )
    age_file(store.requests_dir / "0a9b1ac5a6.json", 30 * 3600)

    assert store.sweep_stale() == 1
    assert store.load("0a9b1ac5a6") is None
