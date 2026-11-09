import logging
from aiogram import Bot, BaseMiddleware
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
)
import asyncio
from handlers.menu import handle_start
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.exceptions import TelegramRetryAfter
import time
from aiogram.enums import ChatMemberStatus
import config

logger = logging.getLogger(__name__)

OK_STATUSES = {
    ChatMemberStatus.MEMBER,
    ChatMemberStatus.ADMINISTRATOR,
    ChatMemberStatus.CREATOR,
}

CHECK_CB = "check_subscription"

_ok_cache: dict[int, float] = {}
CACHE_TTL = 1800  # 30 минут


def is_cached_ok(user_id: int) -> bool:
    return _ok_cache.get(user_id, 0) > time.time()


def mark_ok(user_id: int) -> None:
    _ok_cache[user_id] = time.time() + CACHE_TTL

_sem = asyncio.Semaphore(15)


async def _is_missing(bot: Bot, ch: dict, user_id: int):
    async with _sem:
        try:
            member = await bot.get_chat_member(ch["id"], user_id)
            return None if member.status in OK_STATUSES else ch
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after)
            return await _is_missing(bot, ch, user_id)
        except Exception as e:
            logger.error("Не удалось проверить канал %s: %s", ch["id"], e)
            return None


async def get_unsubscribed(bot: Bot, user_id: int) -> list[dict]:
    results = await asyncio.gather(
        *(_is_missing(bot, ch, user_id) for ch in config.REQUIRED_CHANNELS)
    )
    return [ch for ch in results if ch is not None]


def build_kb(channels: list[dict]) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=f"📢 {c['title']}", url=c["url"])] for c in channels]
    rows.append([InlineKeyboardButton(text="✅ Я подписался", callback_data=CHECK_CB)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


TEXT = "Чтобы пользоваться ботом, подпишитесь на каналы ниже и нажмите «Я подписался»:"

class SubscriptionMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        bot: Bot = data["bot"]
        user = data.get("event_from_user")

        if user is None:  # or user.id == config.ADMIN_ID:
            return await handler(event, data)

        # оплату пропускаем всегда
        if isinstance(event, Message) and event.successful_payment:
            return await handler(event, data)

        # кнопка «Я подписался»
        if isinstance(event, CallbackQuery) and event.data == CHECK_CB:
            missing = await get_unsubscribed(bot, user.id)
            try:
                if missing:
                    await event.answer("Вы ещё не подписались на все каналы", show_alert=True)
                else:
                    await event.answer("Спасибо! Доступ открыт ✅")
            except TelegramBadRequest as e:
                logger.warning("answer не прошёл (устарел запрос?): %s", e)

            if missing:
                try:
                    await event.message.edit_reply_markup(reply_markup=build_kb(missing))
                except TelegramBadRequest:
                    pass  # «message is not modified»
            else:
                mark_ok(user.id)

                # собираем «сообщение /start» от пользователя
                fake_msg = event.message.model_copy(
                    update={"from_user": event.from_user, "text": "/start"}
                ).as_(bot)

                try:
                    await event.message.delete()
                except TelegramBadRequest:
                    pass

                await handle_start(fake_msg)
            return

        # обычные действия: сначала кэш
        if is_cached_ok(user.id):
            return await handler(event, data)

        missing = await get_unsubscribed(bot, user.id)
        if not missing:
            mark_ok(user.id)
            return await handler(event, data)

        if isinstance(event, Message):
            await event.answer(TEXT, reply_markup=build_kb(missing))
        elif isinstance(event, CallbackQuery):
            await event.answer()
            await event.message.answer(TEXT, reply_markup=build_kb(missing))