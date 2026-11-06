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
