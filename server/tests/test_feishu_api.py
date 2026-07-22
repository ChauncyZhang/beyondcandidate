from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from server.app.core.settings import Settings
from server.app.identity.models import Organization, User, UserRole, UserStatus
from server.app.identity.security import PasswordService
from server.app.identity.service import Clock, TokenSource
from server.app.integrations.feishu.models import FeishuIdentityBinding
from server.app.integrations.feishu.provider import FakeFeishuProvider, OAuthIdentity
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


def test_config_is_disabled_by_default_and_never_returns_plaintext(feishu_app) -> None:
    app, client = feishu_app
    seed_user(app)
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

    tested = client.post(
        "/api/v1/settings/integrations/feishu/test",
        headers=write_headers(csrf),
    )
    assert tested.status_code == 200
    assert tested.json()["data"]["last_test_status"] == "succeeded"


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
