import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.methods import GetMe
from aiogram.exceptions import TelegramAPIError

from app import register_bot_commands


@pytest.mark.asyncio
async def test_command_registration_failure_is_nonfatal(caplog):
    """A command-menu API failure must not prevent polling startup."""
    bot = SimpleNamespace(
        set_my_commands=AsyncMock(side_effect=TelegramAPIError(GetMe(), "boom"))
    )

    with caplog.at_level(logging.WARNING, logger="app"):
        await register_bot_commands(bot)

    assert "Bot command registration failed" in caplog.text
