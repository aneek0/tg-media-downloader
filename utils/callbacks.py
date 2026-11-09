"""Typed callback payloads plus the token protocol they carry.

A catalogue button addresses two things at once — the card it belongs to and
the album index or track Request behind it — and the token string is the only
place that fits (callback_data is capped at 64 bytes). The format lives here so
building and parsing cannot drift apart.
"""

from aiogram.filters.callback_data import CallbackData


class UiCallback(CallbackData, prefix="ui"):
    action: str


class RequestCallback(CallbackData, prefix="req"):
    token: str
    action: str


class InlineCancelCallback(CallbackData, prefix="icxl"):
    token: str


class GalleryNavCallback(CallbackData, prefix="gnav"):
    token: str
    index: int


class CatalogueCallback(CallbackData, prefix="cat"):
    action: str
    token: str


class SettingsCallback(CallbackData, prefix="set"):
    action: str
    value: str = ""


class FollowCallback(CallbackData, prefix="fol"):
    action: str
    provider: str
    artist_id: str


class PixivNavCallback(CallbackData, prefix="pxv"):
    token: str
    page: int
    action: str


_SEPARATOR = "."


def join_token(head: str, tail: str | int) -> str:
    """Callback token addressing ``head`` plus an index or a child token."""
    return f"{head}{_SEPARATOR}{tail}"


def split_token(token: str) -> tuple[str, str | None]:
    """Reverse :func:`join_token`; the tail is None for a plain token."""
    head, _, tail = token.partition(_SEPARATOR)
    return head, tail or None
