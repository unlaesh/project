"""HTTP-приложение сервиса бота: приём вебхука Telegram и служебные адреса."""

from fastapi import FastAPI

from pary import __version__
from pary.api.routes import health


def create_app() -> FastAPI:
    """Создаёт приложение FastAPI.

    Фабрика вместо глобального объекта нужна, чтобы тесты получали
    чистый экземпляр приложения с нужными настройками.
    """
    app = FastAPI(title="ЮУрГУ.Пары", version=__version__)
    app.include_router(health.router)
    return app
