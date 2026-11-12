from fastapi.testclient import TestClient

from pary import __version__
from pary.api import create_app


def test_health_returns_ok() -> None:
    client = TestClient(create_app())

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": __version__}


def test_health_rejects_post() -> None:
    client = TestClient(create_app())

    response = client.post("/health")

    assert response.status_code == 405
