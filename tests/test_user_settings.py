import json
from pathlib import Path

import pytest

from services.user_settings import UserPreferences, UserSettingsStore


def test_get_missing_file_returns_defaults_without_creating_file(tmp_path: Path):
    path = tmp_path / "user_settings.json"
    store = UserSettingsStore(path)

    assert store.get(42) == UserPreferences()
    assert not path.exists()


def test_get_applies_custom_defaults(tmp_path: Path):
    store = UserSettingsStore(tmp_path / "user_settings.json")

    prefs = store.get(42, default_provider="spotify-web", default_audio_format="mp3")

    assert prefs.provider == "spotify-web"
    assert prefs.audio_format == "mp3"
    assert prefs.text_search is True


def test_audio_quality_defaults_to_lossless(tmp_path: Path):
    store = UserSettingsStore(tmp_path / "user_settings.json")

    assert store.get(1).audio_quality == "lossless"
    assert store.update(1, audio_quality="hires").audio_quality == "hires"


def test_legacy_store_without_audio_quality_still_loads(tmp_path: Path):
    """Pins the v1 store shape written before the Hi-Res preference existed."""
    path = tmp_path / "user_settings.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "entries": {
                    "5": {
                        "text_search": True,
                        "provider": "deezer",
                        "audio_format": "flac",
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    assert UserSettingsStore(path).get(5).audio_quality == "lossless"


def test_update_persists_and_reloads(tmp_path: Path):
    path = tmp_path / "user_settings.json"
    store = UserSettingsStore(path)

    updated = store.update(
        7, provider="spotify-web", audio_format="mp3", text_search=False
    )

    assert updated == UserPreferences(
        text_search=False, provider="spotify-web", audio_format="mp3"
    )
    assert not list(tmp_path.glob("*.tmp"))

    reloaded = UserSettingsStore(path)
    assert reloaded.get(7) == updated


def test_update_is_partial_per_user(tmp_path: Path):
    store = UserSettingsStore(tmp_path / "user_settings.json")
    store.update(1, provider="spotify-web")

    other = store.update(2, audio_format="mp3")

    assert store.get(1).provider == "spotify-web"
    assert store.get(1).audio_format == "flac"
    assert other.provider == "deezer"
    assert store.get(2).provider == "deezer"


@pytest.mark.parametrize(
    "field,value",
    [
        ("provider", "applemusic"),
        ("audio_format", "wav"),
        ("audio_quality", "dsd"),
        ("text_search", "yes"),
        ("text_search", 1),
    ],
)
def test_update_rejects_invalid_values(tmp_path: Path, field, value):
    path = tmp_path / "user_settings.json"
    store = UserSettingsStore(path)

    with pytest.raises(ValueError):
        store.update(1, **{field: value})

    assert not path.exists()


def test_update_rejects_unknown_fields(tmp_path: Path):
    store = UserSettingsStore(tmp_path / "user_settings.json")

    with pytest.raises(ValueError):
        store.update(1, language="ru")


@pytest.mark.parametrize("user_id", ["7", True, None, 7.0])
def test_get_and_update_reject_non_int_user_id(tmp_path: Path, user_id):
    store = UserSettingsStore(tmp_path / "user_settings.json")

    with pytest.raises(ValueError):
        store.get(user_id)
    with pytest.raises(ValueError):
        store.update(user_id, provider="deezer")


def test_get_rejects_invalid_defaults(tmp_path: Path):
    store = UserSettingsStore(tmp_path / "user_settings.json")

    with pytest.raises(ValueError):
        store.get(1, default_provider="applemusic")
    with pytest.raises(ValueError):
        store.get(1, default_audio_format="wav")


def test_malformed_json_raises_and_keeps_file(tmp_path: Path):
    path = tmp_path / "user_settings.json"
    path.write_text("{not json", encoding="utf-8")

    with pytest.raises(RuntimeError, match="unreadable"):
        UserSettingsStore(path)

    assert path.read_text(encoding="utf-8") == "{not json"


def test_invalid_schema_raises_without_overwriting(tmp_path: Path):
    path = tmp_path / "user_settings.json"
    path.write_text(
        json.dumps({"version": 1, "entries": {"1": {"provider": "deezer"}}}),
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError):
        UserSettingsStore(path)

    assert json.loads(path.read_text(encoding="utf-8"))["entries"]["1"] == {
        "provider": "deezer"
    }


def test_wrong_version_raises(tmp_path: Path):
    path = tmp_path / "user_settings.json"
    path.write_text(
        json.dumps({"version": 2, "entries": {}}), encoding="utf-8"
    )

    with pytest.raises(RuntimeError):
        UserSettingsStore(path)