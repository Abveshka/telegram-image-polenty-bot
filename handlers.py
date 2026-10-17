import logging
from aiogram import Router, F
from aiogram.types import ErrorEvent, Message
from aiogram.filters import CommandStart
from aiogram.types import BufferedInputFile
import uuid
from user_service import start_generation, finish_generation
from admin_alerts import notify_admin_once, resolve_admin_alert
import time
import config
import re
from user_service import (
    get_or_create_user,
    get_balance,
    charge_for_generation,
    credit_balance,
    reserve_payment_refund,
    undo_payment_refund,
)
from providers.nano_banana_provider import NanoBananaProvider
from aiogram.types import LabeledPrice, PreCheckoutQuery
from aiogram.filters import Command, CommandObject
from io import BytesIO
from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from providers.base import (
    ImageGenerationError,
    ProviderOutOfFundsError,
    ProviderTemporaryError,
)

ALERT_OUT_OF_FUNDS = "openrouter_out_of_funds"
ALERT_UNAVAILABLE = "openrouter_unavailable"

GENERATION_COOLDOWN_SECONDS = 10
_active_generations: set[int] = set()
_last_generation_at: dict[int, float] = {}

BUY_COOLDOWN_SECONDS = 10
_last_buy_at: dict[int, float] = {}

logger = logging.getLogger(__name__)

router = Router()

PACKAGES_BY_PAYLOAD = {f"stars_{p['credit_stars']}": p for p in config.STAR_PACKAGES}


provider = NanoBananaProvider(api_key=config.OPENROUTER_API_KEY)

@router.message(CommandStart())
async def handle_start(message: Message) -> None:
    if message.from_user is not None:
        await get_or_create_user(message.from_user.id, message.from_user.username)
    await message.answer(
        "Привет! Отправь мне /generate и текстовое описание, а я сгенерирую по нему изображение.\n\n"
        "Например: «/generate кот-космонавт на скейтборде»"
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

    gen_id = uuid.uuid4().hex[:8]
    telegram_id = message.from_user.id
    username = message.from_user.username
    with_photo = reference_image is not None
    await get_or_create_user(telegram_id, username)

    status_message = await message.answer("Генерирую изображение...")

    if not await charge_for_generation(telegram_id):
        balance = await get_balance(telegram_id)
        await status_message.edit_text(
            f"Недостаточно звёзд для генерации.\n"
            f"Ваш баланс: {balance} ★\n"
            f"Стоимость генерации: {config.PRICE_PER_GENERATION_STARS} ★\n\n"
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

@router.message(Command("generate"))
async def handle_generate_prompt(message: Message, command: CommandObject, bot: Bot) -> None:
    prompt = command.args

    if not prompt:
        await message.answer(
            "Укажите текст после команды, например:\n"
            "/generate кот-космонавт на скейтборде"
        )
        return

    if message.from_user is None:
        return

    telegram_id = message.from_user.id

    if telegram_id in _active_generations:
        await message.answer("Предыдущая генерация ещё выполняется. Дождитесь результата.")
        return

    elapsed = time.monotonic() - _last_generation_at.get(telegram_id, 0.0)
    if elapsed < GENERATION_COOLDOWN_SECONDS:
        wait = int(GENERATION_COOLDOWN_SECONDS - elapsed) + 1
        await message.answer(f"Слишком часто. Подождите {wait} сек.")
        return

    _active_generations.add(telegram_id)
    try:
        await _run_generation(message, prompt, bot)
    finally:
        _active_generations.discard(telegram_id)
        _last_generation_at[telegram_id] = time.monotonic()

@router.error()
async def log_update_error(event: ErrorEvent) -> bool:
    """Пишет полный traceback, чтобы ошибка апдейта не терялась в консоли."""
    logger.exception("Необработанная ошибка при обработке Telegram update", exc_info=event.exception)
    return True

@router.message(Command("buy"))
async def handle_buy(message: Message) -> None:
    if message.from_user is None:
        return

    user_id = message.from_user.id
    now = time.monotonic()
    last = _last_buy_at.get(user_id)
    if last is not None and now - last < BUY_COOLDOWN_SECONDS:
        await message.answer("Счета уже отправлены выше. Подождите немного перед повтором.")
        return
    _last_buy_at[user_id] = now

    for package in config.STAR_PACKAGES:
        await message.answer_invoice(
            title=package["label"],
            description=f"Пополнение баланса бота на {package['credit_stars']} ★",
            payload=f"stars_{package['credit_stars']}",
            currency="XTR",
            prices=[LabeledPrice(label=package["label"], amount=package["stars_to_pay"])],
        )


@router.pre_checkout_query()
async def handle_pre_checkout(query: PreCheckoutQuery) -> None:
    package = PACKAGES_BY_PAYLOAD.get(query.invoice_payload)
    if (
        package is None
        or query.currency != "XTR"
        or query.total_amount != package["stars_to_pay"]
    ):
        await query.answer(ok=False, error_message="Пакет недоступен, откройте /buy заново")
        return
    await query.answer(ok=True)


@router.message(F.successful_payment)
async def handle_successful_payment(message: Message) -> None:
    if message.from_user is None or message.successful_payment is None:
        return

    payment = message.successful_payment  # <-- добавлено
    payload = payment.invoice_payload
    credit_stars = int(payload.replace("stars_", ""))

    try:
        await credit_balance(
            telegram_id=message.from_user.id,
            username=message.from_user.username,
            stars_amount=credit_stars,
            charge_id=payment.telegram_payment_charge_id,
        )
    except Exception:
        logger.critical(
            "ОПЛАТА НЕ НАЧИСЛЕНА: user=%s, кредиты=%s, charge_id=%s",
            message.from_user.id, credit_stars, payment.telegram_payment_charge_id,
            exc_info=True,
        )
        await message.answer(
            "Оплата получена, но при начислении произошёл сбой. "
            "Мы разберёмся и начислим вручную."
        )
        return

    balance = await get_balance(message.from_user.id)
    await message.answer(
        f"Оплата прошла успешно! Начислено {credit_stars} ★.\n"
        f"Текущий баланс: {balance} ★"
    )

@router.message(Command("balance"))
async def handle_balance(message: Message) -> None:
    if message.from_user is None:
        return
    await get_or_create_user(message.from_user.id, message.from_user.username)
    balance = await get_balance(message.from_user.id)
    await message.answer(
        f"Баланс: {balance} ★ (≈ {balance // config.PRICE_PER_GENERATION_STARS} генераций)"
    )

@router.message(Command("refund"))
async def handle_refund(message: Message, command: CommandObject, bot: Bot) -> None:
    # Только для вас: иначе любой пользователь сможет вернуть чужие платежи
    if message.from_user is None or message.from_user.id != config.ADMIN_ID:
        return

    if not command.args:
        await message.answer("Использование: /refund <charge_id>")
        return

    charge_id = command.args.strip()

    try:
        telegram_id, amount = await reserve_payment_refund(charge_id)
    except ValueError as error:
        await message.answer(f"Возврат невозможен: {error}")
        return

    try:
        await bot.refund_star_payment(
            user_id=telegram_id,
            telegram_payment_charge_id=charge_id,
        )
    except TelegramAPIError as error:
        logger.exception("Telegram не вернул звёзды по платежу %s", charge_id)
        await undo_payment_refund(charge_id, telegram_id, amount)
        await message.answer(f"Telegram отклонил возврат, баланс восстановлен: {error}")
        return

    await message.answer(f"Готово: звёзды возвращены, с баланса списано {amount} ★")

@router.message(Command("compensate"))
async def handle_compensate(message: Message, command: CommandObject) -> None:
    """Начисляет звёзды в качестве компенсации за сбой по нашей вине."""
    if message.from_user is None or message.from_user.id != config.ADMIN_ID:
        return

    usage = (
        "Использование: /compensate <telegram_id> <количество> [код_обращения]\n"
        "Например: /compensate 1498757244 10 a1b2c3d4"
    )
    if not command.args:
        await message.answer(usage)
        return

    parts = command.args.split()
    if len(parts) not in (2, 3) or not all(p.lstrip("-").isdigit() for p in parts[:2]):
        await message.answer(usage)
        return

    target_id, amount = int(parts[0]), int(parts[1])
    if amount <= 0:
        await message.answer("Количество должно быть положительным")
        return

    gen_id = parts[2].lower() if len(parts) == 3 else None
    if gen_id is not None and not re.fullmatch(r"[0-9a-f]{8}", gen_id):
        await message.answer("Код обращения должен состоять из 8 символов (0-9, a-f)")
        return

    if gen_id is not None:
        charge_id = f"compensation_{gen_id}"
    else:
        # Без кода защиты от повтора нет
        charge_id = f"compensation_{target_id}_{int(time.time())}"

    credited = await credit_balance(
        telegram_id=target_id,
        username=None,
        stars_amount=amount,
        charge_id=charge_id,
        tx_type="compensation",
    )
    if not credited:
        await message.answer(f"Генерация {gen_id} уже была компенсирована, повторно не начисляю.")
        return

    logger.info("Компенсация: user=%s amount=%s gen=%s", target_id, amount, gen_id)

    balance = await get_balance(target_id)
    await message.answer(f"Начислено {amount} ★ юзеру {target_id} (компенсация).\nЕго баланс теперь: {balance} ★")

    try:
        await message.bot.send_message(
            target_id,
            f"Приносим извинения за техническую проблему с генерацией. "
            f"Вам начислено {amount} ★ в качестве компенсации.",
        )
    except Exception:
        logger.warning("Не удалось уведомить юзера %s о компенсации", target_id)


@router.message(F.photo)
async def handle_photo_prompt(message: Message, bot: Bot) -> None:
    prompt = message.caption

    if not prompt:
        await message.answer(
            "Добавьте описание к фотографии в поле подписи, "
            "например: «Сделай в стиле акварели»."
        )
        return

    if message.from_user is None:
        return

    telegram_id = message.from_user.id

    # Фото должно соблюдать тот же лимит, что и команда /generate.
    if telegram_id in _active_generations:
        await message.answer("Предыдущая генерация ещё выполняется. Дождитесь результата.")
        return

    elapsed = time.monotonic() - _last_generation_at.get(telegram_id, 0.0)
    if elapsed < GENERATION_COOLDOWN_SECONDS:
        wait = int(GENERATION_COOLDOWN_SECONDS - elapsed) + 1
        await message.answer(f"Слишком часто. Подождите {wait} сек.")
        return

    _active_generations.add(telegram_id)
    try:
        photo = message.photo[-1]
        buffer = BytesIO()
        try:
            await bot.download(photo.file_id, destination=buffer)
        except Exception:
            logger.exception("Не удалось скачать фотографию пользователя %s", telegram_id)
            await message.answer("Не удалось скачать фотографию. Попробуйте отправить её ещё раз.")
            return

        reference_image = buffer.getvalue()
        if not reference_image:
            await message.answer("Не удалось скачать фотографию. Попробуйте отправить её ещё раз.")
            return

        await _run_generation(
            message,
            prompt,
            bot,
            reference_image=reference_image,
            media_type="image/jpeg",
        )
    finally:
        _active_generations.discard(telegram_id)
        _last_generation_at[telegram_id] = time.monotonic()
