import json
from pathlib import Path

import pytest

from services.follow_store import Follow, FollowStore


def test_toggle_adds_with_baseline_then_removes(tmp_path: Path):
    path = tmp_path / "follows.json"
    store = FollowStore(path)

    assert store.toggle(
        1, "deezer", "a1", "Aria", "https://c/1.jpg", ["al1", "al2"]
    ) is True

    follows = store.list_for_user(1)
    assert follows == [
        Follow(
            user_id=1,
            provider="deezer",
            artist_id="a1",
            artist_name="Aria",
            cover_url="https://c/1.jpg",
            known_album_ids=["al1", "al2"],
        )
    ]
    assert store.toggle(1, "deezer", "a1", "Aria") is False
    assert store.list_for_user(1) == []
    assert store.toggle(1, "deezer", "a1", "Aria") is True


def test_toggle_persists_across_reload(tmp_path: Path):
    path = tmp_path / "follows.json"
    store = FollowStore(path)
    store.toggle(5, "spotify-web", "sp1", "Bolt", known_album_ids=["x"])

    reloaded = FollowStore(path)

    assert reloaded.get("spotify-web", "sp1") == [
        Follow(
            user_id=5,
            provider="spotify-web",
            artist_id="sp1",
            artist_name="Bolt",
            known_album_ids=["x"],
        )
    ]
    assert not list(tmp_path.glob("*.tmp"))


def test_users_have_independent_follows(tmp_path: Path):
    store = FollowStore(tmp_path / "follows.json")
    store.toggle(1, "deezer", "a1", "Aria")
    store.toggle(2, "deezer", "a1", "Aria")
    store.toggle(2, "deezer", "a2", "Bolt")

    assert [(f.user_id, f.artist_id) for f in store.list_for_user(1)] == [(1, "a1")]
    assert store.list_for_user(3) == []
    assert store.get("deezer", "a1") == store.list_for_user(1) + [
        f for f in store.list_for_user(2) if f.artist_id == "a1"
    ]
    assert store.all_artists() == [("deezer", "a1"), ("deezer", "a2")]


def test_update_albums_updates_every_follower_and_persists(tmp_path: Path):
    path = tmp_path / "follows.json"
    store = FollowStore(path)
    store.toggle(1, "deezer", "a1", "Aria", known_album_ids=["old"])
    store.toggle(2, "deezer", "a1", "Aria", known_album_ids=["old"])
    store.toggle(3, "deezer", "a2", "Bolt", known_album_ids=["keep"])

    store.update_albums("deezer", "a1", ["n1", "n2"])

    assert [f.known_album_ids for f in store.get("deezer", "a1")] == [
        ["n1", "n2"],
        ["n1", "n2"],
    ]
    assert store.get("deezer", "a2")[0].known_album_ids == ["keep"]
    assert [
        f.known_album_ids for f in FollowStore(path).get("deezer", "a1")
    ] == [["n1", "n2"], ["n1", "n2"]]


def test_update_albums_unknown_artist_is_noop(tmp_path: Path):
    path = tmp_path / "follows.json"
    store = FollowStore(path)

    store.update_albums("deezer", "missing", ["n1"])

    assert not path.exists()


def test_remove(tmp_path: Path):
    store = FollowStore(tmp_path / "follows.json")
    store.toggle(1, "deezer", "a1", "Aria")

    assert store.remove(1, "deezer", "a1") is True
    assert store.remove(1, "deezer", "a1") is False
    assert store.remove(2, "deezer", "a1") is False


@pytest.mark.parametrize(
    "args,kwargs",
    [
        ((1, "applemusic", "a1", "Aria"), {}),
        ((1, "deezer", "", "Aria"), {}),
        ((1, "deezer", "a1", 5), {}),
        ((True, "deezer", "a1", "Aria"), {}),
        (("1", "deezer", "a1", "Aria"), {}),
        ((1, "deezer", "a1", "Aria"), {"known_album_ids": "not-a-list"}),
        ((1, "deezer", "a1", "Aria"), {"known_album_ids": [1, 2]}),
    ],
)
def test_toggle_rejects_invalid_input(tmp_path: Path, args, kwargs):
    path = tmp_path / "follows.json"
    store = FollowStore(path)

    with pytest.raises(ValueError):
        store.toggle(*args, **kwargs)

    assert not path.exists()


def test_query_helpers_validate_input(tmp_path: Path):
    store = FollowStore(tmp_path / "follows.json")

    with pytest.raises(ValueError):
        store.list_for_user("1")
    with pytest.raises(ValueError):
        store.get("applemusic", "a1")
    with pytest.raises(ValueError):
        store.get("deezer", "")
    with pytest.raises(ValueError):
        store.remove(1, "applemusic", "a1")
    with pytest.raises(ValueError):
        store.update_albums("deezer", "a1", "not-a-list")


def test_malformed_json_raises_and_keeps_file(tmp_path: Path):
    path = tmp_path / "follows.json"
    path.write_text("[broken", encoding="utf-8")

    with pytest.raises(RuntimeError, match="unreadable"):
        FollowStore(path)

    assert path.read_text(encoding="utf-8") == "[broken"


def test_invalid_record_raises_without_overwriting(tmp_path: Path):
    path = tmp_path / "follows.json"
    payload = {
        "version": 1,
        "follows": [
            {
                "user_id": 1,
                "provider": "deezer",
                "artist_id": "a1",
                "artist_name": "Aria",
                "cover_url": "",
                "known_album_ids": [],
            },
            {"user_id": 2, "provider": "applemusic", "artist_id": "x"},
        ],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(RuntimeError):
        FollowStore(path)

    assert json.loads(path.read_text(encoding="utf-8")) == payload


def test_wrong_version_raises(tmp_path: Path):
    path = tmp_path / "follows.json"
    path.write_text(json.dumps({"version": 2, "follows": []}), encoding="utf-8")

    with pytest.raises(RuntimeError):
        FollowStore(path)


def test_toggle_supports_tidal_provider(tmp_path: Path):
    """Tidal Artist Cards must be able to persist and reload follows."""
    path = tmp_path / "follows.json"
    store = FollowStore(path)

    assert store.toggle(1, "tidal-web", "tidal:1", "Artist") is True

    assert FollowStore(path).get("tidal-web", "tidal:1") == [
        Follow(user_id=1, provider="tidal-web", artist_id="tidal:1", artist_name="Artist")
    ]