import asyncio

from fastapi.testclient import TestClient

from server.app.main import create_app


class Probe:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error

    async def check(self) -> None:
        if self.error:
            raise self.error


class HangingProbe:
    async def check(self) -> None:
        await asyncio.Event().wait()


def test_live_health_ignores_failed_dependencies() -> None:
    app = create_app(
        database_probe=Probe(RuntimeError("database password leaked")),
        storage_probe=Probe(RuntimeError("storage secret leaked")),
    )

    response = TestClient(app).get("/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "live"}


def test_ready_health_succeeds_when_dependencies_are_ready() -> None:
    app = create_app(database_probe=Probe(), storage_probe=Probe())

    response = TestClient(app).get("/health/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ready"}


def test_ready_health_returns_safe_problem_when_dependency_fails() -> None:
    app = create_app(
        database_probe=Probe(RuntimeError("postgresql://user:secret@db/database")),
        storage_probe=Probe(),
    )

    response = TestClient(app).get("/health/ready")

    assert response.status_code == 503
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["code"] == "dependencies_unavailable"
    assert "secret" not in response.text


def test_ready_health_times_out_hanging_probes() -> None:
    from server.app.core.settings import Settings

    app = create_app(
        settings=Settings(readiness_timeout_seconds=0.01),
        database_probe=HangingProbe(),
        storage_probe=Probe(),
    )

    response = TestClient(app).get("/health/ready")

    assert response.status_code == 503
    assert response.json()["code"] == "dependencies_unavailable"
