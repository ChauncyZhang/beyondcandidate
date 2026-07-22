import json
import logging

from fastapi.testclient import TestClient

from server.app.core.logging import JsonFormatter, redact
from server.app.main import create_app


class HealthyProbe:
    async def check(self) -> None:
        return None


def test_trace_id_is_returned() -> None:
    response = TestClient(
        create_app(database_probe=HealthyProbe(), storage_probe=HealthyProbe())
    ).get("/health/live")

    assert len(response.headers["x-trace-id"]) == 32


def test_invalid_caller_trace_id_is_replaced() -> None:
    response = TestClient(
        create_app(database_probe=HealthyProbe(), storage_probe=HealthyProbe())
    ).get("/health/live", headers={"X-Trace-ID": "../../invalid trace"})

    assert response.headers["x-trace-id"] != "../../invalid trace"
    assert len(response.headers["x-trace-id"]) == 32


def test_redact_removes_sensitive_keys_recursively() -> None:
    value = {
        "password": "one",
        "nested": {"api_key": "two", "safe": "visible"},
        "items": [{"authorization": "three"}],
    }

    assert redact(value) == {
        "password": "[REDACTED]",
        "nested": {"api_key": "[REDACTED]", "safe": "visible"},
        "items": [{"authorization": "[REDACTED]"}],
    }


def test_redact_matches_sensitive_key_fragments() -> None:
    value = {
        "object_storage_secret_key": "one",
        "access_token": "two",
        "client_secret": "three",
        "database_password": "four",
        "safe": "visible",
    }

    assert redact(value) == {
        "object_storage_secret_key": "[REDACTED]",
        "access_token": "[REDACTED]",
        "client_secret": "[REDACTED]",
        "database_password": "[REDACTED]",
        "safe": "visible",
    }


def test_json_formatter_redacts_structured_context() -> None:
    record = logging.LogRecord("test", logging.INFO, "", 0, "request", (), None)
    record.context = {"cookie": "session", "path": "/health/live"}

    payload = json.loads(JsonFormatter().format(record))

    assert payload["context"]["cookie"] == "[REDACTED]"
    assert payload["context"]["path"] == "/health/live"


def test_request_log_uses_route_template_instead_of_raw_identifier(caplog) -> None:
    app = create_app(database_probe=HealthyProbe(), storage_probe=HealthyProbe())

    @app.get("/test/resumes/{resume_id}")
    async def resume(resume_id: str) -> dict[str, str]:
        return {"resume_id": resume_id}

    canary = "550e8400-e29b-41d4-a716-446655440000"
    with caplog.at_level(logging.INFO, logger="server.app.main"):
        TestClient(app).get(f"/test/resumes/{canary}")

    contexts = [getattr(record, "context", {}) for record in caplog.records]
    assert canary not in repr(contexts)
    assert any(context.get("route") == "/test/resumes/{resume_id}" for context in contexts)
