import logging
import time
import uuid
from io import BytesIO
from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import BufferedInputFile, CallbackQuery, Message
import config
from admin_alerts import notify_admin_once, resolve_admin_alert
from handlers.keyboards import main_menu_keyboard
from providers.base import (
    ImageGenerationError, ProviderOutOfFundsError, ProviderTemporaryError,
)
from providers.nano_banana_provider import NanoBananaProvider
from user_service import (
    charge_for_generation, finish_generation, get_balance,
    get_or_create_user, start_generation,
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

logger = logging.getLogger(__name__)
router = Router(name="generation")

provider = NanoBananaProvider(api_key=config.OPENROUTER_API_KEY)

ALERT_OUT_OF_FUNDS = "openrouter_out_of_funds"
ALERT_UNAVAILABLE = "openrouter_unavailable"

GENERATION_COOLDOWN_SECONDS = 10
_active_generations: set[int] = set()
_last_generation_at: dict[int, float] = {}

class Gen(StatesGroup):
    waiting_input = State()


def cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="gen:cancel")]]
    )

def log_failed_generation(
    gen_id: str, telegram_id: int, username: str | None, prompt: str, error: Exception | str
) -> None:
    """Пишет в лог данные для ручной сверки. Деньги не возвращает."""
    logger.error(
        "СБОЙ ГЕНЕРАЦИИ (попытка списана): gen=%s user=%s username=%s prompt=%r error=%r",
        gen_id, telegram_id, username, prompt, error,
    )

async def _safe_start_generation(
    gen_id: str, telegram_id: int, prompt: str, with_photo: bool
) -> None:
    try:
        await start_generation(gen_id, telegram_id, prompt, with_photo)
    except Exception:
        logger.exception("Не удалось записать старт генерации: gen=%s", gen_id)


async def _safe_finish_generation(
    gen_id: str, status: str, error: Exception | None = None
) -> None:
    try:
        await finish_generation(gen_id, status, error)
    except Exception:
        logger.exception("Не удалось записать итог генерации: gen=%s status=%s", gen_id, status)


async def _run_generation(
    message: Message,
    prompt: str,
    bot: Bot,
    reference_image: bytes | None = None,
    media_type: str = "image/jpeg",
) -> None:
    if message.from_user is None:
        return

    if not prompt or not prompt.strip():
        await message.answer("Пустое описание. Отправьте текст запроса.")
        return

    gen_id = uuid.uuid4().hex[:8]
    telegram_id = message.from_user.id
    username = message.from_user.username
    with_photo = reference_image is not None
    await get_or_create_user(telegram_id, username)

    status_message = await message.answer("Генерирую изображение...")

    if not await charge_for_generation(telegram_id):
        balance = await get_balance(telegram_id)
        await status_message.edit_text(
            f"Недостаточно токенов для генерации.\n"
            f"Ваш баланс: {balance} токенов\n"
            f"Стоимость генерации: {config.PRICE_PER_GENERATION_STARS} токенов\n\n"
            "Пополнить баланс: /buy"
        )
        return

    # Звёзды списаны, с этого момента ведём журнал
    await _safe_start_generation(gen_id, telegram_id, prompt, with_photo)
    logger.info(
        "Генерация началась: gen=%s user=%s with_photo=%s prompt=%r",
        gen_id, telegram_id, with_photo, prompt,
    )

    try:
        image_bytes = await provider.generate(
            prompt,
            reference_image=reference_image,
            media_type=media_type,
        )
    except ProviderOutOfFundsError as error:
        log_failed_generation(gen_id, telegram_id, username, prompt, error)
        await _safe_finish_generation(gen_id, "failed", error)
        await notify_admin_once(
            bot, ALERT_OUT_OF_FUNDS,
            "🚨 На OpenRouter кончились средства, генерации не работают!",
        )
        await status_message.edit_text(
            f"Сервис временно недоступен. Администратор уведомлён.\n"
            f"Код обращения: {gen_id}"
        )
        return
    except ProviderTemporaryError as error:
        log_failed_generation(gen_id, telegram_id, username, prompt, error)
        await _safe_finish_generation(gen_id, "failed", error)
        await notify_admin_once(
            bot, ALERT_UNAVAILABLE,
            "⚠️ OpenRouter временно недоступен (429/5xx/сеть).",
        )
        await status_message.edit_text(
            f"Сервис сейчас перегружен. Попробуйте чуть позже.\n"
            f"Код обращения: {gen_id}"
        )
        return
    except ImageGenerationError as error:
        log_failed_generation(gen_id, telegram_id, username, prompt, error)
        await _safe_finish_generation(gen_id, "failed", error)
        await status_message.edit_text(
            f"Не получилось сгенерировать изображение. "
            f"Попробуйте ещё раз или измените запрос.\n"
            f"Код обращения: {gen_id}"
        )
        return
    except Exception as error:
        logger.exception("Неожиданная ошибка: gen=%s user=%s", gen_id, telegram_id)
        log_failed_generation(gen_id, telegram_id, username, prompt, error)
        await _safe_finish_generation(gen_id, "failed", error)
        await notify_admin_once(
            bot, "unexpected_generation_error",
            f"⚠️ Неожиданная ошибка генерации (gen={gen_id}): {error!r}",
        )
        await status_message.edit_text(
            f"Произошла техническая ошибка при генерации. "
            f"Администратор уведомлён.\nКод обращения: {gen_id}"
        )
        return
    else:
        # Картинка получена, значит и деньги на OpenRouter есть, и сервис жив
        resolve_admin_alert(ALERT_OUT_OF_FUNDS)
        resolve_admin_alert(ALERT_UNAVAILABLE)
        resolve_admin_alert("unexpected_generation_error")

    try:
        photo = BufferedInputFile(image_bytes, filename="generated.png")
        await message.answer_photo(photo, caption=f"«{prompt}»")
    except Exception as error:
        log_failed_generation(gen_id, telegram_id, username, prompt, error)
        await _safe_finish_generation(gen_id, "delivery_failed", error)
        await status_message.edit_text(
            f"Изображение создано, но Telegram не смог его отправить.\n"
            f"Код обращения: {gen_id}"
        )
        return

    await _safe_finish_generation(gen_id, "success")
    logger.info("Генерация завершена: gen=%s user=%s", gen_id, telegram_id)
    await status_message.delete()


async def run_guarded(
    message: Message, prompt: str, bot: Bot, reference_image: bytes | None = None
) -> None:
    if message.from_user is None:
        return
    user_id = message.from_user.id

    refusal = check_limits(user_id)
    if refusal:
        await message.answer(refusal)
        return

    _active_generations.add(user_id)
    try:
        await _run_generation(message, prompt, bot, reference_image=reference_image)
    finally:
        _active_generations.discard(user_id)
        _last_generation_at[user_id] = time.monotonic()


def check_limits(user_id: int) -> str | None:
    """Возвращает текст отказа или None, если можно генерировать."""
    if user_id in _active_generations:
        return "Предыдущая генерация ещё выполняется. Дождитесь результата."

    elapsed = time.monotonic() - _last_generation_at.get(user_id, 0.0)
    if elapsed < GENERATION_COOLDOWN_SECONDS:
        wait = int(GENERATION_COOLDOWN_SECONDS - elapsed) + 1
        return f"Слишком часто. Подождите {wait} сек."

    return None

@router.message(Command("generate"))
async def handle_generate_prompt(
    message: Message, command: CommandObject, bot: Bot, state: FSMContext
) -> None:
    if not command.args:
        await state.set_state(Gen.waiting_input)
        await message.answer(
            "🎨 <b>Генерация изображений</b>\n\n"
            "Выбрана модель: Nano Banana 2.1\n"
            f"Стоимость: {config.PRICE_PER_GENERATION_STARS} токенов\n\n"
            "Отправьте описание текстом или фото с подписью одним сообщением.",
            reply_markup=cancel_keyboard(),
        )
        return

    await run_guarded(message, command.args, bot)


@router.callback_query(F.data == "menu:generate")
async def cb_generate(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    if callback.message is None:
        return
    await state.set_state(Gen.waiting_input)
    await callback.message.answer(
        "🎨 <b>Генерация изображений</b>\n\n"
        "Выбрана модель: Nano Banana 2.1\n"
        f"Стоимость: {config.PRICE_PER_GENERATION_STARS} токенов\n\n"
        "Отправьте описание текстом или фото с подписью одним сообщением.",
        reply_markup=cancel_keyboard(),
    )


@router.callback_query(F.data == "gen:cancel")
async def cb_gen_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.answer("Отменено")
    if callback.message is not None:
        await callback.message.edit_text(
            "Генерация отменена",
            reply_markup=main_menu_keyboard(),
        )


# Текст принимается только в режиме ожидания и только если это не команда
@router.message(Gen.waiting_input, F.text, ~F.text.startswith("/"))
async def got_text_prompt(message: Message, state: FSMContext, bot: Bot) -> None:
    if message.from_user is None or message.text is None:
        return
    refusal = check_limits(message.from_user.id)
    if refusal:
        await message.answer(refusal)   # состояние сохраняем, можно повторить
        return
    await state.clear()
    await run_guarded(message, message.text, bot)


# Фото работает и в режиме ожидания, и само по себе (но обязательно с подписью)
@router.message(F.photo)
async def handle_photo_prompt(message: Message, state: FSMContext, bot: Bot) -> None:
    if message.from_user is None:
        return

    prompt = message.caption
    if not prompt:
        await message.answer(
            "Вы отправили фото без описания. Добавьте описание и прикрепите фото заново!",
            reply_markup=cancel_keyboard() if await state.get_state() else None,
        )
        return

    refusal = check_limits(message.from_user.id)
    if refusal:
        await message.answer(refusal)
        return

    buffer = BytesIO()
    try:
        await bot.download(message.photo[-1].file_id, destination=buffer)
    except Exception:
        logger.exception("Не удалось скачать фотографию")
        await message.answer("Не удалось скачать фотографию. Попробуйте отправить её ещё раз.")
        return

    reference_image = buffer.getvalue()
    if not reference_image:
        await message.answer("Не удалось скачать фотографию. Попробуйте отправить её ещё раз.")
        return

    await state.clear()
    await run_guarded(message, prompt, bot, reference_image=reference_image)