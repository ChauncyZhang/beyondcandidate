from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from server.app.core.settings import Settings
from server.app.identity.models import Organization, User, UserRole, UserStatus
from server.app.identity.security import PasswordService, hash_token
from server.app.identity.service import Clock, TokenSource
from server.app.main import create_app


class Probe:
    async def check(self) -> None:
        pass


class FrozenClock(Clock):
    def __init__(self) -> None:
        self.now = datetime(2026, 7, 12, 8, tzinfo=timezone.utc)

    def current_time(self) -> datetime:
        return self.now

    def advance(self, **kwargs: int) -> None:
        self.now += timedelta(**kwargs)


class SequenceTokens(TokenSource):
    def __init__(self) -> None:
        self.values = iter(f"token-{index:064d}" for index in range(100))

    def new_token(self) -> str:
        return next(self.values)


@pytest.fixture
def identity_app(tmp_path):
    settings = Settings(
        environment="test",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'identity.db'}",
        cors_origins=["https://hr.example.test"],
    )
    clock = FrozenClock()
    tokens = SequenceTokens()
    app = create_app(
        settings=settings,
        database_probe=Probe(),
        storage_probe=Probe(),
        clock=clock,
        token_source=tokens,
        initialize_identity_schema=True,
    )
    with TestClient(app) as client:
        yield app, client, clock


def seed_user(app, *, email="admin@example.test", password="correct horse", status=UserStatus.ACTIVE):
    password_service = PasswordService()
    with app.state.identity_store.sync_session() as session:
        organization = Organization(slug="acme", name="Acme", status="active")
        user = User(
            organization=organization,
            email=email,
            normalized_email=email.casefold(),
            display_name="Admin",
            password_hash=password_service.hash(password),
            status=status,
        )
        user.roles.append(UserRole(role="recruiting_admin"))
        session.add(user)
        session.commit()
        return user.id


def login(client, **overrides):
    body = {"organization_slug": "acme", "email": "admin@example.test", "password": "correct horse"}
    body.update(overrides)
    return client.post(
        "/api/v1/auth/login",
        json=body,
        headers={"Origin": "https://hr.example.test"},
    )


def get_me(client, *, fetch_site="same-origin", origin=None):
    headers = {"Sec-Fetch-Site": fetch_site} if fetch_site is not None else {}
    if origin is not None:
        headers["Origin"] = origin
    return client.get("/api/v1/me", headers=headers)


def test_public_auth_config_preserves_multi_tenant_login_when_default_is_unset(
    identity_app,
) -> None:
    _, client, _ = identity_app

    response = client.get("/api/v1/auth/config")

    assert response.status_code == 200
    assert response.json() == {"data": {"default_organization": None}}
    assert "X-Trace-ID" in response.headers


def test_public_auth_config_exposes_only_configured_default_organization(
    tmp_path,
) -> None:
    settings = Settings(
        environment="test",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'configured-identity.db'}",
        cors_origins=["https://hr.example.test"],
        default_organization_slug="acme",
        default_organization_name="Acme Recruiting",
        object_storage_secret_key="must-not-leak",
    )
    app = create_app(
        settings=settings,
        database_probe=Probe(),
        storage_probe=Probe(),
    )

    response = TestClient(app).get("/api/v1/auth/config")

    assert response.status_code == 200
    assert response.json() == {
        "data": {
            "default_organization": {
                "slug": "acme",
                "name": "Acme Recruiting",
            }
        }
    }
    assert "must-not-leak" not in response.text


def test_passwords_use_argon2id_and_verify() -> None:
    service = PasswordService()
    encoded = service.hash("correct horse")
    assert encoded.startswith("$argon2id$")
    assert service.verify(encoded, "correct horse")
    assert not service.verify(encoded, "wrong")


def test_login_persists_only_hashes_and_sets_host_cookie(identity_app) -> None:
    app, client, clock = identity_app
    seed_user(app)
    response = login(client)
    assert response.status_code == 200
    csrf = response.headers["X-CSRF-Token"]
    cookie = response.headers["set-cookie"]
    assert "hr_session=" in cookie and "__Host-hr_session=" not in cookie
    assert "HttpOnly" in cookie and "Path=/" in cookie and "SameSite=lax" in cookie
    assert "Secure" not in cookie
    assert "password" not in response.text.casefold()
    with app.state.identity_store.sync_session() as session:
        stored = session.query(app.state.identity_store.SessionModel).one()
        assert stored.token_hash == hash_token(client.cookies["hr_session"])
        assert stored.csrf_token_hash == hash_token(csrf)
        assert client.cookies["hr_session"] not in repr(stored)
        assert csrf not in repr(stored)
        assert stored.idle_expires_at == clock.now.replace(tzinfo=None) + timedelta(hours=2)
        assert stored.absolute_expires_at == clock.now.replace(tzinfo=None) + timedelta(hours=12)


@pytest.mark.parametrize(
    "case",
    ["unknown_organization", "unknown_user", "wrong_password", "disabled"],
)
def test_login_failures_are_generic(identity_app, case) -> None:
    app, client, clock = identity_app
    if case not in {"unknown_organization", "unknown_user"}:
        user_id = seed_user(app, status=UserStatus.DISABLED if case == "disabled" else UserStatus.ACTIVE)
    overrides = {
        "unknown_organization": {"organization_slug": "missing"},
        "unknown_user": {"email": "missing@example.test"},
        "wrong_password": {"password": "wrong"},
    }.get(case, {})
    response = login(client, **overrides)
    assert response.status_code == 401
    assert response.json()["code"] == "authentication_failed"
    assert response.json()["detail"] == "Invalid credentials or account unavailable."


def test_five_failures_lock_for_fifteen_minutes_and_success_resets(identity_app) -> None:
    app, client, clock = identity_app
    user_id = seed_user(app)
    for _ in range(4):
        assert login(client, password="wrong").status_code == 401
    assert login(client).status_code == 200
    with app.state.identity_store.sync_session() as session:
        user = session.get(User, user_id)
        assert user.failed_login_count == 0 and user.locked_until is None
    for _ in range(4):
        assert login(client, password="wrong").status_code == 401
    locked = login(client, password="wrong")
    assert locked.status_code == 429
    assert locked.json()["code"] == "account_temporarily_locked"
    assert locked.json()["retry_after_seconds"] == 900
    assert locked.headers["Retry-After"] == "900"

    clock.advance(minutes=4, seconds=59)
    still_locked = login(client)
    assert still_locked.status_code == 429
    assert still_locked.json()["retry_after_seconds"] == 601
    assert still_locked.headers["Retry-After"] == "601"

    clock.advance(minutes=10, seconds=2)
    assert login(client).status_code == 200


def test_me_rotates_csrf_refreshes_idle_but_not_absolute(identity_app) -> None:
    app, client, clock = identity_app
    seed_user(app)
    initial = login(client)
    old_csrf = initial.headers["X-CSRF-Token"]
    with app.state.identity_store.sync_session() as session:
        original = session.query(app.state.identity_store.SessionModel).one()
        absolute = original.absolute_expires_at
        idle = original.idle_expires_at
    clock.advance(minutes=31)
    response = get_me(client)
    assert response.status_code == 200
    new_csrf = response.headers["X-CSRF-Token"]
    assert new_csrf != old_csrf
    assert response.json()["data"]["roles"] == ["recruiting_admin"]
    assert "session" not in response.text.casefold() and "password" not in response.text.casefold()
    with app.state.identity_store.sync_session() as session:
        refreshed = session.query(app.state.identity_store.SessionModel).one()
        assert refreshed.csrf_token_hash == hash_token(new_csrf)
        assert refreshed.idle_expires_at == clock.now.replace(tzinfo=None) + timedelta(hours=2)
        assert refreshed.idle_expires_at > idle
        assert refreshed.absolute_expires_at == absolute


def test_principal_business_access_refreshes_idle_but_not_absolute(identity_app) -> None:
    app, client, clock = identity_app
    seed_user(app)
    assert login(client).status_code == 200
    token = client.cookies["hr_session"]
    with app.state.identity_store.sync_session() as session:
        original = session.query(app.state.identity_store.SessionModel).one()
        absolute = original.absolute_expires_at

    clock.advance(minutes=31)
    principal = app.state.identity_service.principal(token)

    assert principal.active
    with app.state.identity_store.sync_session() as session:
        refreshed = session.query(app.state.identity_store.SessionModel).one()
        assert refreshed.idle_expires_at == clock.now.replace(tzinfo=None) + timedelta(hours=2)
        assert refreshed.absolute_expires_at == absolute


def test_valid_csrf_business_access_refreshes_idle_without_duplicate_write(identity_app) -> None:
    app, client, clock = identity_app
    seed_user(app)
    response = login(client)
    token = client.cookies["hr_session"]
    csrf = response.headers["X-CSRF-Token"]
    with app.state.identity_store.sync_session() as session:
        absolute = session.query(app.state.identity_store.SessionModel).one().absolute_expires_at

    clock.advance(minutes=31)
    assert app.state.identity_service.validate_csrf(
        token, csrf, trace_id="csrf-renewal", network="127.0.0.1"
    )
    with app.state.identity_store.sync_session() as session:
        renewed = session.query(app.state.identity_store.SessionModel).one()
        renewed_idle = renewed.idle_expires_at
        assert renewed_idle == clock.now.replace(tzinfo=None) + timedelta(hours=2)
        assert renewed.absolute_expires_at == absolute

    clock.advance(minutes=1)
    assert app.state.identity_service.validate_csrf(
        token, csrf, trace_id="csrf-no-rewrite", network="127.0.0.1"
    )
    with app.state.identity_store.sync_session() as session:
        unchanged = session.query(app.state.identity_store.SessionModel).one()
        assert unchanged.idle_expires_at == renewed_idle
        assert unchanged.absolute_expires_at == absolute


def test_activity_renews_idle_to_absolute_when_final_extension_is_below_throttle(identity_app) -> None:
    app, client, clock = identity_app
    seed_user(app)
    assert login(client).status_code == 200
    token = client.cookies["hr_session"]
    with app.state.identity_store.sync_session() as session:
        stored = session.query(app.state.identity_store.SessionModel).one()
        absolute = stored.absolute_expires_at
        stored.idle_expires_at = absolute - timedelta(minutes=15)
        session.commit()

    clock.advance(hours=10)
    assert app.state.identity_service.principal(token).active

    with app.state.identity_store.sync_session() as session:
        renewed = session.query(app.state.identity_store.SessionModel).one()
        assert renewed.idle_expires_at == absolute
        assert renewed.absolute_expires_at == absolute


def test_idle_absolute_disable_and_authorization_version_revoke(identity_app) -> None:
    app, client, clock = identity_app
    user_id = seed_user(app)
    assert login(client).status_code == 200
    clock.advance(hours=2, seconds=1)
    assert get_me(client).status_code == 401
    assert login(client).status_code == 200
    with app.state.identity_store.sync_session() as session:
        user = session.get(User, user_id)
        user.authorization_version += 1
        session.commit()
    assert get_me(client).status_code == 401
    assert login(client).status_code == 200
    with app.state.identity_store.sync_session() as session:
        user = session.get(User, user_id)
        user.status = UserStatus.DISABLED
        session.commit()
    assert get_me(client).status_code == 401


def test_absolute_expiry_is_not_extended_by_activity(identity_app) -> None:
    app, client, clock = identity_app
    seed_user(app)
    assert login(client).status_code == 200
    for _ in range(23):
        clock.advance(minutes=29)
        assert get_me(client).status_code == 200
    clock.advance(hours=1)
    assert get_me(client).status_code == 401


def test_logout_requires_origin_and_matching_csrf_then_revokes(identity_app) -> None:
    app, client, _ = identity_app
    seed_user(app)
    csrf = login(client).headers["X-CSRF-Token"]
    for headers in ({}, {"Origin": "https://evil.test", "X-CSRF-Token": csrf}, {"Origin": "https://hr.example.test", "X-CSRF-Token": "wrong"}):
        response = client.post("/api/v1/auth/logout", headers=headers)
        assert response.status_code == 403
        assert response.json()["code"] == "csrf_validation_failed"
    response = client.post(
        "/api/v1/auth/logout",
        headers={"Origin": "https://hr.example.test", "X-CSRF-Token": csrf},
    )
    assert response.status_code == 204
    assert get_me(client).status_code == 401
    with app.state.identity_store.sync_session() as session:
        events = session.query(app.state.identity_store.AuditModel).filter_by(event_type="authentication.logout").all()
        assert [event.outcome for event in events] == ["denied", "denied", "denied", "success"]


def test_production_cookie_is_secure(identity_app) -> None:
    app, _, _ = identity_app
    seed_user(app)
    app.state.settings.environment = "production"
    with TestClient(app, base_url="https://hr.example.test") as client:
        response = login(client)
    assert "__Host-hr_session=" in response.headers["set-cookie"]
    assert "Secure" in response.headers["set-cookie"]


def test_me_rejects_cross_site_fetch_before_mutating_session(identity_app) -> None:
    app, client, _ = identity_app
    seed_user(app)
    csrf = login(client).headers["X-CSRF-Token"]
    response = get_me(client, fetch_site="cross-site")
    assert response.status_code == 403
    with app.state.identity_store.sync_session() as session:
        stored = session.query(app.state.identity_store.SessionModel).one()
        assert stored.csrf_token_hash == hash_token(csrf)


@pytest.mark.parametrize("fetch_site", [None, "none", "unexpected"])
def test_me_rejects_missing_or_unrecognized_fetch_metadata(identity_app, fetch_site) -> None:
    app, client, _ = identity_app
    seed_user(app)
    csrf = login(client).headers["X-CSRF-Token"]
    response = get_me(client, fetch_site=fetch_site)
    assert response.status_code == 403
    with app.state.identity_store.sync_session() as session:
        assert session.query(app.state.identity_store.SessionModel).one().csrf_token_hash == hash_token(csrf)


@pytest.mark.parametrize("fetch_site", ["same-origin", "same-site"])
def test_me_allows_recognized_same_site_metadata(identity_app, fetch_site) -> None:
    app, client, _ = identity_app
    seed_user(app)
    login(client)
    assert get_me(client, fetch_site=fetch_site).status_code == 200


def test_me_rejects_disallowed_origin_even_with_same_origin_metadata(identity_app) -> None:
    app, client, _ = identity_app
    seed_user(app)
    login(client)
    assert get_me(client, origin="https://evil.test").status_code == 403


def test_csrf_middleware_protects_future_state_changing_routes(identity_app) -> None:
    app, client, _ = identity_app
    from fastapi import APIRouter

    extra = APIRouter(prefix="/api/v1")

    @extra.post("/future-write")
    def future_write():
        return {"written": True}

    app.include_router(extra)
    response = client.post("/api/v1/future-write", headers={"Origin": "https://hr.example.test"})
    assert response.status_code == 403
    assert response.json()["code"] == "csrf_validation_failed"
    assert response.headers["X-Trace-ID"] == response.json()["trace_id"]


def test_anonymous_csrf_failures_do_not_create_audit_rows(identity_app) -> None:
    app, client, _ = identity_app
    for _ in range(5):
        assert client.post("/api/v1/unknown-write").status_code == 403
        assert client.post("/api/v1/unknown-write", headers={"Origin": "https://hr.example.test", "X-CSRF-Token": "wrong"}).status_code == 403
    with app.state.identity_store.sync_session() as session:
        assert session.query(app.state.identity_store.AuditModel).count() == 0


def test_authenticated_wrong_csrf_creates_redacted_audit(identity_app) -> None:
    app, client, _ = identity_app
    seed_user(app)
    login(client)
    response = client.post("/api/v1/unknown-write", headers={"Origin": "https://hr.example.test", "X-CSRF-Token": "wrong"})
    assert response.status_code == 403
    with app.state.identity_store.sync_session() as session:
        audit = session.query(app.state.identity_store.AuditModel).filter_by(event_type="csrf.denied").one()
        assert audit.actor_user_id is not None
        assert audit.metadata_json.keys() == {"network_id"}


@pytest.mark.parametrize(
    ("invalid_kind", "expected_reason"),
    [
        ("stale_authorization", "authorization_version_stale"),
        ("disabled_user", "user_disabled"),
        ("expired", "idle_expired"),
    ],
)
def test_state_changing_request_revokes_known_invalid_session_once(identity_app, invalid_kind, expected_reason) -> None:
    app, client, clock = identity_app
    user_id = seed_user(app)
    login(client)
    with app.state.identity_store.sync_session() as session:
        if invalid_kind == "stale_authorization":
            session.get(User, user_id).authorization_version += 1
        elif invalid_kind == "disabled_user":
            session.get(User, user_id).status = UserStatus.DISABLED
        session.commit()
    if invalid_kind == "expired":
        clock.advance(hours=2, seconds=1)

    headers = {"Origin": "https://hr.example.test", "X-CSRF-Token": "wrong"}
    assert client.post("/api/v1/unknown-write", headers=headers).status_code == 403
    assert client.post("/api/v1/unknown-write", headers=headers).status_code == 403

    with app.state.identity_store.sync_session() as session:
        stored = session.query(app.state.identity_store.SessionModel).one()
        assert stored.revoked_at is not None
        assert stored.revocation_reason == expected_reason
        events = session.query(app.state.identity_store.AuditModel).filter_by(event_type="session.invalidated").all()
        assert len(events) == 1
        assert events[0].actor_user_id == user_id
        assert events[0].metadata_json.keys() == {"network_id"}


def test_state_changing_request_with_unknown_token_creates_no_rows(identity_app) -> None:
    app, client, _ = identity_app
    client.cookies.set("hr_session", "random-unknown-token")
    headers = {"Origin": "https://hr.example.test", "X-CSRF-Token": "wrong"}
    assert client.post("/api/v1/unknown-write", headers=headers).status_code == 403
    with app.state.identity_store.sync_session() as session:
        assert session.query(app.state.identity_store.SessionModel).count() == 0
        assert session.query(app.state.identity_store.AuditModel).count() == 0


def test_audit_events_do_not_contain_credentials_tokens_or_full_ip(identity_app) -> None:
    app, client, _ = identity_app
    seed_user(app)
    response = client.post(
        "/api/v1/auth/login",
        json={"organization_slug": "acme", "email": "admin@example.test", "password": "correct horse"},
        headers={"Origin": "https://hr.example.test", "X-Forwarded-For": "203.0.113.9, 10.0.0.1"},
    )
    csrf = response.headers["X-CSRF-Token"]
    token = client.cookies["hr_session"]
    with app.state.identity_store.sync_session() as session:
        audit = session.query(app.state.identity_store.AuditModel).one()
        rendered = repr(audit.metadata_json)
        assert "correct horse" not in rendered
        assert csrf not in rendered and token not in rendered
        assert "203.0.113.9" not in rendered and "10.0.0.1" not in rendered
