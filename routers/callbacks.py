from __future__ import annotations

import logging
from typing import cast

from aiogram import Bot, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery, InaccessibleMessage

from routers.commands import handle_ui_callback
from services import pixiv, pixiv_flow
from services.executor import execute_request
from services.session import Session
from services.status import chat_status, inline_status
from utils import text
from utils.callbacks import PixivNavCallback, RequestCallback, UiCallback
from utils.keyboards import pixiv_page_keyboard
from utils.models import DownloadOption, RequestType, StoredRequest

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


@router.callback_query(PixivNavCallback.filter())
async def pixiv_callback(
    query: CallbackQuery,
    callback_data: PixivNavCallback,
    session: Session,
) -> None:
    """Profile Page navigation and page downloads; the token outlives both."""
    stored = session.request_store.load(callback_data.token)
    if not stored or stored.request_type is not RequestType.PIXIV_PROFILE:
        await query.answer(text.REQUEST_EXPIRED, show_alert=True)
        return
    # aiogram binds a bot to every dispatched update.
    bot = cast(Bot, query.bot)
    page = max(callback_data.page, 0)
    if query.inline_message_id:
        await _pixiv_inline(
            query, bot, session, stored, page, callback_data.action
        )
        return
    message = query.message
    if not message or isinstance(message, InaccessibleMessage):
        await query.answer()
        return
    status = chat_status(message)
    if callback_data.action == "dl":
        await pixiv_flow.download_page(
            session,
            bot=bot,
            stored=stored,
            page=page,
            status=status,
            reply=message,
            chat_id=message.chat.id,
        )
    else:
        await pixiv_flow.render_page(
            session,
            bot=bot,
            stored=stored,
            page=page,
            chat_id=message.chat.id,
            status=status,
        )
    await query.answer()


async def _pixiv_inline(
    query: CallbackQuery,
    bot: Bot,
    session: Session,
    stored: StoredRequest,
    page: int,
    action: str,
) -> None:
    inline_message_id = query.inline_message_id or ""
    if action == "dl":
        await pixiv_flow.download_page(
            session,
            bot=bot,
            stored=stored,
            page=page,
            status=inline_status(bot, inline_message_id, name="Pixiv"),
            reply=None,
            chat_id=query.from_user.id,
        )
        await query.answer()
        return
    try:
        summary, has_next = await pixiv_flow.page_summary(session, stored, page)
    except pixiv.PixivError as exc:
        await query.answer(text.esc(str(exc)), show_alert=True)
        return
    try:
        await bot.edit_message_text(
            inline_message_id=inline_message_id,
            text=summary,
            reply_markup=pixiv_page_keyboard(
                token=stored.token,
                page=page,
                download_label=text.PIXIV_DOWNLOAD_PAGE_SHORT,
                has_next=has_next,
            ),
        )
    except TelegramAPIError:
        await query.answer(text.REQUEST_EXPIRED, show_alert=True)
        return
    await query.answer()
