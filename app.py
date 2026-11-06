from __future__ import annotations

import asyncio
import contextlib
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import PRODUCTION, TelegramAPIServer
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.types import BotCommand

from utils.logging_config import setup_logging
from routers.callbacks import router as callbacks_router
from routers.catalogue import router as catalogue_router
from routers.commands import router as commands_router
from routers.follows import router as follows_router
from routers.intake import router as intake_router
from routers.inline import router as inline_router
from routers.settings import router as settings_router
from config import Settings
from services.mornye import MornyeService
from services.session import Session, build_session
from services.follows import FollowPoller

BOT_COMMANDS = [
    BotCommand(command="start", description="Start the bot"),
    BotCommand(command="help", description="Supported links and usage"),
    BotCommand(command="about", description="About this bot"),
    BotCommand(command="settings", description="Search and download preferences"),
    BotCommand(command="follow", description="Follow an artist by URL"),
    BotCommand(command="follows", description="Manage followed artists"),
    BotCommand(command="unfollow", description="Unfollow artists"),
]


async def register_bot_commands(bot: Bot) -> None:
    """Install the command menu without making it a polling prerequisite."""
    try:
        await bot.set_my_commands(BOT_COMMANDS)
    except TelegramAPIError as exc:
        logging.getLogger(__name__).warning(
            "Bot command registration failed | error=%s", exc
        )


def create_dispatcher(session: Session) -> Dispatcher:
    dispatcher = Dispatcher()
    dispatcher.include_router(commands_router)
    dispatcher.include_router(settings_router)
    dispatcher.include_router(follows_router)
    dispatcher.include_router(catalogue_router)
    dispatcher.include_router(inline_router)
    dispatcher.include_router(intake_router)
    dispatcher.include_router(callbacks_router)
    dispatcher.workflow_data.update(session=session)
    return dispatcher


async def run() -> None:
    setup_logging()
    settings = Settings.from_env()
    settings.ensure_directories()
    api_server: TelegramAPIServer | None = None
    if settings.telegram_api_url:
        api_server = TelegramAPIServer.from_base(
            settings.telegram_api_url, is_local=True
        )
        logging.getLogger(__name__).info(
            "Using local Bot API server | url=%s", settings.telegram_api_url
        )

    telegram_session: AiohttpSession | None = None
    if settings.telegram_proxy and not api_server:
        telegram_session = AiohttpSession(
            proxy=settings.telegram_proxy,
            api=PRODUCTION,
            timeout=600,
        )
        logging.getLogger(__name__).info(
            "Telegram session routed through proxy | proxy=%s",
            settings.telegram_proxy,
        )
    elif api_server:
        # A self-hosted Bot API server is typically reachable directly
        # (localhost/tailnet); the proxy would only break that route.
        # 600 s: media-group uploads of multi-hundred-MB FLACs over LAN
        # need well over aiogram's 60 s default.
        telegram_session = AiohttpSession(api=api_server, timeout=600)

    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        session=telegram_session,
    )
    await register_bot_commands(bot)

    session = build_session(settings, MornyeService(settings))
    try:
        me = await bot.get_me()
        if me.username:
            session.identity.mention = f"@{me.username}"
    except TelegramAPIError as exc:
        logging.getLogger(__name__).warning(
            "Bot identity unavailable | error=%s", exc
        )

    session.request_store.sweep_stale()
    maintenance_task = None
    if session.mornye.enabled:
        await session.mornye.warm_up()
        maintenance_task = asyncio.create_task(
            session.mornye.run_signed_session_maintenance(),
            name="mornye-session-maintenance",
        )
    dispatcher = create_dispatcher(session)

    logging.getLogger(__name__).info(
        "Bot is starting | download_dir=%s requests_dir=%s proxy=%s cooldown=%ss",
        settings.download_location,
        settings.requests_dir,
        "enabled" if settings.http_proxy else "disabled",
        settings.request_cooldown_seconds,
    )
    follow_task = asyncio.create_task(FollowPoller(session, bot).run())
    try:
        await dispatcher.start_polling(bot)
    finally:
        for task in (follow_task, maintenance_task):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        await session.mornye.close()
