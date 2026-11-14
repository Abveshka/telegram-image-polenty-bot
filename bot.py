import sys
import asyncio
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
from subscription_check import SubscriptionMiddleware
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
from subscription_worker import subscription_worker
import yookassa_client
from support_bot import run_support_bot
import os

LOG_DIR = os.getenv("LOG_DIR", "logs")
os.makedirs(LOG_DIR, exist_ok=True)

file_handler = TimedRotatingFileHandler(
    os.path.join(LOG_DIR, "bot.log"),
    when="midnight",
    backupCount=14,
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
        BotCommand(command="subscription", description="💳 Подписка"),
        BotCommand(command="generate", description="🎨 Генерация изображений"),
        BotCommand(command="buy", description="⭐ Пополнить баланс"),
        BotCommand(command="balance", description="💰 Мой баланс"),
        BotCommand(command="support", description="⚒️ Техподдержка"),
    ]
    await bot.set_my_commands(user_commands)

    admin_commands = user_commands + [
        BotCommand(
            command="refund",
            description="↩️ Возврат ЮKassa: /refund <payment_id> [сумма] [revoke]",
        ),
        BotCommand(
            command="refund_stars",
            description="↩️ Возврат звёзд за токены: /refund_stars <charge_id>",
        ),
        BotCommand(
            command="refund_stars_sub",
            description="↩️ Возврат звёзд за подписку: /refund_stars_sub <charge_id> [revoke]",
        ),
        BotCommand(
            command="compensate",
            description="🎁 Компенсация: /compensate <telegram_id> <кол-во> [код_обращения]",
        ),
        BotCommand(
            command="grant_sub",
            description="✅ Выдать подписку (ЮKassa): /grant_sub <payment_id>",
        ),
        BotCommand(
            command="grant_stars_sub",
            description="✅ Выдать подписку (звёзды): /grant_stars_sub <charge_id>",
        ),
    ]
    await bot.set_my_commands(
        admin_commands,
        scope=BotCommandScopeChat(chat_id=config.ADMIN_ID),
    )

async def main() -> None:
    logger = logging.getLogger(__name__)

    async with Bot(
        token=config.TELEGRAM_BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        session=AiohttpSession(proxy=config.TELEGRAM_PROXY_URL),
    ) as bot:
        dp = Dispatcher()
        dp.message.outer_middleware(SubscriptionMiddleware())
        dp.callback_query.outer_middleware(SubscriptionMiddleware())
        dp.include_router(router)
        await setup_commands(bot)
        if config.TELEGRAM_PROXY_URL:
            logger.info("Бот запускается через локальный прокси...")
        else:
            logger.info("Бот запускается (polling) без прокси...")

        watcher_task = asyncio.create_task(
            watch_openrouter_balance(provider._client, config.OPENROUTER_MANAGEMENT_KEY, bot)
        )
        sub_task = asyncio.create_task(subscription_worker(bot))
        support_task = asyncio.create_task(run_support_bot())
        try:
            await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
        finally:
            watcher_task.cancel()
            sub_task.cancel()
            support_task.cancel()
            await yookassa_client.aclose()
            aclose = getattr(provider, "aclose", None)
            if aclose is not None:
                await aclose()


if __name__ == "__main__":
    asyncio.run(main())