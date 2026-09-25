from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from utils.callbacks import GalleryNavCallback, RequestCallback, UiCallback
from utils.models import DownloadOption


def start_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text="Help", callback_data=UiCallback(action="help").pack()
        ),
        InlineKeyboardButton(
            text="About", callback_data=UiCallback(action="about").pack()
        ),
    )
    builder.row(
        InlineKeyboardButton(
            text="Close", callback_data=UiCallback(action="close").pack()
        )
    )
    return builder.as_markup()


def help_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text="Home", callback_data=UiCallback(action="home").pack()
        ),
        InlineKeyboardButton(
            text="About", callback_data=UiCallback(action="about").pack()
        ),
    )
    builder.row(
        InlineKeyboardButton(
            text="Close", callback_data=UiCallback(action="close").pack()
        )
    )
    return builder.as_markup()


def about_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text="Home", callback_data=UiCallback(action="home").pack()
        ),
        InlineKeyboardButton(
            text="Help", callback_data=UiCallback(action="help").pack()
        ),
    )
    builder.row(
        InlineKeyboardButton(
            text="Close", callback_data=UiCallback(action="close").pack()
        )
    )
    return builder.as_markup()


def format_keyboard(token: str, options: list[DownloadOption]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for option in options:
        builder.row(
            InlineKeyboardButton(
                text=option.label,
                callback_data=RequestCallback(
                    token=token, action=option.option_id
                ).pack(),
            )
        )
    builder.row(
        InlineKeyboardButton(
            text="Close", callback_data=UiCallback(action="close").pack()
        )
    )
    return builder.as_markup()


def gallery_keyboard(token: str, count: int, index: int) -> InlineKeyboardMarkup:
    """Inline gallery pager «prev./next.». Only built for count >= 2."""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text=f"◀ {index + 1}/{count}",
            callback_data=GalleryNavCallback(token=token, index=(index - 1) % count).pack(),
        ),
        InlineKeyboardButton(
            text="▶",
            callback_data=GalleryNavCallback(token=token, index=(index + 1) % count).pack(),
        ),
    )
    return builder.as_markup()
