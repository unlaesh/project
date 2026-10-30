"""Настройки приложения.

Все значения читаются из переменных окружения (и файла .env при локальном запуске).
Секреты — токен бота, пароль базы данных — в коде и в репозитории не хранятся (NFR-13).
"""

from functools import lru_cache
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="PARY_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    env: Literal["dev", "test", "prod"] = "dev"
    log_level: str = "INFO"

    api_host: str = "127.0.0.1"
    api_port: int = 8000

    # Понадобится в задаче 3.2.1 (приём вебхука); пока необязателен,
    # чтобы /health и тесты запускались без секретов.
    bot_token: SecretStr | None = None


@lru_cache
def get_settings() -> Settings:
    """Возвращает настройки, прочитанные один раз за время жизни процесса."""
    return Settings()
