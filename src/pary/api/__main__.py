"""Запуск HTTP-сервиса: python -m pary.api"""

import uvicorn

from pary.config import get_settings


def main() -> None:
    settings = get_settings()
    uvicorn.run(
        "pary.api:create_app",
        factory=True,
        host=settings.api_host,
        port=settings.api_port,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    main()
