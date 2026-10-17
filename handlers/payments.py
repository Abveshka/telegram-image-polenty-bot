import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, LabeledPrice, Message, PreCheckoutQuery

import config
from user_service import credit_balance, get_balance
from .keyboards import buy_menu_keyboard, packages_keyboard, payment_menu_keyboard
from .menu import build_menu_menu

logger = logging.getLogger(__name__)
router = Router(name="payments")

PACKAGES_BY_PAYLOAD = {f"stars_{p['credit_stars']}": p for p in config.STAR_PACKAGES}

BUY_TEXT = (
    "❤️ Пополните баланс, чтобы пользоваться ботом без ограничений!\n\n"
    "Что я умею?\n"
    "🔹 <b>Генерирую фото</b> - пришли описание\n"
    "🔹 <b>Редактирую фото</b> - пришли картинку с подписью\n\n"
    "<i>Оплатить токены можно с помощью Telegram Stars</i>"
)

PACKAGES_TEXT = "📦 Выберите комплект токенов"


# /buy командой: новое сообщение с меню
@router.message(Command("buy"))
async def handle_buy(message: Message) -> None:
    await message.answer(BUY_TEXT, reply_markup=buy_menu_keyboard())


# Кнопка «Пополнить» в главном меню
@router.callback_query(F.data == "menu:buy")
async def cb_buy(callback: CallbackQuery) -> None:
    await callback.answer()
    if callback.message is None:
        return
    await callback.message.edit_text(BUY_TEXT, reply_markup=buy_menu_keyboard())


# «Купить токены»: список пакетов
@router.callback_query(F.data == "buy:tokens")
async def cb_buy_tokens(callback: CallbackQuery) -> None:
    await callback.answer()
    if callback.message is None:
        return
    await callback.message.edit_text(PACKAGES_TEXT, reply_markup=packages_keyboard())


# «Назад» из списка пакетов -> меню покупки
@router.callback_query(F.data == "buy:menu")
async def cb_buy_back_to_menu(callback: CallbackQuery) -> None:
    await callback.answer()
    if callback.message is None:
        return
    await callback.message.edit_text(BUY_TEXT, reply_markup=buy_menu_keyboard())


# «Назад» из меню покупки -> главное меню
@router.callback_query(F.data == "buy:main")
async def cb_buy_back_to_main(callback: CallbackQuery) -> None:
    await callback.answer()
    if callback.message is None:
        return
    user = callback.from_user
    text, keyboard = await build_menu_menu(user.id, user.first_name)
    await callback.message.edit_text(text, reply_markup=keyboard)


# Нажатие на пакет: отправляем счёт
@router.callback_query(F.data.startswith("buy:pkg:"))
async def cb_buy_package(callback: CallbackQuery) -> None:
    if callback.message is None or callback.data is None:
        await callback.answer()
        return

    credit_stars = callback.data.removeprefix("buy:pkg:")
    package = PACKAGES_BY_PAYLOAD.get(f"stars_{credit_stars}")
    if package is None:
        await callback.answer("Пакет недоступен, откройте /buy заново", show_alert=True)
        return

    await callback.answer()
    await callback.message.answer_invoice(
        title=package["label"],
        description=f"Пополнение баланса бота на {package['credit_stars']} токенов",
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

    payment = message.successful_payment
    credit_stars = int(payment.invoice_payload.replace("stars_", ""))

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
        f"Оплата прошла успешно! Начислено {credit_stars} токенов.\n"
        f"Текущий баланс: {balance} токенов",
        reply_markup=payment_menu_keyboard(),
    )