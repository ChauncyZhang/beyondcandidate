from datetime import timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from server.app.identity.models import AuditLog, PasswordInvitation, User, UserRole, UserSession, UserStatus
from server.app.identity.security import hash_token
from server.app.identity.service import InvalidSession
from server.tests.test_identity_management import login, management_app, seed_user


ORIGIN = {"Origin": "https://hr.example.test"}
NEW_PASSWORD = "replacement secure password"


def issue(client, user_id, headers):
    return client.post(f"/api/v1/settings/users/{user_id}/password-reset", headers=headers)


def accept(client, token, password=NEW_PASSWORD, headers=ORIGIN):
    return client.post("/api/v1/auth/password-reset/accept", json={"token": token, "password": password}, headers=headers)


def setup_reset(app, client):
    seed_user(app, role="system_admin", email="admin@example.test")
    target = seed_user(app, role="recruiter", email="target@example.test")
    headers = login(client, "admin@example.test")
    response = issue(client, target.user_id, headers)
    assert response.status_code == 200
    return target, headers, response.json()["data"]["password_reset"]["token"]


def test_issue_preserves_account_sessions_and_invitations_and_replaces_only_resets(management_app):
    app, client, clock = management_app
    seed_user(app, role="system_admin", email="admin@example.test")
    target = seed_user(app, role="recruiter", email="target@example.test")
    login(client, "target@example.test")
    with app.state.identity_store.sync_session() as db:
        user = db.get(User, target.user_id)
        user.failed_login_count = 5
        user.failed_login_window_started_at = clock.now
        user.locked_until = clock.now + timedelta(minutes=15)
        invitation = PasswordInvitation(organization_id=target.organization_id, user_id=target.user_id, token_hash=hash_token("activation-token-not-for-reset"), expires_at=clock.now + timedelta(hours=48))
        db.add(invitation)
        db.commit()
        db.refresh(user)
        original = (user.password_hash, user.status, user.authorization_version, user.failed_login_count, user.locked_until)
    headers = login(client, "admin@example.test")
    first = issue(client, target.user_id, headers)
    assert first.status_code == 200
    assert first.headers["Cache-Control"] == "no-store"
    first_token = first.json()["data"]["password_reset"]["token"]
    assert first.json()["data"]["password_reset"]["expires_at"] == (clock.now + timedelta(hours=1)).isoformat()
    second = issue(client, target.user_id, headers)
    assert second.status_code == 200
    second_token = second.json()["data"]["password_reset"]["token"]
    assert first_token != second_token
    with app.state.identity_store.sync_session() as db:
        user = db.get(User, target.user_id)
        assert (user.password_hash, user.status, user.authorization_version, user.failed_login_count, user.locked_until) == original
        assert all(s.revoked_at is None for s in db.scalars(select(UserSession)))
        invitation = db.scalar(select(PasswordInvitation).where(PasswordInvitation.purpose == "invitation"))
        assert invitation.used_at is None
        assert app.state.identity_service._aware(invitation.expires_at) == clock.now + timedelta(hours=48)
        audits = db.scalars(select(AuditLog).where(AuditLog.event_type == "identity.password_reset_issued")).all()
        assert len(audits) == 2
        assert all(a.resource_id == target.user_id and a.trace_id for a in audits)
        assert all(first_token not in repr(a.metadata_json) and hash_token(first_token) not in repr(a.metadata_json) for a in audits)
    assert accept(client, first_token).json()["code"] == "password_reset_invalid_or_expired"
    assert accept(client, second_token).status_code == 200


def test_accept_replaces_password_clears_lockout_revokes_every_session_and_does_not_login(management_app):
    app, client, clock = management_app
    target, _, token = setup_reset(app, client)
    with TestClient(app) as target_client:
        login(target_client, "target@example.test")
        login(target_client, "target@example.test")
        with app.state.identity_store.sync_session() as db:
            user = db.get(User, target.user_id)
            user.failed_login_count = 5
            user.failed_login_window_started_at = clock.now
            user.locked_until = clock.now + timedelta(minutes=15)
            db.add(PasswordInvitation(organization_id=target.organization_id, user_id=target.user_id, purpose="password_reset", reset_authorization_version=user.authorization_version, token_hash=hash_token("sibling-reset-token-value"), expires_at=clock.now + timedelta(hours=1)))
            db.commit()
        with TestClient(app) as anonymous:
            response = accept(anonymous, token)
            assert response.status_code == 200
            assert response.json() == {"data": {"email": "target@example.test", "organization_slug": "acme"}}
            assert "set-cookie" not in response.headers
            assert anonymous.get("/api/v1/me", headers={"Sec-Fetch-Site": "same-origin"}).status_code == 401
        assert target_client.get("/api/v1/me", headers={"Sec-Fetch-Site": "same-origin"}).status_code == 401
    with app.state.identity_store.sync_session() as db:
        user = db.get(User, target.user_id)
        assert user.status == UserStatus.ACTIVE
        assert user.authorization_version == 2
        assert user.failed_login_count == 0
        assert user.failed_login_window_started_at is None and user.locked_until is None
        sessions = db.scalars(select(UserSession).where(UserSession.user_id == target.user_id)).all()
        assert len(sessions) == 2
        assert all(s.revoked_at is not None and s.revocation_reason == "password_reset" for s in sessions)
        reset = db.scalar(select(PasswordInvitation).where(PasswordInvitation.token_hash == hash_token(token)))
        assert reset.used_at is not None
        audit = db.scalar(select(AuditLog).where(AuditLog.event_type == "identity.password_reset_accepted"))
        assert audit.resource_id == target.user_id and audit.trace_id
        assert token not in repr(audit.metadata_json) and NEW_PASSWORD not in repr(audit.metadata_json)
    for invalid in (token, "sibling-reset-token-value", "unknown-reset-token-value"):
        response = accept(client, invalid)
        assert response.status_code == 422
        assert response.json()["code"] == "password_reset_invalid_or_expired"
    payload = {"organization_slug": "acme", "email": "target@example.test", "password": "correct horse battery"}
    assert client.post("/api/v1/auth/login", json=payload, headers=ORIGIN).status_code == 401
    payload["password"] = NEW_PASSWORD
    assert client.post("/api/v1/auth/login", json=payload, headers=ORIGIN).status_code == 200


@pytest.mark.parametrize("role,expected", [("system_admin", 403), ("recruiting_admin", 403), ("recruiter", 200), ("hiring_manager", 200), ("interviewer", 200)])
def test_recruiting_admin_target_roles(management_app, role, expected):
    app, client, _ = management_app
    seed_user(app, role="recruiting_admin", email="admin@example.test")
    target = seed_user(app, role=role, email="target@example.test")
    assert issue(client, target.user_id, login(client, "admin@example.test")).status_code == expected


@pytest.mark.parametrize("role", ["system_admin", "recruiting_admin", "recruiter", "hiring_manager", "interviewer"])
def test_system_admin_can_reset_each_allowed_active_role(management_app, role):
    app, client, _ = management_app
    seed_user(app, role="system_admin", email="admin@example.test")
    target = seed_user(app, role=role, email="target@example.test")
    assert issue(client, target.user_id, login(client, "admin@example.test")).status_code == 200


@pytest.mark.parametrize("changed_user", ["actor", "target"])
def test_issue_rechecks_current_roles_after_principal_resolution(management_app, monkeypatch, changed_user):
    app, client, _ = management_app
    actor = seed_user(app, role="recruiting_admin", email="admin@example.test")
    target = seed_user(app, role="recruiter", email="target@example.test")
    headers = login(client, "admin@example.test")
    service = app.state.identity_service
    original_principal = service.principal

    def role_change_before_lock(token):
        principal = original_principal(token)
        with app.state.identity_store.sync_session() as db:
            changed = db.get(User, actor.user_id if changed_user == "actor" else target.user_id)
            if changed_user == "actor":
                changed.roles = [UserRole(role="recruiter")]
            else:
                changed.roles.append(UserRole(role="system_admin"))
            db.commit()
        return principal

    monkeypatch.setattr(service, "principal", role_change_before_lock)
    assert issue(client, target.user_id, headers).status_code == 403
    with app.state.identity_store.sync_session() as db:
        assert db.query(PasswordInvitation).count() == 0


def test_issue_denies_self_foreign_missing_nonadmin_csrf_and_nonactive(management_app):
    app, client, _ = management_app
    admin = seed_user(app, role="system_admin", email="admin@example.test")
    target = seed_user(app, role="recruiter", email="target@example.test")
    other = seed_user(app, role="recruiter", email="other@example.test", organization_slug="other")
    headers = login(client, "admin@example.test")
    assert issue(client, target.user_id, {}).status_code == 403
    assert issue(client, target.user_id, {**headers, "Origin": "https://evil.test"}).status_code == 403
    assert issue(client, admin.user_id, headers).status_code == 409
    assert issue(client, other.user_id, headers).status_code == 404
    assert issue(client, uuid4(), headers).status_code == 404
    for status in (UserStatus.DISABLED, UserStatus.INVITED):
        with app.state.identity_store.sync_session() as db:
            db.get(User, target.user_id).status = status
            db.commit()
        assert issue(client, target.user_id, headers).status_code == 409
    with app.state.identity_store.sync_session() as db:
        db.get(User, target.user_id).status = UserStatus.ACTIVE
        db.commit()
    assert issue(client, admin.user_id, login(client, "target@example.test")).status_code == 403
    with app.state.identity_store.sync_session() as db:
        assert db.query(PasswordInvitation).count() == 0


@pytest.mark.parametrize("invalid", ["expired", "disabled", "invited", "wrong_purpose"])
def test_accept_generic_error_and_no_mutation_for_invalid_reset(management_app, invalid):
    app, client, clock = management_app
    target, _, token = setup_reset(app, client)
    with app.state.identity_store.sync_session() as db:
        user = db.get(User, target.user_id)
        reset = db.scalar(select(PasswordInvitation).where(PasswordInvitation.token_hash == hash_token(token)))
        original_hash = user.password_hash
        if invalid == "expired":
            reset.expires_at = clock.now
        elif invalid == "wrong_purpose":
            reset.purpose = "invitation"
        else:
            user.status = UserStatus(invalid)
        db.commit()
    response = accept(client, token)
    assert response.status_code == 422
    assert response.json()["code"] == "password_reset_invalid_or_expired"
    with app.state.identity_store.sync_session() as db:
        assert db.get(User, target.user_id).password_hash == original_hash
        assert db.scalar(select(PasswordInvitation).where(PasswordInvitation.token_hash == hash_token(token))).used_at is None


def test_activation_rejects_reset_even_when_target_invited(management_app):
    app, client, _ = management_app
    target, _, token = setup_reset(app, client)
    with app.state.identity_store.sync_session() as db:
        db.get(User, target.user_id).status = UserStatus.INVITED
        db.commit()
    response = client.post("/api/v1/auth/invitations/accept", json={"token": token, "password": NEW_PASSWORD}, headers=ORIGIN)
    assert response.status_code == 422
    assert response.json()["code"] == "invitation_invalid_or_expired"


@pytest.mark.parametrize("password", ["x" * 11, "x" * 129])
def test_reset_password_bounds_do_not_consume_token(management_app, password):
    app, client, _ = management_app
    _, _, token = setup_reset(app, client)
    assert accept(client, token, password).status_code == 422
    assert accept(client, token, headers={"Origin": "https://evil.test"}).status_code == 403
    assert accept(client, token).status_code == 200


def test_reset_issued_before_target_promotion_cannot_take_over_promoted_account(management_app):
    app, client, _ = management_app
    seed_user(app, role="system_admin", email="system@example.test")
    seed_user(app, role="recruiting_admin", email="admin@example.test")
    target = seed_user(app, role="recruiter", email="target@example.test")
    token = issue(client, target.user_id, login(client, "admin@example.test")).json()["data"]["password_reset"]["token"]
    promoted = client.patch(
        f"/api/v1/settings/users/{target.user_id}/recruiting-scope",
        json={"role": "system_admin", "recruiting_scope_type": "jobs"},
        headers=login(client, "system@example.test"),
    )
    assert promoted.status_code == 200
    response = accept(client, token)
    assert response.status_code == 422
    assert response.json()["code"] == "password_reset_invalid_or_expired"
    assert login(client, "target@example.test")


def test_password_change_invalidates_previously_issued_reset(management_app):
    app, client, _ = management_app
    _, _, token = setup_reset(app, client)
    response = client.post(
        "/api/v1/me/password",
        json={"current_password": "correct horse battery", "new_password": "member selected password"},
        headers=login(client, "target@example.test"),
    )
    assert response.status_code == 204
    assert accept(client, token).json()["code"] == "password_reset_invalid_or_expired"


def test_completed_reset_blocks_inflight_password_change_with_stale_session(management_app, monkeypatch):
    app, client, _ = management_app
    target, _, reset_token = setup_reset(app, client)
    login(client, "target@example.test")
    session_token = client.cookies.get("hr_session")
    service = app.state.identity_service
    original_resolve = service._resolve_session
    interleaved = False

    def resolve_then_complete_reset(db, token, **kwargs):
        nonlocal interleaved
        record = original_resolve(db, token, **kwargs)
        if not interleaved:
            interleaved = True
            service.accept_password_reset(reset_token, NEW_PASSWORD, trace_id="controlled-reset-interleave")
        return record

    monkeypatch.setattr(service, "_resolve_session", resolve_then_complete_reset)
    with pytest.raises(InvalidSession):
        service.change_password(session_token, "correct horse battery", "stale session overwrite", trace_id="controlled-change-interleave", network=None)
    assert interleaved
    with app.state.identity_store.sync_session() as db:
        user = db.get(User, target.user_id)
        assert service.passwords.verify(user.password_hash, NEW_PASSWORD)
        assert not service.passwords.verify(user.password_hash, "stale session overwrite")
        assert user.authorization_version == 2
        assert db.scalar(select(UserSession).where(UserSession.token_hash == hash_token(session_token))).revoked_at is not None
