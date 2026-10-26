import logging
from aiogram import Router
from aiogram.types import ErrorEvent
from aiogram import BaseMiddleware
from . import admin, generation, menu, payments, subscription
from .generation import provider

logger = logging.getLogger(__name__)

router = Router(name="root")


@router.error()
async def log_update_error(event: ErrorEvent) -> bool:
    logger.exception(
        "Необработанная ошибка при обработке Telegram update",
        exc_info=event.exception,
    )
    update = event.update
    if update.message is not None:
        await update.message.answer("Что-то пошло не так. Попробуйте ещё раз.")
    elif update.callback_query is not None:
        await update.callback_query.answer("Что-то пошло не так.", show_alert=True)
    return True


class ResetStateOnNavigation(BaseMiddleware):
    async def __call__(self, handler, event, data):
        text = getattr(event, "text", None)        # Message
        cb_data = getattr(event, "data", None)     # CallbackQuery

        is_command = bool(text) and text.startswith("/")
        is_nav = bool(cb_data) and cb_data.startswith(("menu:", "buy:"))

        if is_command or is_nav:
            state = data.get("state")
            if state is not None:
                await state.clear()
        return await handler(event, data)


router.message.outer_middleware(ResetStateOnNavigation())
router.callback_query.outer_middleware(ResetStateOnNavigation())

router.include_routers(
    admin.router,
    menu.router,
    payments.router,
    subscription.router,
    generation.router,
)

__all__ = ["router", "provider"]