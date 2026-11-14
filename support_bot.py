import asyncio
import logging
import html
import time
from aiogram.types import ErrorEvent
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandStart
from aiogram.types import Message
from sqlalchemy import select
from collections import defaultdict
import config
from database import SupportTicket, User, async_session
from user_service import get_or_create_user

logger = logging.getLogger(__name__)

support_router = Router(name="support")


@support_router.message(CommandStart(), F.chat.type == "private")
async def on_start(message: Message) -> None:
    await message.answer(
        "Здравствуйте! Опишите проблему одним или несколькими сообщениями, "
        "мы ответим вам здесь."
    )


_TOPIC_GONE_MARKERS = ("thread not found", "topic_id_invalid", "topic_deleted")
_user_locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
_last_alert_at = 0.0
ALERT_COOLDOWN_SECONDS = 60

def _topic_gone(error: TelegramBadRequest) -> bool:
    text = str(error).lower()
    return any(marker in text for marker in _TOPIC_GONE_MARKERS)


async def _create_topic(bot: Bot, tg) -> int:
    name = f"{tg.full_name} · {tg.id}"[:128]
    topic = await bot.create_forum_topic(config.SUPPORT_GROUP_ID, name=name)
    return topic.message_thread_id


@support_router.message(F.chat.type == "private")
async def from_user(message: Message, bot: Bot) -> None:
    tg = message.from_user
    user = await get_or_create_user(tg.id, tg.username)

    async with _user_locks[tg.id]:
        async with async_session() as session:
            ticket = await session.scalar(
                select(SupportTicket).where(SupportTicket.user_id == user.id)
            )
            if ticket is None:
                topic_id = await _create_topic(bot, tg)
                session.add(SupportTicket(user_id=user.id, topic_id=topic_id))
                await session.commit()
                await message.copy_to(config.SUPPORT_GROUP_ID, message_thread_id=topic_id)
                return

            topic_id = ticket.topic_id
            try:
                if ticket.status == "closed":
                    try:
                        await bot.reopen_forum_topic(config.SUPPORT_GROUP_ID, topic_id)
                    except TelegramBadRequest as e:
                        if "not_modified" not in str(e).lower():
                            raise
                    ticket.status = "open"
                await message.copy_to(config.SUPPORT_GROUP_ID, message_thread_id=topic_id)
            except TelegramBadRequest as e:
                if not _topic_gone(e):
                    raise
                logger.warning("Тема %s удалена, создаю новую для %s", topic_id, tg.id)
                topic_id = await _create_topic(bot, tg)
                ticket.topic_id = topic_id
                ticket.status = "open"
                await session.commit()
                await message.copy_to(config.SUPPORT_GROUP_ID, message_thread_id=topic_id)
            await session.commit()


@support_router.message(
    Command("close"),
    F.chat.id == config.SUPPORT_GROUP_ID,
    F.message_thread_id,
)
async def close_ticket(message: Message, bot: Bot) -> None:
    async with async_session() as session:
        row = (
            await session.execute(
                select(SupportTicket, User.telegram_id)
                .join(User, User.id == SupportTicket.user_id)
                .where(SupportTicket.topic_id == message.message_thread_id)
            )
        ).first()
        if row is None:
            return
        ticket, telegram_id = row
        ticket.status = "closed"
        await session.commit()

    await bot.close_forum_topic(config.SUPPORT_GROUP_ID, message.message_thread_id)

    try:
        await bot.send_message(
            telegram_id,
            "Обращение закрыто. Если вопрос остался, просто напишите нам снова.",
        )
    except (TelegramForbiddenError, TelegramBadRequest):
        logger.info("Не удалось уведомить пользователя %s о закрытии", telegram_id)


@support_router.message(
    F.chat.id == config.SUPPORT_GROUP_ID,
    F.message_thread_id,
    ~F.from_user.is_bot,
)
async def from_support(message: Message) -> None:
    async with async_session() as session:
        telegram_id = await session.scalar(
            select(User.telegram_id)
            .join(SupportTicket, SupportTicket.user_id == User.id)
            .where(SupportTicket.topic_id == message.message_thread_id)
        )
    if telegram_id is None:
        return

    try:
        await message.copy_to(telegram_id)
    except TelegramForbiddenError:
        await message.reply("⚠️ Пользователь заблокировал бота, ответ не доставлен.")
    except TelegramBadRequest:
        logger.warning(
            "Не удалось доставить сообщение в тему %s", message.message_thread_id
        )


@support_router.errors()
async def on_error(event: ErrorEvent, bot: Bot) -> bool:
    global _last_alert_at

    exc = event.exception
    logger.error("Ошибка в боте поддержки", exc_info=exc)

    message = event.update.message
    private_message = message is not None and message.chat.type == "private"

    # пользователю: коротко и без подробностей
    if private_message:
        try:
            await message.answer(
                "Не удалось отправить сообщение в поддержку. Попробуйте чуть позже."
            )
        except Exception:
            pass

    # тебе: не чаще раза в минуту, чтобы не засыпало при массовом сбое
    now = time.monotonic()
    if now - _last_alert_at < ALERT_COOLDOWN_SECONDS:
        return True
    _last_alert_at = now

    who = f"\nПользователь: <code>{message.from_user.id}</code>" if private_message else ""
    text = (
        "⚠️ Ошибка в боте поддержки"
        f"{who}\n<code>{html.escape(type(exc).__name__)}: {html.escape(str(exc))[:800]}</code>"
    )
    try:
        await bot.send_message(config.ADMIN_ID, text)
    except Exception:
        logger.warning("Не удалось отправить уведомление об ошибке админу")
    return True

async def run_support_bot() -> None:
    try:
        async with Bot(
            token=config.SUPPORT_BOT_TOKEN,
            default=DefaultBotProperties(parse_mode=ParseMode.HTML),
            session=AiohttpSession(proxy=config.TELEGRAM_PROXY_URL),
        ) as bot:
            dp = Dispatcher()
            dp.include_router(support_router)
            logger.info("Бот поддержки запускается (polling)...")
            await dp.start_polling(
                bot,
                allowed_updates=dp.resolve_used_update_types(),
                handle_signals=False,
            )
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("Бот поддержки упал")