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
    stored = make_stored("tok1")

    store.save(stored)
    assert store.load("tok1") == stored

    store.delete("tok1")
    assert store.load("tok1") is None


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
    stored = make_stored("tok2")
    store.save(stored)

    work = store.work_directory("tok2")
    (work / "part.bin").write_bytes(b"x")

    store.delete("tok2")

    assert not store.requests_dir.joinpath("tok2.json").exists()
    assert not work.exists()


def test_request_store_load_missing_token_returns_none(tmp_path: Path):
    store = make_store(tmp_path)

    assert store.load("never-saved") is None


def test_request_store_delete_missing_token_is_noop(tmp_path: Path):
    store = make_store(tmp_path)

    store.delete("does-not-exist")


# --- sweep_stale -------------------------------------------------------------


def test_sweep_stale_removes_old_request_and_work_dir(tmp_path: Path):
    store = make_store(tmp_path)
    stale = make_stored("oldtok")
    store.save(stale)
    age_file(store.requests_dir / "oldtok.json", 25 * 3600)

    work = store.work_directory("oldtok")
    (work / "leftover.mp4").write_bytes(b"x")

    fresh = make_stored("newtok")
    store.save(fresh)

    removed = store.sweep_stale()

    assert removed == 1
    assert store.load("oldtok") is None
    assert not work.exists()
    assert store.load("newtok") is not None


def test_sweep_stale_keeps_fresh_requests(tmp_path: Path):
    store = make_store(tmp_path)
    store.save(make_stored("freshtok"))
    age_file(store.requests_dir / "freshtok.json", 3600)

    assert store.sweep_stale() == 0
    assert store.load("freshtok") is not None


def test_sweep_stale_age_boundary_is_inclusive(tmp_path: Path):
    store = make_store(tmp_path)
    store.save(make_stored("edgetok"))
    age_file(store.requests_dir / "edgetok.json", 24 * 3600 - 5)

    assert store.sweep_stale() == 0
    assert store.load("edgetok") is not None


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
    store.save(make_stored("hourold"))
    age_file(store.requests_dir / "hourold.json", 2 * 3600)

    assert store.sweep_stale(max_age_seconds=3600) == 1
    assert store.load("hourold") is None


def test_sweep_stale_work_dir_removed_without_extra_work_dirs(tmp_path: Path):
    # A stale request with no work dir at all must still be swept cleanly.
    store = make_store(tmp_path)
    payload = make_stored("orphan")
    store.requests_dir.joinpath("orphan.json").write_text(
        json.dumps(payload.to_dict()), encoding="utf-8"
    )
    age_file(store.requests_dir / "orphan.json", 30 * 3600)

    assert store.sweep_stale() == 1
    assert store.load("orphan") is None
