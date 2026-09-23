import sys
import asyncio

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode

import config
from database import init_db
from handlers import router, provider
from balance_watcher import watch_openrouter_balance


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    logger = logging.getLogger(__name__)

    logger.info("Инициализация базы данных...")
    await init_db()

    async with Bot(
        token=config.TELEGRAM_BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        session=AiohttpSession(proxy=config.TELEGRAM_PROXY_URL),
    ) as bot:
        dp = Dispatcher()
        dp.include_router(router)

        if config.TELEGRAM_PROXY_URL:
            logger.info("Бот запускается через локальный прокси...")
        else:
            logger.info("Бот запускается (polling) без прокси...")

        watcher_task = asyncio.create_task(
            watch_openrouter_balance(provider._client, config.OPENROUTER_MANAGEMENT_KEY, bot)
        )

        try:
            await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
        finally:
            watcher_task.cancel()
            aclose = getattr(provider, "aclose", None)
            if aclose is not None:
                await aclose()


if __name__ == "__main__":
    asyncio.run(main())