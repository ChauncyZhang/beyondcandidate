from fastapi.testclient import TestClient

from server.app.main import create_app


class HealthyProbe:
    async def check(self) -> None:
        return None


def test_http_metrics_use_template_routes_and_fixed_status_classes() -> None:
    app = create_app(database_probe=HealthyProbe(), storage_probe=HealthyProbe())

    @app.get("/test/candidates/{candidate_id}")
    async def candidate(candidate_id: str) -> dict[str, str]:
        return {"candidate_id": candidate_id}

    canary = "candidate-550e8400-e29b-41d4-a716-446655440000"
    client = TestClient(app)
    assert client.get(f"/test/candidates/{canary}?email=alice@example.test").status_code == 200

    response = client.get("/metrics")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert 'route="/test/candidates/{candidate_id}"' in response.text
    assert 'method="GET"' in response.text
    assert 'status_class="2xx"' in response.text
    assert canary not in response.text
    assert "alice@example.test" not in response.text


def test_readiness_metrics_are_bounded_by_dependency_and_result() -> None:
    client = TestClient(
        create_app(database_probe=HealthyProbe(), storage_probe=HealthyProbe())
    )

    assert client.get("/health/ready").status_code == 200
    metrics = client.get("/metrics").text

    assert 'dependency="database",result="ready"' in metrics
    assert 'dependency="storage",result="ready"' in metrics
    assert "ux09_readiness_duration_seconds" in metrics


def test_unknown_http_methods_collapse_to_other() -> None:
    client = TestClient(
        create_app(database_probe=HealthyProbe(), storage_probe=HealthyProbe())
    )

    assert client.request("CANARYMETHOD", "/health/live").status_code == 405
    metrics = client.get("/metrics").text

    assert 'method="OTHER"' in metrics
    assert "CANARYMETHOD" not in metrics


def test_login_failures_use_a_fixed_safe_reason() -> None:
    client = TestClient(
        create_app(database_probe=HealthyProbe(), storage_probe=HealthyProbe())
    )

    response = client.post(
        "/api/v1/auth/login",
        json={"organization_slug": "missing", "email": "alice@example.test", "password": "canary"},
    )
    assert response.status_code == 403

    metrics = client.get("/metrics").text
    assert 'ux09_login_failures_total{reason="request_rejected"} 1.0' in metrics
    assert "alice@example.test" not in metrics
    assert "canary" not in metrics
