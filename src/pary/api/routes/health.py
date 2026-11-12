"""Служебный адрес /health для внешнего мониторинга (NFR-10, WBS 3.2.3)."""

from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel

from pary import __version__

router = APIRouter(tags=["service"])


class HealthResponse(BaseModel):
    status: Literal["ok"]
    version: str


@router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    """Проверка живости процесса: отвечает 200, если сервис запущен.

    Проверка подключения к PostgreSQL будет добавлена вместе с базой данных (2.2.3).
    """
    return HealthResponse(status="ok", version=__version__)
