import json
import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, LabeledPrice, Message, PreCheckoutQuery

import config
from user_service import credit_balance, get_balance
from .keyboards import (
    buy_menu_keyboard,
    packages_keyboard,
    payment_menu_keyboard,
    payment_method_keyboard,
)
from .menu import build_menu_menu

logger = logging.getLogger(__name__)
router = Router(name="payments")

# Пакеты по числу токенов: {40: {...}, 100: {...}}, чтобы быстро находить пакет
PACKAGES_BY_CREDIT = {p["credit_stars"]: p for p in config.STAR_PACKAGES}

# ───────────── Тексты экранов ─────────────

BUY_TEXT = (
    "❤️ Пополните баланс, чтобы пользоваться ботом без ограничений!\n\n"
    "Что я умею?\n"
    "🔹 <b>Генерирую фото</b> - пришли описание\n"
    "🔹 <b>Редактирую фото</b> - пришли картинку с подписью\n\n"
    "<i>Оплатить можно банковской картой или с помощью Telegram Stars</i>"
)

PACKAGES_TEXT = "📦 Выберите комплект токенов"

METHOD_TEXT = "Выберите способ оплаты\n\nВы можете оплатить картой вашего банка или звёздами"


# ───────────── Вспомогательные функции ─────────────

def make_payload(kind: str, credit: int) -> str:
    """Собирает payload счёта: 'yk:100' (карта) или 'stars:100' (звёзды).
    Telegram вернёт его нам при оплате, пользователь его не видит."""
    return f"{kind}:{credit}"


def parse_payload(payload: str) -> tuple[str, int] | None:
    """Разбирает payload обратно в ('yk', 100). Если формат неверный, вернёт None."""
    try:
        kind, credit = payload.split(":", 1)
        if kind not in ("stars", "yk"):
            return None
        return kind, int(credit)
    except (ValueError, AttributeError):
        return None


def _package_from_callback(data: str, prefix: str) -> dict | None:
    """Достаёт пакет из callback-данных: 'buy:rub:100' с префиксом 'buy:rub:' -> пакет на 100."""
    try:
        return PACKAGES_BY_CREDIT.get(int(data.removeprefix(prefix)))
    except ValueError:
        return None


def _plural_generations(n: int) -> str:
    """Склоняет слово: 1 генерация, 4 генерации, 10 генераций."""
    if n % 10 == 1 and n % 100 != 11:
        return "генерация"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return "генерации"
    return "генераций"


def _item_name(package: dict) -> str:
    """Название товара для счёта и чека. Без эмодзи, их ОФД может не принять."""
    n = package["credit_stars"] // config.PRICE_PER_GENERATION_STARS
    return f"Баланс: {n} {_plural_generations(n)}"


# ───────────── Навигация по меню (только меняют экран) ─────────────

@router.message(Command("buy"))
async def handle_buy(message: Message) -> None:
    """Команда /buy: новое сообщение с меню покупки."""
    await message.answer(BUY_TEXT, reply_markup=buy_menu_keyboard())


@router.callback_query(F.data == "menu:buy")
async def cb_buy(callback: CallbackQuery) -> None:
    """Кнопка «Пополнить» в главном меню."""
    await callback.answer()
    if callback.message is None:
        return
    await callback.message.edit_text(BUY_TEXT, reply_markup=buy_menu_keyboard())


@router.callback_query(F.data == "buy:tokens")
async def cb_buy_tokens(callback: CallbackQuery) -> None:
    """«Купить токены»: показывает список пакетов."""
    await callback.answer()
    if callback.message is None:
        return
    await callback.message.edit_text(PACKAGES_TEXT, reply_markup=packages_keyboard())


@router.callback_query(F.data == "buy:menu")
async def cb_buy_back_to_menu(callback: CallbackQuery) -> None:
    """«Назад» из списка пакетов в меню покупки."""
    await callback.answer()
    if callback.message is None:
        return
    await callback.message.edit_text(BUY_TEXT, reply_markup=buy_menu_keyboard())


@router.callback_query(F.data == "buy:main")
async def cb_buy_back_to_main(callback: CallbackQuery) -> None:
    """«Назад» из меню покупки в главное меню."""
    await callback.answer()
    if callback.message is None:
        return
    user = callback.from_user
    text, keyboard = await build_menu_menu(user.id, user.first_name)
    await callback.message.edit_text(text, reply_markup=keyboard)


# ───────────── Выбор пакета и способа оплаты ─────────────

@router.callback_query(F.data.startswith("buy:pkg:"))
async def cb_buy_package(callback: CallbackQuery) -> None:
    """Нажали на пакет: показываем выбор способа оплаты (карта / звёзды)."""
    if callback.message is None or callback.data is None:
        await callback.answer()
        return

    package = _package_from_callback(callback.data, "buy:pkg:")
    if package is None:
        await callback.answer("Пакет недоступен, откройте /buy заново", show_alert=True)
        return

    await callback.answer()
    await callback.message.edit_text(
        METHOD_TEXT,
        reply_markup=payment_method_keyboard(package["credit_stars"], package["rub_to_pay"]),
    )


# ───────────── Выставление счетов ─────────────

@router.callback_query(F.data.startswith("buy:stars:"))
async def cb_pay_stars(callback: CallbackQuery) -> None:
    """Оплата звёздами: provider_token не нужен, валюта XTR."""
    if callback.message is None or callback.data is None:
        await callback.answer()
        return

    package = _package_from_callback(callback.data, "buy:stars:")
    if package is None:
        await callback.answer("Пакет недоступен, откройте /buy заново", show_alert=True)
        return

    await callback.answer()
    await callback.message.answer_invoice(
        title=package["label"],
        description=f"Пополнение баланса бота на {package['credit_stars']} токенов",
        payload=make_payload("stars", package["credit_stars"]),
        currency="XTR",
        prices=[LabeledPrice(label=package["label"], amount=package["stars_to_pay"])],
    )


@router.callback_query(F.data.startswith("buy:rub:"))
async def cb_pay_rub(callback: CallbackQuery) -> None:
    """Оплата картой через ЮKassa (встроенное окно оплаты Telegram)."""
    if callback.message is None or callback.data is None:
        await callback.answer()
        return

    # Токен ЮKassa не задан в .env, оплату картой выключаем
    if not config.YOOKASSA_PROVIDER_TOKEN:
        await callback.answer("Оплата картой временно недоступна", show_alert=True)
        return

    package = _package_from_callback(callback.data, "buy:rub:")
    if package is None:
        await callback.answer("Пакет недоступен, откройте /buy заново", show_alert=True)
        return

    rub = package["rub_to_pay"]
    item_name = _item_name(package)

    # Данные для чека по 54-ФЗ: что продали, за сколько, с каким НДС
    provider_data = {
        "receipt": {
            "items": [
                {
                    "description": item_name[:128],
                    "quantity": "1.00",
                    "amount": {"value": f"{rub:.2f}", "currency": "RUB"},
                    "vat_code": config.YOOKASSA_VAT_CODE,
                    "payment_mode": "full_payment",
                    "payment_subject": "service",
                }
            ]
        }
    }

    await callback.answer()
    await callback.message.answer_invoice(
        title=item_name[:32],  # лимит Telegram на заголовок: 32 символа
        description=f"{item_name} ({package['credit_stars']} токенов на баланс бота)",
        payload=make_payload("yk", package["credit_stars"]),
        provider_token=config.YOOKASSA_PROVIDER_TOKEN,
        currency="RUB",
        prices=[LabeledPrice(label=item_name[:32], amount=rub * 100)],  # в копейках!
        need_email=True,               # Telegram сам спросит email для чека
        send_email_to_provider=True,   # и передаст его в ЮKassa
        provider_data=json.dumps(provider_data, ensure_ascii=False),
    )


# ───────────── Проверка перед оплатой и после неё ─────────────

@router.pre_checkout_query()
async def handle_pre_checkout(query: PreCheckoutQuery) -> None:
    """Telegram спрашивает «всё ок?» перед списанием денег. Нужно ответить за ~10 секунд.
    Проверяем, что пакет существует, а валюта и сумма совпадают с ожидаемыми."""
    parsed = parse_payload(query.invoice_payload)
    package = PACKAGES_BY_CREDIT.get(parsed[1]) if parsed else None

    ok = False
    if package is not None:
        kind = parsed[0]
        if kind == "stars":
            ok = query.currency == "XTR" and query.total_amount == package["stars_to_pay"]
        elif kind == "yk":
            ok = query.currency == "RUB" and query.total_amount == package["rub_to_pay"] * 100

    if not ok:
        await query.answer(ok=False, error_message="Пакет недоступен, откройте /buy заново")
        return
    await query.answer(ok=True)


@router.message(F.successful_payment)
async def handle_successful_payment(message: Message) -> None:
    """Деньги уже списаны: начисляем токены и пишем пользователю."""
    if message.from_user is None or message.successful_payment is None:
        return

    payment = message.successful_payment
    parsed = parse_payload(payment.invoice_payload)

    # Деньги получены, но непонятно за что: пишем в лог для ручного разбора
    if parsed is None:
        logger.critical(
            "ОПЛАТА С НЕИЗВЕСТНЫМ PAYLOAD: user=%s, payload=%s, charge_id=%s",
            message.from_user.id, payment.invoice_payload, payment.telegram_payment_charge_id,
        )
        await message.answer("Оплата получена, но не удалось определить пакет. Мы разберёмся вручную.")
        return

    kind, credit_stars = parsed

    try:
        # Вернёт False, если этот charge_id уже начисляли (защита от дублей)
        credited = await credit_balance(
            telegram_id=message.from_user.id,
            username=message.from_user.username,
            stars_amount=credit_stars,
            charge_id=payment.telegram_payment_charge_id,
        )
    except Exception:
        # Сбой при начислении: по charge_id из лога найдёшь платёж и начислишь вручную
        logger.critical(
            "ОПЛАТА НЕ НАЧИСЛЕНА: user=%s, способ=%s, кредиты=%s, charge_id=%s, provider_charge_id=%s",
            message.from_user.id, kind, credit_stars,
            payment.telegram_payment_charge_id, payment.provider_payment_charge_id,
            exc_info=True,
        )
        await message.answer(
            "Оплата получена, но при начислении произошёл сбой. "
            "Мы разберёмся и начислим вручную."
        )
        return

    # Дубль: токены уже были начислены, второй раз не пишем пользователю
    if not credited:
        logger.warning(
            "Дубликат платежа проигнорирован: user=%s, charge_id=%s",
            message.from_user.id, payment.telegram_payment_charge_id,
        )
        return

    # provider_payment_charge_id пригодится для возвратов через ЮKassa
    logger.info(
        "Оплата: user=%s, способ=%s, кредиты=%s, charge_id=%s, provider_charge_id=%s",
        message.from_user.id, kind, credit_stars,
        payment.telegram_payment_charge_id, payment.provider_payment_charge_id,
    )

    balance = await get_balance(message.from_user.id)
    await message.answer(
        f"Оплата прошла успешно! Начислено {credit_stars} токенов.\n"
        f"Текущий баланс: {balance} токенов",
        reply_markup=payment_menu_keyboard(),
    )