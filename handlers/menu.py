import html
from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
import config
from user_service import get_balance, get_or_create_user
from .keyboards import main_menu_keyboard, start_menu_keyboard, balance_menu_keyboard

router = Router(name="menu")

async def build_menu_start():
    text = (
        "✨ Добро пожаловать в LinasGPT!\n\n"
        "Создавай нейрофото, пробуй трендовые образы и превращай свои фото в искусство 💗\n\n"
        "👇 Выбирай, с чего начнём!"
    )
    return text, start_menu_keyboard()

@router.message(CommandStart())
async def handle_start(message: Message) -> None:
    if message.from_user is not None:
        await get_or_create_user(message.from_user.id, message.from_user.username)
    text, keyboard = await build_menu_start()
    await message.answer(text, reply_markup=keyboard)

async def build_menu_menu(user_id: int, first_name: str) -> tuple[str, InlineKeyboardMarkup]:
    balance = await get_balance(user_id)
    text = (
        "🔐 <b>Мой профиль</b>\n"
        f"├ Имя: <b>{html.escape(first_name)}</b> (ID: <code>{user_id}</code>)\n"
        f"└ Токены: {balance}\n\n"
        "⚙️ <b>Настройки:</b>\n"
        "├ Модель фото: Nano Banana\n"
        f"└ Стоимость генерации: {config.PRICE_PER_GENERATION_STARS} токенов"
    )
    return text, main_menu_keyboard()


@router.message(Command("menu"))
async def handle_menu(message: Message) -> None:
    if message.from_user is None:
        return
    user = message.from_user
    await get_or_create_user(user.id, user.username)
    text, keyboard = await build_menu_menu(user.id, user.first_name)
    await message.answer(text, reply_markup=keyboard)


@router.message(Command("balance"))
async def handle_balance(message: Message) -> None:
    if message.from_user is None:
        return
    await get_or_create_user(message.from_user.id, message.from_user.username)
    balance = await get_balance(message.from_user.id)
    await message.answer(
        f"Баланс: {balance} токенов (≈ {balance // config.PRICE_PER_GENERATION_STARS} генераций)",
        reply_markup = balance_menu_keyboard(),
    )


@router.callback_query(F.data == "menu:balance")
async def cb_balance(callback: CallbackQuery) -> None:
    await callback.answer()
    if callback.message is None:
        return
    await get_or_create_user(callback.from_user.id, callback.from_user.username)
    balance = await get_balance(callback.from_user.id)
    await callback.message.answer(
        f"Баланс: {balance} токенов (≈ {balance // config.PRICE_PER_GENERATION_STARS} генераций)",
        reply_markup=balance_menu_keyboard(),
    )