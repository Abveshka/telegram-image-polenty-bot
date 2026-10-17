import sys
import asyncio
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
import logging
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.types import BotCommand, BotCommandScopeChat
import config
from handlers import router, provider
from balance_watcher import watch_openrouter_balance
import logging
from logging.handlers import TimedRotatingFileHandler

file_handler = TimedRotatingFileHandler(
    "logs/bot.log",
    when="midnight",   # новый файл каждый день
    backupCount=14,    # хранить последние 14 дней, старые удаляются сами
    encoding="utf-8",
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[logging.StreamHandler(), file_handler],
)

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("aiogram.event").setLevel(logging.WARNING)

async def setup_commands(bot: Bot) -> None:
    user_commands = [
        BotCommand(command="start", description="🔄 Перезапуск"),
        BotCommand(command="menu", description="🏠 Главное меню"),
        BotCommand(command="generate", description="🎨 Генерация изображений"),
        BotCommand(command="buy", description="⭐ Пополнить баланс"),
        BotCommand(command="balance", description="💰 Мой баланс"),
    ]
    await bot.set_my_commands(user_commands)

    admin_commands = user_commands + [
        BotCommand(command="refund", description="↩️ Возврат платежа. Использование: /refund <charge_id>"),
        BotCommand(command="compensate", description="🎁 Компенсация. Использование: /compensate <telegram_id> <количество> [код_обращения]"),
    ]
    await bot.set_my_commands(
        admin_commands,
        scope=BotCommandScopeChat(chat_id=config.ADMIN_ID),
    )

async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    logger = logging.getLogger(__name__)

    async with Bot(
        token=config.TELEGRAM_BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        session=AiohttpSession(proxy=config.TELEGRAM_PROXY_URL),
    ) as bot:
        dp = Dispatcher()
        dp.include_router(router)
        await setup_commands(bot)
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