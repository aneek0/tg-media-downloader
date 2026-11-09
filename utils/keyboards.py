from collections.abc import Sequence

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from utils.callbacks import (
    GalleryNavCallback,
    PixivNavCallback,
    RequestCallback,
    UiCallback,
)
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


def pixiv_page_keyboard(
    *,
    token: str,
    page: int,
    download_label: str,
    has_next: bool = True,
    works: Sequence[str] = (),
) -> InlineKeyboardMarkup:
    """Profile Page switcher: one button per Work on the page (its title picks
    the Work), then previous, download this page, next (hidden on the last)."""
    builder = InlineKeyboardBuilder()
    for index, title in enumerate(works):
        builder.row(
            InlineKeyboardButton(
                text=title,
                callback_data=PixivNavCallback(
                    token=token, page=page, action=f"w{index}"
                ).pack(),
            )
        )
    buttons = [
        InlineKeyboardButton(
            text="◀",
            callback_data=PixivNavCallback(
                token=token, page=max(page - 1, 0), action="prev"
            ).pack(),
        ),
        InlineKeyboardButton(
            text=download_label,
            callback_data=PixivNavCallback(
                token=token, page=page, action="dl"
            ).pack(),
        ),
    ]
    if has_next:
        buttons.append(
            InlineKeyboardButton(
                text="▶",
                callback_data=PixivNavCallback(
                    token=token, page=page + 1, action="next"
                ).pack(),
            )
        )
    builder.row(*buttons)
    return builder.as_markup()
