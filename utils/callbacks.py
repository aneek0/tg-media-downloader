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
