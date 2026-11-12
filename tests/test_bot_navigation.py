"""Навигация через настоящий Dispatcher; Telegram API заменён локальной сессией."""

import asyncio
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from typing import Any

import pytest
from aiogram import Bot, Dispatcher, Router
from aiogram.client.session.base import BaseSession
from aiogram.enums import ChatType
from aiogram.methods import SendMessage
from aiogram.methods.base import TelegramMethod
from aiogram.types import Chat, Message, ReplyKeyboardMarkup, Update, User

from pary.bot import (
    MenuHandlers,
    back_to_menu_keyboard,
    create_menu_router,
    show_main_menu,
)

NOW = datetime(2026, 11, 12, tzinfo=UTC)


class RecordingSession(BaseSession):
    """Запрещает любые сетевые вызовы и записывает исходящие сообщения."""

    def __init__(self) -> None:
        super().__init__()
        self.sent: list[SendMessage] = []

    async def close(self) -> None:
        pass

    async def make_request(
        self, bot: Bot, method: TelegramMethod[Any], timeout: int | None = None
    ) -> Message:
        assert isinstance(method, SendMessage), f"Unexpected API method: {type(method)}"
        self.sent.append(method)
        return Message(
            message_id=len(self.sent),
            date=NOW,
            chat=Chat(id=int(method.chat_id), type=ChatType.PRIVATE),
            text=method.text,
        )

    async def stream_content(
        self,
        url: str,
        headers: dict[str, Any] | None = None,
        timeout: int = 30,
        chunk_size: int = 65536,
        raise_for_status: bool = True,
    ) -> AsyncGenerator[bytes, None]:
        raise AssertionError("Network access is forbidden in navigation tests")
        yield b""  # pragma: no cover — establishes an async-generator method.


def incoming(text: str | None, *, chat_type: ChatType = ChatType.PRIVATE) -> Update:
    return Update(
        update_id=1,
        message=Message(
            message_id=1,
            date=NOW,
            chat=Chat(id=42, type=chat_type),
            from_user=User(id=42, is_bot=False, first_name="Test"),
            text=text,
        ),
    )


def callbacks(calls: list[tuple[str, str | None]]) -> dict[str, Any]:
    def section(name: str):
        async def handle(message: Message) -> str:
            calls.append((name, message.text))
            return name

        return handle

    return {name: section(name) for name in ("today", "tomorrow", "week", "settings")}


def application(handlers: MenuHandlers) -> tuple[Dispatcher, Bot, RecordingSession]:
    session = RecordingSession()
    bot = Bot(token="123456:offline-only-not-a-real-token", session=session)
    dispatcher = Dispatcher()
    dispatcher.include_router(create_menu_router(handlers))
    return dispatcher, bot, session


def button_labels(markup: ReplyKeyboardMarkup) -> list[str]:
    return [button.text for row in markup.keyboard for button in row]


@pytest.mark.parametrize(
    ("label", "section"),
    [
        ("Сегодня", "today"),
        ("Завтра", "tomorrow"),
        ("Неделя", "week"),
        ("Настройки", "settings"),
    ],
)
def test_button_dispatches_to_its_section_without_a_command(label: str, section: str) -> None:
    calls: list[tuple[str, str | None]] = []
    dispatcher, bot, session = application(MenuHandlers(**callbacks(calls)))

    result = asyncio.run(dispatcher.feed_update(bot, incoming(label)))

    assert result == section
    assert calls == [(section, label)]
    assert session.sent == []  # The navigation layer does not invent a section's response.


def test_nested_section_can_return_to_main_menu_and_leave_its_dialog_state() -> None:
    calls: list[tuple[str, str | None]] = []
    sections = callbacks(calls)

    async def nested_settings(message: Message) -> None:
        await message.answer("Выберите действие", reply_markup=back_to_menu_keyboard())

    sections["settings"] = nested_settings
    dispatcher, bot, session = application(MenuHandlers(**sections))

    async def visit_and_return() -> None:
        await dispatcher.feed_update(bot, incoming("Настройки"))
        state = dispatcher.fsm.get_context(bot=bot, chat_id=42, user_id=42)
        await state.set_state("settings:input")
        await state.set_data({"draft": "unfinished input"})

        await dispatcher.feed_update(bot, incoming("Главное меню"))

        assert await state.get_state() is None
        assert await state.get_data() == {}

    asyncio.run(visit_and_return())

    assert len(session.sent) == 2
    assert button_labels(session.sent[0].reply_markup) == ["Главное меню"]
    assert session.sent[1].text == "Выберите раздел:"
    menu = session.sent[1].reply_markup
    assert isinstance(menu, ReplyKeyboardMarkup)
    assert button_labels(menu) == ["Сегодня", "Завтра", "Неделя", "Настройки"]
    assert menu.resize_keyboard is True
    assert menu.is_persistent is True


@pytest.mark.parametrize("with_notes", [False, True])
def test_notes_visibility_and_dispatch_use_the_same_ready_handler(with_notes: bool) -> None:
    calls: list[tuple[str, str | None]] = []

    async def notes(message: Message) -> str:
        calls.append(("notes", message.text))
        return "notes"

    handlers = MenuHandlers(**callbacks(calls), notes=notes if with_notes else None)
    dispatcher, bot, session = application(handlers)
    fallback = Router()

    async def unhandled(message: Message) -> str:
        return "unhandled"

    fallback.message.register(unhandled)
    dispatcher.include_router(fallback)

    async def show_and_click() -> str:
        # A registration handler can show the same menu after checking the user.
        message = incoming("registered").message.as_(bot)
        await show_main_menu(message, handlers=handlers)
        return await dispatcher.feed_update(bot, incoming("Заметки"))

    result = asyncio.run(show_and_click())

    menu = session.sent[0].reply_markup
    assert isinstance(menu, ReplyKeyboardMarkup)
    assert ("Заметки" in button_labels(menu)) is with_notes
    assert result == ("notes" if with_notes else "unhandled")
    assert calls == ([("notes", "Заметки")] if with_notes else [])


@pytest.mark.parametrize(
    "text", ["/start", "/start@pary_bot", "/help", "ЕТ-411", "12.11", "Сегодня вечером", None]
)
def test_registration_commands_and_free_input_reach_the_following_router(text: str | None) -> None:
    calls: list[tuple[str, str | None]] = []
    dispatcher, bot, session = application(MenuHandlers(**callbacks(calls)))
    fallback = Router()

    async def other_scenario(message: Message) -> str:
        calls.append(("other", message.text))
        return "other"

    fallback.message.register(other_scenario)
    dispatcher.include_router(fallback)

    result = asyncio.run(dispatcher.feed_update(bot, incoming(text)))

    assert result == "other"
    assert calls == [("other", text)]
    assert session.sent == []


def test_menu_is_not_exposed_in_a_group_chat() -> None:
    calls: list[tuple[str, str | None]] = []
    dispatcher, bot, session = application(MenuHandlers(**callbacks(calls)))

    asyncio.run(dispatcher.feed_update(bot, incoming("Сегодня", chat_type=ChatType.GROUP)))

    assert calls == []
    assert session.sent == []


@pytest.mark.parametrize("name", ["today", "tomorrow", "week", "settings"])
def test_navigation_rejects_an_unimplemented_required_section(name: str) -> None:
    sections = callbacks([])
    sections[name] = None

    with pytest.raises(TypeError, match=f"{name} must be an async handler"):
        MenuHandlers(**sections)


def test_navigation_rejects_a_synchronous_section_handler() -> None:
    sections = callbacks([])
    sections["today"] = lambda message: "not asynchronous"

    with pytest.raises(TypeError, match="today must be an async handler"):
        MenuHandlers(**sections)


@pytest.mark.parametrize(("section", "label"), [("today", "Сегодня"), ("notes", "Заметки")])
def test_dispatcher_awaits_async_callable_instances(section: str, label: str) -> None:
    calls: list[tuple[str, str | None]] = []

    class AsyncSection:
        async def __call__(self, message: Message) -> str:
            calls.append((section, message.text))
            return "async instance was awaited"

    sections = callbacks(calls)
    sections[section] = AsyncSection()
    handlers = MenuHandlers(**sections)
    dispatcher, bot, session = application(handlers)

    async def show_and_dispatch() -> str:
        await show_main_menu(incoming("registered").message.as_(bot), handlers=handlers)
        return await dispatcher.feed_update(bot, incoming(label))

    result = asyncio.run(show_and_dispatch())

    assert result == "async instance was awaited"
    assert calls == [(section, label)]
    assert label in button_labels(session.sent[0].reply_markup)
