from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.types import Chat, Message

from routers.settings import (
    _selection_is_unchanged,
    settings_callback,
    settings_view,
)
from services.user_settings import UserPreferences
from tests.conftest import make_session, make_settings
from utils import text as strings
from utils.callbacks import SettingsCallback


def _button_texts(markup) -> list[str]:
    return [button.text for row in markup.inline_keyboard for button in row]


def test_view_marks_selected_quality():
    text, keyboard = settings_view(UserPreferences(audio_quality="hires"))

    assert "Audio: FLAC · Hi-Res" in text
    assert "Hi-Res ✓" in _button_texts(keyboard)


def test_view_says_mp3_cannot_be_hi_res():
    text, _ = settings_view(
        UserPreferences(audio_format="mp3", audio_quality="hires")
    )

    assert "Audio: MP3 · CD (MP3 has no Hi-Res)" in text


def test_tidal_provider_is_available_in_settings(tmp_path):
    session = make_session(make_settings(tmp_path))

    preferences = session.update_preferences(7, provider="tidal-web")
    view, keyboard = settings_view(preferences)

    assert preferences.provider == "tidal-web"
    assert "Catalogue: Tidal" in view
    assert "Tidal ✓" in _button_texts(keyboard)


def test_session_update_preferences_persists_quality(tmp_path):
    session = make_session(make_settings(tmp_path))

    session.update_preferences(7, audio_quality="hires")

    assert session.preferences(7).audio_quality == "hires"
    assert "Hi-Res" in settings_view(session.preferences(7))[0]


def test_reselecting_the_active_quality_is_a_no_op(tmp_path):
    """Pin the 'message is not modified' guard for repeated button taps."""
    session = make_session(make_settings(tmp_path))
    session.update_preferences(7, audio_quality="hires")

    assert _selection_is_unchanged(session, 7, {"audio_quality": "hires"})
    assert not _selection_is_unchanged(session, 7, {"audio_quality": "lossless"})
    assert _selection_is_unchanged(session, 7, {"text_search": True})
    assert not _selection_is_unchanged(session, 7, {"text_search": False})


@pytest.mark.asyncio
async def test_repeated_quality_tap_never_edits_an_identical_message(
    monkeypatch, tmp_path
):
    """Regression for TelegramBadRequest: message is not modified."""
    session = make_session(make_settings(tmp_path))
    session.update_preferences(7, audio_quality="hires")
    message = Message.model_construct(
        message_id=1,
        date=datetime.now(timezone.utc),
        chat=Chat(id=7, type="private"),
    )
    edit_text = AsyncMock()
    monkeypatch.setattr("routers.settings.Message.edit_text", edit_text)
    query = SimpleNamespace(
        from_user=SimpleNamespace(id=7), message=message, answer=AsyncMock()
    )

    await settings_callback(
        query, SettingsCallback(action="quality", value="hires"), session
    )

    edit_text.assert_not_awaited()
    query.answer.assert_awaited_once_with(strings.SETTINGS_UNCHANGED)

    await settings_callback(
        query, SettingsCallback(action="quality", value="lossless"), session
    )

    assert edit_text.await_count == 1
    assert session.preferences(7).audio_quality == "lossless"


@pytest.mark.asyncio
async def test_inline_settings_tap_names_the_bot(tmp_path):
    """A settings button in a foreign chat must point at this bot by handle,
    not at an unnamed 'the bot chat'."""
    session = make_session(make_settings(tmp_path))
    session.identity.mention = "@my_media_bot"
    query = SimpleNamespace(
        from_user=SimpleNamespace(id=7),
        message=None,
        answer=AsyncMock(),
    )

    await settings_callback(
        query, SettingsCallback(action="quality", value="hires"), session
    )

    call = query.answer.await_args
    assert call.kwargs["show_alert"] is True
    assert "@my_media_bot" in call.args[0]