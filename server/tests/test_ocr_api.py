from fastapi.testclient import TestClient
from sqlalchemy import select

from server.app.identity.models import AuditLog
from server.app.ocr.api import router as ocr_router
from server.app.ocr.models import OcrProviderConfig
from server.tests.test_screening_api import app_and_seed, login


class Gateway:
    def __init__(self):
        self.calls = []

    def validate_provider(self, provider_id, base_url, model):
        self.calls.append(("validate", provider_id, base_url, model))

    async def test_connection(self, provider_id, base_url, model, api_key):
        self.calls.append(("test", provider_id, base_url, model, api_key))
        return 17


def ocr_app(tmp_path):
    app, _, _ = app_and_seed(tmp_path)
    app.include_router(ocr_router)
    app.state.ocr_gateway = Gateway()
    return app


def test_ocr_config_authz_key_redaction_locking_and_idempotency(tmp_path):
    app = ocr_app(tmp_path)
    payload = {
        "provider_id": "vision",
        "base_url": "https://vision.example/v1",
        "model": "vision-1",
        "enabled": True,
        "api_key": "sk-private",
    }
    with TestClient(app) as client:
        system = login(client, "system@example.test")
        missing_match = client.put(
            "/api/v1/settings/ocr", json=payload, headers={**system, "Idempotency-Key": "missing"}
        )
        assert missing_match.status_code == 428
        saved = client.put(
            "/api/v1/settings/ocr",
            json=payload,
            headers={**system, "If-Match": '"0"', "Idempotency-Key": "save"},
        )
        assert saved.status_code == 200, saved.text
        assert saved.json()["data"]["key_configured"] is True
        assert saved.json()["data"]["version"] == 1
        assert "api_key" not in saved.text and "sk-private" not in saved.text

        replay = client.put(
            "/api/v1/settings/ocr",
            json=payload,
            headers={**system, "If-Match": '"0"', "Idempotency-Key": "save"},
        )
        assert replay.status_code == 200 and replay.json() == saved.json()
        conflict = client.put(
            "/api/v1/settings/ocr",
            json={**payload, "enabled": False},
            headers={**system, "If-Match": '"0"', "Idempotency-Key": "save"},
        )
        assert conflict.status_code == 409 and conflict.json()["code"] == "idempotency_conflict"
        stale = client.put(
            "/api/v1/settings/ocr",
            json={**payload, "api_key": None},
            headers={**system, "If-Match": '"0"', "Idempotency-Key": "stale"},
        )
        assert stale.status_code == 409 and stale.json()["code"] == "resource_version_conflict"
        preserved = client.put(
            "/api/v1/settings/ocr",
            json={key: value for key, value in payload.items() if key != "api_key"},
            headers={**system, "If-Match": '"1"', "Idempotency-Key": "preserve"},
        )
        assert preserved.status_code == 200 and preserved.json()["data"]["version"] == 2

        tested = client.post(
            "/api/v1/settings/ocr/test", headers={**system, "Idempotency-Key": "test"}
        )
        assert tested.status_code == 200 and tested.json()["data"] == {
            "status": "succeeded",
            "safe_error_code": None,
            "latency_ms": 17,
        }
        test_replay = client.post(
            "/api/v1/settings/ocr/test", headers={**system, "Idempotency-Key": "test"}
        )
        assert test_replay.json() == tested.json()
        assert [call[0] for call in app.state.ocr_gateway.calls].count("test") == 1

        changed = client.put(
            "/api/v1/settings/ocr",
            json={**payload, "model": "vision-2", "api_key": None},
            headers={**system, "If-Match": '"2"', "Idempotency-Key": "change-model"},
        )
        assert changed.status_code == 200, changed.text
        assert changed.json()["data"]["last_test_status"] is None
        assert changed.json()["data"]["last_tested_at"] is None

        client.post("/api/v1/auth/logout", headers=system)
        admin = login(client, "admin@example.test")
        visible = client.get("/api/v1/settings/ocr", headers=admin)
        assert visible.status_code == 200
        assert visible.json()["data"]["last_test_status"] is None
        assert "key_configured" not in visible.text
        denied_write = client.put(
            "/api/v1/settings/ocr",
            json=payload,
            headers={**admin, "If-Match": '"3"', "Idempotency-Key": "denied"},
        )
        assert denied_write.status_code == 404
        denied_test = client.post(
            "/api/v1/settings/ocr/test", headers={**admin, "Idempotency-Key": "denied-test"}
        )
        assert denied_test.status_code == 404
        client.post("/api/v1/auth/logout", headers=admin)
        manager = login(client, "manager@example.test")
        assert client.get("/api/v1/settings/ocr", headers=manager).status_code == 404

    with app.state.identity_store.sync_session() as db:
        config = db.scalar(select(OcrProviderConfig))
        assert config.encrypted_api_key and b"sk-private" not in config.encrypted_api_key
        rendered = str([audit.metadata_json for audit in db.scalars(select(AuditLog))])
        assert "sk-private" not in rendered


def test_ocr_config_requires_key_when_enabled_and_rejects_bad_url(tmp_path):
    app = ocr_app(tmp_path)
    from server.app.ocr.gateway import OcrGateway

    app.state.ocr_gateway = OcrGateway(resolver=lambda *args, **kwargs: [(2, 1, 6, "", ("8.8.8.8", 443))])
    with TestClient(app) as client:
        system = login(client, "system@example.test")
        missing = client.put(
            "/api/v1/settings/ocr",
            json={
                "provider_id": "vision",
                "base_url": "https://vision.example/v1",
                "model": "model",
                "enabled": True,
            },
            headers={**system, "If-Match": '"0"', "Idempotency-Key": "missing-key"},
        )
        assert missing.status_code == 422 and missing.json()["code"] == "api_key_required"
        unsafe = client.put(
            "/api/v1/settings/ocr",
            json={
                "provider_id": "vision",
                "base_url": "https://127.0.0.1/v1",
                "model": "model",
                "enabled": False,
            },
            headers={**system, "If-Match": '"0"', "Idempotency-Key": "unsafe"},
        )
        assert unsafe.status_code == 422 and unsafe.json()["code"] == "provider_address_forbidden"


def test_ocr_config_normalizes_mixed_case_provider_id(tmp_path):
    app = ocr_app(tmp_path)
    with TestClient(app) as client:
        system = login(client, "system@example.test")
        saved = client.put(
            "/api/v1/settings/ocr",
            json={
                "provider_id": " Ali ",
                "base_url": "https://vision.example/v1",
                "model": "qwen3.5-ocr",
                "enabled": True,
                "api_key": "sk-private",
            },
            headers={**system, "If-Match": '"0"', "Idempotency-Key": "normalize-provider"},
        )

        assert saved.status_code == 200, saved.text
        assert saved.json()["data"]["provider_id"] == "ali"
