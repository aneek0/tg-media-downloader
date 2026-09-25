from __future__ import annotations

import logging

from aiogram import Router
from aiogram.types import CallbackQuery

from config import Settings
from routers.commands import handle_ui_callback
from services.executor import execute_request
from services.media_cache import MediaCache
from services.request_store import RequestStore
from services.thumbnail_store import ThumbnailStore
from utils import text
from utils.callbacks import RequestCallback, UiCallback
from utils.models import DownloadOption, StoredRequest

router = Router(name="callbacks")
logger = logging.getLogger(__name__)


def _find_option(stored: StoredRequest, action: str) -> DownloadOption | None:
    for option in stored.options:
        if option.option_id == action:
            return option
    return None


@router.callback_query(UiCallback.filter())
async def ui_callback(query: CallbackQuery, callback_data: UiCallback) -> None:
    await handle_ui_callback(query, callback_data.action)


@router.callback_query(RequestCallback.filter())
async def request_callback(
    query: CallbackQuery,
    callback_data: RequestCallback,
    settings: Settings,
    request_store: RequestStore,
    thumbnail_store: ThumbnailStore,
    media_cache: MediaCache,
) -> None:
    if not query.message:
        await query.answer()
        return

    stored = request_store.load(callback_data.token)
    if not stored:
        logger.warning(
            "Expired request callback | user=%s token=%s action=%s",
            query.from_user.id,
            callback_data.token,
            callback_data.action,
        )
        await query.message.edit_text(text.REQUEST_EXPIRED)
        await query.answer()
        return

    option = _find_option(stored, callback_data.action)
    if not option:
        logger.warning(
            "Unknown request option | user=%s token=%s action=%s",
            query.from_user.id,
            callback_data.token,
            callback_data.action,
        )
        await query.message.edit_text(text.REQUEST_EXPIRED)
        await query.answer()
        return

    await execute_request(
        stored=stored,
        option=option,
        bot=query.bot,
        status_message=query.message,
        source_message=query.message.reply_to_message or query.message,
        user_id=query.from_user.id,
        settings=settings,
        request_store=request_store,
        thumbnail_store=thumbnail_store,
        media_cache=media_cache,
    )
    await query.answer()
