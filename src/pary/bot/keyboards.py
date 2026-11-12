"""Стандартные клавиатуры Telegram для общей навигации."""

from aiogram.types import KeyboardButton, ReplyKeyboardMarkup

from pary.bot import texts


def main_menu_keyboard(*, include_notes: bool = False) -> ReplyKeyboardMarkup:
    """Возвращает новую клавиатуру; заметки доступны только при готовом обработчике."""
    rows = [
        [KeyboardButton(text=texts.TODAY), KeyboardButton(text=texts.TOMORROW)],
        [KeyboardButton(text=texts.WEEK), KeyboardButton(text=texts.SETTINGS)],
    ]
    if include_notes:
        rows.append([KeyboardButton(text=texts.NOTES)])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True, is_persistent=True)


def back_to_menu_keyboard() -> ReplyKeyboardMarkup:
    """Клавиатура вложенного сценария с явным возвратом без знания команд."""
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=texts.MAIN_MENU)]],
        resize_keyboard=True,
        is_persistent=True,
    )
