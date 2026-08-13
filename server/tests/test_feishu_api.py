import base64
import hashlib
import json
from contextlib import contextmanager
from datetime import datetime, timezone

import pytest
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from fastapi.testclient import TestClient
from sqlalchemy import select

from server.app.core.settings import Settings
from server.app.identity.models import AuditLog, Organization, User, UserRole, UserStatus
from server.app.identity.security import PasswordService
from server.app.identity.service import Clock, TokenSource
from server.app.integrations.feishu.models import FeishuIdentityBinding, FeishuOrganizationConfig
from server.app.integrations.feishu.provider import ConnectionResult, FakeFeishuProvider, OAuthIdentity
from server.app.main import create_app


class Probe:
    async def check(self) -> None:
        pass


class FixedClock(Clock):
    def current_time(self) -> datetime:
        return datetime(2026, 7, 16, 8, tzinfo=timezone.utc)


class Tokens(TokenSource):
    def __init__(self) -> None:
        self.index = 0

    def new_token(self) -> str:
        self.index += 1
        return f"token-{self.index:064d}"


@pytest.fixture
def feishu_app(tmp_path):
    settings = Settings(
        environment="test",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'feishu.db'}",
        cors_origins=["https://hr.example.test"],
    )
    app = create_app(
        settings=settings,
        database_probe=Probe(),
        storage_probe=Probe(),
        clock=FixedClock(),
        token_source=Tokens(),
        initialize_identity_schema=True,
    )
    app.state.feishu_provider = FakeFeishuProvider()
    with TestClient(app) as client:
        yield app, client


def seed_user(app, *, email="admin@example.test", status=UserStatus.ACTIVE):
    with app.state.identity_store.sync_session() as db:
        organization = db.scalar(select(Organization).where(Organization.slug == "acme"))
        if organization is None:
            organization = Organization(slug="acme", name="Acme", status="active")
            db.add(organization)
            db.flush()
        user = User(
            organization_id=organization.id,
            email=email,
            normalized_email=email.casefold(),
            display_name=email.split("@", 1)[0],
            password_hash=PasswordService().hash("correct horse"),
            status=status,
        )
        user.roles.append(UserRole(role="recruiting_admin"))
        db.add(user)
        db.commit()
        return user.id


def login(client, email="admin@example.test") -> str:
    response = client.post(
        "/api/v1/auth/login",
        json={"organization_slug": "acme", "email": email, "password": "correct horse"},
        headers={"Origin": "https://hr.example.test"},
    )
    assert response.status_code == 200
    return response.headers["X-CSRF-Token"]


def write_headers(csrf: str) -> dict[str, str]:
    return {"Origin": "https://hr.example.test", "X-CSRF-Token": csrf}


def config_payload(**overrides):
    payload = {
        "app_id": "cli_test",
        "app_secret": "app-secret-value",
        "redirect_uri": "https://hr.example.test/api/v1/auth/feishu/callback",
        "calendar_id": "primary",
        "verification_token": "verification-value",
        "encrypt_key": "encrypt-key-value",
        "enabled": True,
    }
    payload.update(overrides)
    return payload


def configure_feishu(feishu_app, *, encrypt_key=None):
    app, client = feishu_app
    admin_id = seed_user(app)
    csrf = login(client)
    response = client.put(
        "/api/v1/settings/integrations/feishu",
        json=config_payload(encrypt_key=encrypt_key),
        headers=write_headers(csrf),
    )
    assert response.status_code == 200
    with app.state.identity_store.sync_session() as db:
        admin = db.get(User, admin_id)
        db.add(
            FeishuIdentityBinding(
                organization_id=admin.organization_id,
                user_id=admin.id,
                union_id="on_admin",
                open_id="ou_admin",
                tenant_key="tenant-key",
            )
        )
        db.commit()
        return admin.organization_id, csrf


def encrypted_webhook(payload: dict, encrypt_key: str, *, signature_override: str | None = None):
    plaintext = json.dumps(payload, separators=(",", ":")).encode()
    padder = padding.PKCS7(128).padder()
    padded = padder.update(plaintext) + padder.finalize()
    iv = b"0123456789abcdef"
    key = hashlib.sha256(encrypt_key.encode()).digest()
    encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    encrypted = iv + encryptor.update(padded) + encryptor.finalize()
    body = json.dumps(
        {"encrypt": base64.b64encode(encrypted).decode()}, separators=(",", ":")
    ).encode()
    timestamp = "1720000000"
    nonce = "nonce-value"
    signature = hashlib.sha256(
        timestamp.encode() + nonce.encode() + encrypt_key.encode() + body
    ).hexdigest()
    return body, {
        "Content-Type": "application/json",
        "X-Lark-Request-Timestamp": timestamp,
        "X-Lark-Request-Nonce": nonce,
        "X-Lark-Signature": signature_override or signature,
    }


def test_config_is_disabled_by_default_and_never_returns_plaintext(feishu_app) -> None:
    app, client = feishu_app
    admin_id = seed_user(app)
    csrf = login(client)

    missing = client.get("/api/v1/settings/integrations/feishu")
    assert missing.status_code == 200
    assert missing.json()["data"] == {"configured": False, "enabled": False}

    saved = client.put(
        "/api/v1/settings/integrations/feishu",
        json=config_payload(),
        headers=write_headers(csrf),
    )
    assert saved.status_code == 200
    rendered = saved.text
    for secret in ("app-secret-value", "verification-value", "encrypt-key-value"):
        assert secret not in rendered
    assert saved.json()["data"]["app_secret_configured"] is True
    assert saved.headers["Cache-Control"] == "no-store"

    with app.state.identity_store.sync_session() as db:
        admin = db.get(User, admin_id)
        db.add(
            FeishuIdentityBinding(
                organization_id=admin.organization_id,
                user_id=admin.id,
                union_id="on_admin",
                open_id="ou_admin",
                tenant_key="tenant",
            )
        )
        db.commit()

    tested = client.post(
        "/api/v1/settings/integrations/feishu/test",
        headers=write_headers(csrf),
    )
    assert tested.status_code == 200
    assert tested.json()["data"]["last_test_status"] == "succeeded"
    assert app.state.feishu_provider.messages[-1][0] == "ou_admin"
    assert "招聘提醒测试成功" in app.state.feishu_provider.messages[-1][1]


def test_connection_test_requires_the_current_admin_to_bind_feishu(feishu_app) -> None:
    app, client = feishu_app
    seed_user(app)
    csrf = login(client)
    assert client.put(
        "/api/v1/settings/integrations/feishu",
        json=config_payload(),
        headers=write_headers(csrf),
    ).status_code == 200

    tested = client.post(
        "/api/v1/settings/integrations/feishu/test",
        headers=write_headers(csrf),
    )

    assert tested.status_code == 409
    assert tested.json()["code"] == "feishu_test_user_unbound"
    assert app.state.feishu_provider.messages == []


def test_login_authorization_is_disabled_safely_until_configured(feishu_app) -> None:
    app, client = feishu_app
    seed_user(app)

    response = client.post(
        "/api/v1/auth/feishu/authorize",
        json={"organization_slug": "acme"},
        headers={"Origin": "https://hr.example.test"},
    )
    assert response.status_code == 409
    assert response.json()["code"] == "feishu_disabled"
    assert app.state.feishu_provider.exchanged_codes == []


def test_oauth_login_activates_only_a_preinvited_user_and_consumes_state_once(feishu_app) -> None:
    app, client = feishu_app
    admin_id = seed_user(app)
    invited_id = seed_user(app, email="invited@example.test", status=UserStatus.INVITED)
    csrf = login(client)
    assert client.put(
        "/api/v1/settings/integrations/feishu",
        json=config_payload(),
        headers=write_headers(csrf),
    ).status_code == 200
    client.post("/api/v1/auth/logout", headers=write_headers(csrf))
    app.state.feishu_provider.identity = OAuthIdentity(
        "on_invited", "ou_invited", "invited@example.test", "tenant"
    )

    authorized = client.post(
        "/api/v1/auth/feishu/authorize",
        json={"organization_slug": "acme"},
        headers={"Origin": "https://hr.example.test"},
    )
    assert authorized.status_code == 200
    state = authorized.json()["data"]["state"]
    assert state in authorized.json()["data"]["authorization_url"]

    callback = client.get(
        "/api/v1/auth/feishu/callback",
        params={"code": "oauth-code", "state": state},
        follow_redirects=False,
    )
    assert callback.status_code == 303
    assert callback.headers["location"] == "/?feishu_status=connected"
    assert "hr_session=" in callback.headers["set-cookie"]
    me = client.get("/api/v1/me", headers={"Sec-Fetch-Site": "same-origin"})
    assert me.status_code == 200
    assert me.json()["data"]["id"] == str(invited_id)
    assert me.headers["x-csrf-token"]
    replay = client.get(
        "/api/v1/auth/feishu/callback", params={"code": "oauth-code", "state": state}
    )
    assert replay.status_code == 422
    assert replay.json()["code"] == "oauth_state_invalid"

    with app.state.identity_store.sync_session() as db:
        assert db.query(User).count() == 2
        assert db.get(User, invited_id).status == UserStatus.ACTIVE
        binding = db.scalar(select(FeishuIdentityBinding).where(FeishuIdentityBinding.user_id == invited_id))
        assert binding.union_id == "on_invited"
        assert db.get(User, admin_id) is not None


def test_oauth_email_matches_and_binds_an_active_unbound_user(feishu_app) -> None:
    app, client = feishu_app
    seed_user(app)
    csrf = login(client)
    client.put(
        "/api/v1/settings/integrations/feishu",
        json=config_payload(),
        headers=write_headers(csrf),
    )
    client.post("/api/v1/auth/logout", headers=write_headers(csrf))
    app.state.feishu_provider.identity = OAuthIdentity(
        "on_unknown", "ou_unknown", "admin@example.test", "tenant"
    )
    authorized = client.post(
        "/api/v1/auth/feishu/authorize",
        json={"organization_slug": "acme"},
        headers={"Origin": "https://hr.example.test"},
    ).json()["data"]

    response = client.get(
        "/api/v1/auth/feishu/callback",
        params={"code": "unknown-code", "state": authorized["state"]},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/?feishu_status=connected"
    assert "hr_session=" in response.headers["set-cookie"]
    with app.state.identity_store.sync_session() as db:
        assert db.query(User).count() == 1
        binding = db.scalar(select(FeishuIdentityBinding))
        assert binding is not None
        assert binding.user_id == db.scalar(select(User.id))


def test_authenticated_user_can_bind_read_status_and_unbind(feishu_app) -> None:
    app, client = feishu_app
    user_id = seed_user(app)
    csrf = login(client)
    client.put(
        "/api/v1/settings/integrations/feishu",
        json=config_payload(),
        headers=write_headers(csrf),
    )
    app.state.feishu_provider.identity = OAuthIdentity("on_admin", "ou_admin", None, "tenant")

    authorized = client.post(
        "/api/v1/me/integrations/feishu/authorize", headers=write_headers(csrf)
    )
    state = authorized.json()["data"]["state"]
    callback = client.get(
        "/api/v1/auth/feishu/callback",
        params={"code": "bind-code", "state": state},
        follow_redirects=False,
    )
    assert callback.status_code == 303
    assert callback.headers["location"] == "/?feishu_status=bound"
    status = client.get(
        "/api/v1/me/integrations/feishu", headers={"Sec-Fetch-Site": "same-origin"}
    )
    assert status.json()["data"] == {"bound": True, "union_id": "on_admin", "open_id": "ou_admin"}
    assert client.delete(
        "/api/v1/me/integrations/feishu", headers=write_headers(csrf)
    ).status_code == 204
    with app.state.identity_store.sync_session() as db:
        assert db.scalar(select(FeishuIdentityBinding).where(FeishuIdentityBinding.user_id == user_id)) is None


def test_freebusy_api_chunks_provider_calls_without_real_network(feishu_app) -> None:
    app, client = feishu_app
    seed_user(app)
    csrf = login(client)
    client.put(
        "/api/v1/settings/integrations/feishu",
        json=config_payload(),
        headers=write_headers(csrf),
    )
    response = client.post(
        "/api/v1/integrations/feishu/freebusy",
        json={
            "open_ids": [f"ou_{index}" for index in range(11)],
            "time_min": "2026-07-01T00:00:00Z",
            "time_max": "2026-07-16T00:00:00Z",
        },
        headers=write_headers(csrf),
    )

    assert response.status_code == 200
    assert response.json()["data"] == []
    assert len(app.state.feishu_provider.freebusy_requests) == 4
    assert max(len(item.user_ids) for item in app.state.feishu_provider.freebusy_requests) == 10


def test_connection_test_releases_database_session_before_provider_calls(
    feishu_app, monkeypatch
) -> None:
    app, client = feishu_app
    _, csrf = configure_feishu(feishu_app)
    original_session = app.state.identity_store.sync_session
    active_sessions = 0

    @contextmanager
    def tracked_session():
        nonlocal active_sessions
        with original_session() as db:
            active_sessions += 1
            try:
                yield db
            finally:
                active_sessions -= 1

    def test_connection(credentials):
        assert active_sessions == 0
        return ConnectionResult(True, 1)

    def send_message(credentials, open_id, text, *, idempotency_key):
        assert active_sessions == 0

    monkeypatch.setattr(app.state.identity_store, "sync_session", tracked_session)
    monkeypatch.setattr(app.state.feishu_provider, "test_connection", test_connection)
    monkeypatch.setattr(app.state.feishu_provider, "send_message", send_message)

    response = client.post(
        "/api/v1/settings/integrations/feishu/test", headers=write_headers(csrf)
    )

    assert response.status_code == 200
    assert response.json()["data"]["last_test_status"] == "succeeded"


def test_connection_test_does_not_persist_stale_result_after_concurrent_config_change(
    feishu_app, monkeypatch
) -> None:
    app, client = feishu_app
    organization_id, csrf = configure_feishu(feishu_app)

    def change_config_during_request(credentials):
        with app.state.identity_store.sync_session() as db:
            config = db.scalar(
                select(FeishuOrganizationConfig).where(
                    FeishuOrganizationConfig.organization_id == organization_id
                )
            )
            config.app_id = "cli_reconfigured"
            config.version += 1
            db.commit()
        return ConnectionResult(True, 1)

    monkeypatch.setattr(
        app.state.feishu_provider, "test_connection", change_config_during_request
    )

    response = client.post(
        "/api/v1/settings/integrations/feishu/test", headers=write_headers(csrf)
    )

    assert response.status_code == 409
    assert response.json()["code"] == "feishu_config_changed"
    with app.state.identity_store.sync_session() as db:
        config = db.scalar(
            select(FeishuOrganizationConfig).where(
                FeishuOrganizationConfig.organization_id == organization_id
            )
        )
    assert config.app_id == "cli_reconfigured"
    assert config.last_test_status is None


def test_feishu_plaintext_url_verification_returns_challenge(feishu_app) -> None:
    _, client = feishu_app
    configure_feishu(feishu_app)

    response = client.post(
        "/api/v1/integrations/feishu/events",
        json={
            "type": "url_verification",
            "token": "verification-value",
            "challenge": "challenge-value",
        },
    )

    assert response.status_code == 200
    assert response.json() == {"challenge": "challenge-value"}


def test_feishu_plaintext_webhook_rejects_invalid_verification_token(
    feishu_app,
) -> None:
    _, client = feishu_app
    configure_feishu(feishu_app)

    response = client.post(
        "/api/v1/integrations/feishu/events",
        json={
            "type": "url_verification",
            "token": "forged-token",
            "challenge": "must-not-leak",
        },
    )

    assert response.status_code == 403
    assert response.json()["code"] == "feishu_event_verification_failed"


def test_feishu_encrypted_url_verification_validates_signature_and_decrypts(
    feishu_app,
) -> None:
    _, client = feishu_app
    encrypt_key = "encrypt-key-value"
    configure_feishu(feishu_app, encrypt_key=encrypt_key)
    body, headers = encrypted_webhook(
        {
            "type": "url_verification",
            "token": "verification-value",
            "challenge": "encrypted-challenge",
        },
        encrypt_key,
    )

    response = client.post(
        "/api/v1/integrations/feishu/events", content=body, headers=headers
    )

    assert response.status_code == 200
    assert response.json() == {"challenge": "encrypted-challenge"}


def test_feishu_encrypted_url_verification_without_event_signature_headers(
    feishu_app,
) -> None:
    _, client = feishu_app
    configure_feishu(feishu_app, encrypt_key="encrypt-key-value")
    body, headers = encrypted_webhook(
        {
            "type": "url_verification",
            "token": "verification-value",
            "challenge": "initial-encrypted-challenge",
        },
        "encrypt-key-value",
    )
    headers = {"Content-Type": headers["Content-Type"]}

    response = client.post(
        "/api/v1/integrations/feishu/events",
        content=body,
        headers=headers,
    )

    assert response.status_code == 200
    assert response.json() == {"challenge": "initial-encrypted-challenge"}


def test_feishu_encrypted_webhook_rejects_invalid_signature(feishu_app) -> None:
    _, client = feishu_app
    encrypt_key = "encrypt-key-value"
    configure_feishu(feishu_app, encrypt_key=encrypt_key)
    body, headers = encrypted_webhook(
        {
            "type": "url_verification",
            "token": "verification-value",
            "challenge": "must-not-leak",
        },
        encrypt_key,
        signature_override="0" * 64,
    )

    response = client.post(
        "/api/v1/integrations/feishu/events", content=body, headers=headers
    )

    assert response.status_code == 403
    assert response.json()["code"] == "feishu_event_verification_failed"
    assert "must-not-leak" not in response.text


def test_feishu_encrypted_event_requires_signature_headers(feishu_app) -> None:
    _, client = feishu_app
    configure_feishu(feishu_app, encrypt_key="encrypt-key-value")
    body, headers = encrypted_webhook(
        {
            "schema": "2.0",
            "header": {
                "event_id": "evt-unsigned",
                "event_type": "calendar.calendar.event.changed_v4",
                "token": "verification-value",
                "app_id": "cli_test",
                "tenant_key": "tenant-key",
            },
            "event": {"calendar_id": "primary", "user_id_list": []},
        },
        "encrypt-key-value",
    )
    headers = {"Content-Type": headers["Content-Type"]}

    response = client.post(
        "/api/v1/integrations/feishu/events",
        content=body,
        headers=headers,
    )

    assert response.status_code == 403
    assert response.json()["code"] == "feishu_event_verification_failed"


@pytest.mark.parametrize(
    "signature_headers",
    [
        {"X-Lark-Signature": ""},
        {
            "X-Lark-Request-Timestamp": "",
            "X-Lark-Request-Nonce": "",
            "X-Lark-Signature": "",
        },
        {"X-Lark-Request-Timestamp": "1720000000"},
        {
            "X-Lark-Request-Timestamp": "1720000000",
            "X-Lark-Request-Nonce": "nonce-value",
        },
    ],
)
def test_feishu_encrypted_url_verification_rejects_partial_or_empty_signature_headers(
    feishu_app,
    signature_headers,
) -> None:
    _, client = feishu_app
    configure_feishu(feishu_app, encrypt_key="encrypt-key-value")
    body, _ = encrypted_webhook(
        {
            "type": "url_verification",
            "token": "verification-value",
            "challenge": "must-not-leak",
        },
        "encrypt-key-value",
    )

    response = client.post(
        "/api/v1/integrations/feishu/events",
        content=body,
        headers={"Content-Type": "application/json", **signature_headers},
    )

    assert response.status_code == 403
    assert response.json()["code"] == "feishu_event_verification_failed"
    assert "must-not-leak" not in response.text


def test_feishu_encrypt_key_configuration_rejects_plaintext_webhook(
    feishu_app,
) -> None:
    _, client = feishu_app
    configure_feishu(feishu_app, encrypt_key="encrypt-key-value")

    response = client.post(
        "/api/v1/integrations/feishu/events",
        json={
            "type": "url_verification",
            "token": "verification-value",
            "challenge": "must-not-leak",
        },
    )

    assert response.status_code == 403
    assert response.json()["code"] == "feishu_event_verification_failed"


def test_feishu_v2_event_requires_app_and_tenant_identity(feishu_app) -> None:
    _, client = feishu_app
    configure_feishu(feishu_app)

    response = client.post(
        "/api/v1/integrations/feishu/events",
        json={
            "schema": "2.0",
            "header": {
                "event_id": "evt-missing-identity",
                "event_type": "calendar.calendar.changed_v4",
                "token": "verification-value",
                "tenant_key": "tenant-key",
            },
            "event": {},
        },
    )

    assert response.status_code == 403
    assert response.json()["code"] == "feishu_event_verification_failed"


def test_feishu_v2_event_uses_app_and_tenant_config_and_deduplicates_event_id(
    feishu_app,
) -> None:
    app, client = feishu_app
    organization_id, _ = configure_feishu(feishu_app)
    payload = {
        "schema": "2.0",
        "header": {
            "event_id": "evt-duplicate",
            "event_type": "calendar.calendar.event.changed_v4",
            "token": "verification-value",
            "app_id": "cli_test",
            "tenant_key": "tenant-key",
        },
        "event": {"calendar_id": "primary", "user_id_list": []},
        "organization_id": "00000000-0000-0000-0000-000000000001",
    }

    first = client.post("/api/v1/integrations/feishu/events", json=payload)
    second = client.post("/api/v1/integrations/feishu/events", json=payload)

    assert first.status_code == 200
    assert second.status_code == 200
    with app.state.identity_store.sync_session() as db:
        received = [
            log
            for log in db.scalars(
                select(AuditLog).where(AuditLog.event_type == "feishu.event_received")
            )
            if log.metadata_json.get("event_id") == "evt-duplicate"
        ]
    assert len(received) == 1
    assert received[0].organization_id == organization_id


def test_feishu_signed_encrypted_v2_event_is_accepted(feishu_app) -> None:
    app, client = feishu_app
    organization_id, _ = configure_feishu(
        feishu_app,
        encrypt_key="encrypt-key-value",
    )
    body, headers = encrypted_webhook(
        {
            "schema": "2.0",
            "header": {
                "event_id": "evt-encrypted-v2",
                "event_type": "calendar.calendar.event.changed_v4",
                "token": "verification-value",
                "app_id": "cli_test",
                "tenant_key": "tenant-key",
            },
            "event": {"calendar_id": "primary", "user_id_list": []},
        },
        "encrypt-key-value",
    )

    response = client.post(
        "/api/v1/integrations/feishu/events",
        content=body,
        headers=headers,
    )

    assert response.status_code == 200
    with app.state.identity_store.sync_session() as db:
        log = db.scalar(
            select(AuditLog).where(
                AuditLog.organization_id == organization_id,
                AuditLog.event_type == "feishu.event_received",
            )
        )
    assert log is not None
    assert log.metadata_json["event_id"] == "evt-encrypted-v2"
    assert log.metadata_json["action"] == "incremental_sync_required"


def test_feishu_v2_event_rejects_wrong_tenant_even_with_valid_token(feishu_app) -> None:
    _, client = feishu_app
    configure_feishu(feishu_app)

    response = client.post(
        "/api/v1/integrations/feishu/events",
        json={
            "schema": "2.0",
            "header": {
                "event_id": "evt-wrong-tenant",
                "event_type": "calendar.calendar.event.changed_v4",
                "token": "verification-value",
                "app_id": "cli_test",
                "tenant_key": "forged-tenant",
            },
            "event": {},
        },
    )

    assert response.status_code == 403
    assert response.json()["code"] == "feishu_event_verification_failed"


def test_calendar_event_change_is_recorded_for_incremental_sync_without_guessing_an_event(
    feishu_app,
) -> None:
    app, client = feishu_app
    organization_id, _ = configure_feishu(feishu_app)

    response = client.post(
        "/api/v1/integrations/feishu/events",
        json={
            "schema": "2.0",
            "header": {
                "event_id": "evt-calendar-list-changed",
                "event_type": "calendar.calendar.event.changed_v4",
                "token": "verification-value",
                "app_id": "cli_test",
                "tenant_key": "tenant-key",
            },
            "event": {"calendar_id": "primary"},
        },
    )

    assert response.status_code == 200
    with app.state.identity_store.sync_session() as db:
        log = db.scalar(
            select(AuditLog).where(
                AuditLog.organization_id == organization_id,
                AuditLog.event_type == "feishu.event_received",
            )
        )
    assert log is not None
    assert log.metadata_json == {
        "event_id": "evt-calendar-list-changed",
        "event_type": "calendar.calendar.event.changed_v4",
        "action": "incremental_sync_required",
        "calendar_id": "primary",
    }
