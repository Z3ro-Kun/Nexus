from fastapi.testclient import TestClient

from app import __version__
from app.persistence.database import Database


def test_app_starts_with_database_on_state(client: TestClient) -> None:
    assert isinstance(client.app.state.database, Database)  # type: ignore[attr-defined]


def test_health_returns_ok(client: TestClient) -> None:
    response = client.get("/api/v1/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "service": "NEXUS",
        "version": __version__,
        "environment": "test",
    }


def test_health_does_not_require_database(client: TestClient) -> None:
    # No PostgreSQL is running in the unit test environment; liveness must still pass.
    assert client.get("/api/v1/health").status_code == 200


def test_unknown_route_returns_404(client: TestClient) -> None:
    assert client.get("/api/v1/does-not-exist").status_code == 404
