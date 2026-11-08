from __future__ import annotations

import logging
from typing import cast

from aiogram import Bot, Router
from aiogram.types import CallbackQuery, InaccessibleMessage

from routers.commands import handle_ui_callback
from services.executor import execute_request
from services.session import Session
from services.status import chat_status
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
    session: Session,
) -> None:
    message = query.message
    if not message or isinstance(message, InaccessibleMessage):
        await query.answer()
        return

    # aiogram binds a bot to every dispatched update.
    bot = cast(Bot, query.bot)

    stored = session.request_store.load(callback_data.token)
    if not stored:
        logger.warning(
            "Expired request callback | user=%s token=%s action=%s",
            query.from_user.id,
            callback_data.token,
            callback_data.action,
        )
        await message.edit_text(text.REQUEST_EXPIRED)
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
        await message.edit_text(text.REQUEST_EXPIRED)
        await query.answer()
        return

    await execute_request(
        session,
        stored,
        option,
        bot=bot,
        status=chat_status(message),
        source_message=message.reply_to_message or message,
        user_id=query.from_user.id,
    )
    await query.answer()
