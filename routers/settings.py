from __future__ import annotations

from dataclasses import replace

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from services.session import Session
from services.user_settings import UserPreferences
from utils import text as strings
from utils.callbacks import SettingsCallback

router = Router(name="settings")
_PROVIDER_LABELS = {
    "deezer": "Deezer",
    "spotify-web": "Spotify",
    "tidal-web": "Tidal",
}


def _quality_label(preferences: UserPreferences) -> str:
    if preferences.audio_format == "mp3":
        return "CD (MP3 has no Hi-Res)"
    return "Hi-Res" if preferences.audio_quality == "hires" else "CD"

def settings_view(preferences: UserPreferences) -> tuple[str, InlineKeyboardMarkup]:
    provider = _PROVIDER_LABELS[preferences.provider]
    text = (
        strings.SETTINGS_TITLE + "\n"
        f"Text search: {'ON' if preferences.text_search else 'OFF'}\n"
        f"Catalogue: {provider}\n"
        f"Audio: {preferences.audio_format.upper()} · {_quality_label(preferences)}"
    )
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"Text search: {'ON' if preferences.text_search else 'OFF'}",
                    callback_data=SettingsCallback(
                        action="text", value="0" if preferences.text_search else "1"
                    ).pack(),
                )
            ],
            [
                InlineKeyboardButton(
                    text="Deezer ✓" if preferences.provider == "deezer" else "Deezer",
                    callback_data=SettingsCallback(
                        action="provider", value="deezer"
                    ).pack(),
                ),
                InlineKeyboardButton(
                    text=(
                        "Spotify ✓"
                        if preferences.provider == "spotify-web"
                        else "Spotify"
                    ),
                    callback_data=SettingsCallback(
                        action="provider", value="spotify-web"
                    ).pack(),
                ),
                InlineKeyboardButton(
                    text="Tidal ✓" if preferences.provider == "tidal-web" else "Tidal",
                    callback_data=SettingsCallback(
                        action="provider", value="tidal-web"
                    ).pack(),
                ),
            ],
            [
                InlineKeyboardButton(
                    text="FLAC ✓" if preferences.audio_format == "flac" else "FLAC",
                    callback_data=SettingsCallback(action="format", value="flac").pack(),
                ),
                InlineKeyboardButton(
                    text="MP3 ✓" if preferences.audio_format == "mp3" else "MP3",
                    callback_data=SettingsCallback(action="format", value="mp3").pack(),
                ),
            ],
            [
                InlineKeyboardButton(
                    text="CD ✓" if preferences.audio_quality == "lossless" else "CD",
                    callback_data=SettingsCallback(
                        action="quality", value="lossless"
                    ).pack(),
                ),
                InlineKeyboardButton(
                    text=(
                        "Hi-Res ✓"
                        if preferences.audio_quality == "hires"
                        else "Hi-Res"
                    ),
                    callback_data=SettingsCallback(
                        action="quality", value="hires"
                    ).pack(),
                ),
            ],
        ]
    )
    return text, keyboard


@router.message(Command("settings"), F.chat.type == "private")
async def settings_command(message: Message, session: Session) -> None:
    if not message.from_user:
        return
    text, keyboard = settings_view(session.preferences(message.from_user.id))
    await message.answer(text, reply_markup=keyboard)


def _selection_is_unchanged(
    session: Session, user_id: int, values: dict[str, object]
) -> bool:
    """True when the tapped button already is the stored preference.

    Re-rendering then would call editText with identical content, which
    Telegram rejects as "message is not modified"."""
    current = session.preferences(user_id)
    return replace(current, **values) == current


@router.callback_query(SettingsCallback.filter())
async def settings_callback(
    query: CallbackQuery, callback_data: SettingsCallback, session: Session
) -> None:
    if not isinstance(query.message, Message):
        await query.answer(
            strings.SETTINGS_OPEN_DM.format(
                bot=session.identity.mention or strings.GENERIC_BOT
            ),
            show_alert=True,
        )
        return
    values: dict[str, object]
    if callback_data.action == "text":
        values = {"text_search": callback_data.value == "1"}
    elif callback_data.action == "provider":
        values = {"provider": callback_data.value}
    elif callback_data.action == "format":
        values = {"audio_format": callback_data.value}
    elif callback_data.action == "quality":
        values = {"audio_quality": callback_data.value}
    else:
        await query.answer()
        return
    # Re-tapping the active button must not call editText with an identical
    # view: Telegram rejects that with "message is not modified".
    if _selection_is_unchanged(session, query.from_user.id, values):
        await query.answer(strings.SETTINGS_UNCHANGED)
        return
    try:
        preferences = session.update_preferences(query.from_user.id, **values)
    except ValueError as exc:
        await query.answer(str(exc), show_alert=True)
        return
    text, keyboard = settings_view(preferences)
    await query.message.edit_text(text, reply_markup=keyboard)
    await query.answer(strings.SETTINGS_SAVED)
