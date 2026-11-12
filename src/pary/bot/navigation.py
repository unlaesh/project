"""Навигация #25: передача кнопок подключённым обработчикам разделов.

Модуль не реализует регистрацию, расписание, настройки или транспорт Telegram.
Приложение подключает router вместе с готовыми обработчиками этих сценариев.
Регистрация вызывает show_main_menu после выбора группы либо при повторном /start.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from inspect import iscoroutinefunction

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from pary.bot import texts
from pary.bot.keyboards import main_menu_keyboard

SectionHandler = Callable[[Message], Awaitable[object]]


@dataclass(frozen=True)
class MenuHandlers:
    """Готовые async-обработчики; отсутствие notes одновременно скрывает его кнопку."""

    today: SectionHandler
    tomorrow: SectionHandler
    week: SectionHandler
    settings: SectionHandler
    notes: SectionHandler | None = None

    def __post_init__(self) -> None:
        for name in ("today", "tomorrow", "week", "settings", "notes"):
            handler = getattr(self, name)
            if name == "notes" and handler is None:
                continue
            # aiogram detects async functions, but not instances with async __call__.
            # Store their bound method so the dispatcher actually awaits the result.
            if callable(handler) and not iscoroutinefunction(handler):
                handler = handler.__call__
            if not iscoroutinefunction(handler):
                raise TypeError(f"{name} must be an async handler")
            object.__setattr__(self, name, handler)


async def show_main_menu(message: Message, *, handlers: MenuHandlers) -> Message:
    """Показывает доступные разделы после проверки регистрации вызывающим кодом."""
    return await message.answer(
        texts.MAIN_MENU_PROMPT,
        reply_markup=main_menu_keyboard(include_notes=handlers.notes is not None),
    )


def create_menu_router(handlers: MenuHandlers) -> Router:
    """Маршрутизирует точные подписи кнопок, оставляя остальной ввод другим router.

    Подключать перед обработчиками свободного ввода во вложенных сценариях,
    чтобы «Главное меню» оставалось доступным в любом состоянии диалога.
    Проверка пользователя и получение данных выполняются обработчиками разделов.
    """
    router = Router(name="main_menu")
    router.message.filter(F.chat.type == ChatType.PRIVATE)
    for label, handler in (
        (texts.TODAY, handlers.today),
        (texts.TOMORROW, handlers.tomorrow),
        (texts.WEEK, handlers.week),
        (texts.SETTINGS, handlers.settings),
    ):
        router.message.register(handler, F.text == label)
    if handlers.notes is not None:
        router.message.register(handlers.notes, F.text == texts.NOTES)

    async def return_to_menu(message: Message, state: FSMContext) -> None:
        await state.clear()
        await show_main_menu(message, handlers=handlers)

    router.message.register(return_to_menu, F.text == texts.MAIN_MENU)
    return router
