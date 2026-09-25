from __future__ import annotations

import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import PRODUCTION, TelegramAPIServer
from aiogram.enums import ParseMode

from config import Settings
from utils.logging_config import setup_logging
from routers.callbacks import router as callbacks_router
from routers.commands import router as commands_router
from routers.intake import router as intake_router
from routers.inline import router as inline_router
from routers.thumbnails import router as thumbnails_router
from services.cooldown import CooldownManager
from services.media_cache import MediaCache
from services.request_store import RequestStore
from services.thumbnail_store import ThumbnailStore


def create_dispatcher(settings: Settings) -> Dispatcher:
    dispatcher = Dispatcher()
    dispatcher.include_router(commands_router)
    dispatcher.include_router(thumbnails_router)
    dispatcher.include_router(inline_router)
    dispatcher.include_router(intake_router)
    dispatcher.include_router(callbacks_router)
    dispatcher.workflow_data.update(
        settings=settings,
        cooldown=CooldownManager(timeout_seconds=settings.request_cooldown_seconds),
        request_store=RequestStore(settings.requests_dir, settings.work_dir),
        thumbnail_store=ThumbnailStore(settings.thumbnails_dir),
        media_cache=MediaCache(settings.media_cache_file),
    )
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

    session: AiohttpSession | None = None
    if settings.telegram_proxy and not api_server:
        session = AiohttpSession(
            proxy=settings.telegram_proxy, api=PRODUCTION
        )
        logging.getLogger(__name__).info(
            "Telegram session routed through proxy | proxy=%s",
            settings.telegram_proxy,
        )
    elif api_server:
        # A self-hosted Bot API server is typically reachable directly
        # (localhost/tailnet); the proxy would only break that route.
        session = AiohttpSession(api=api_server)


    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        session=session,
    )

    request_store = RequestStore(settings.requests_dir, settings.work_dir)
    request_store.sweep_stale()
    dispatcher = create_dispatcher(settings)

    logging.getLogger(__name__).info(
        "Bot is starting | download_dir=%s requests_dir=%s proxy=%s cooldown=%ss",
        settings.download_location,
        settings.requests_dir,
        "enabled" if settings.http_proxy else "disabled",
        settings.request_cooldown_seconds,
    )
    await dispatcher.start_polling(bot)
