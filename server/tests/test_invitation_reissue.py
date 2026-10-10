from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from server.app.identity.models import AuditLog, PasswordInvitation, User, UserStatus
from server.app.identity.security import hash_token
from server.tests.test_identity_management import login, management_app, seed_user
from server.tests.test_password_invitations import invite_user


def invited_id(app):
    with app.state.identity_store.sync_session() as db:
        return db.scalar(select(User.id).where(User.normalized_email == "invited@example.test"))


@pytest.mark.parametrize("elapsed_hours", [0, 49])
def test_reissue_replaces_pending_link_without_changing_account(management_app, elapsed_hours):
    app, client, clock = management_app
    actor = seed_user(app, role="system_admin", email="admin@example.test")
    old_token = invite_user(client)
    user_id = invited_id(app)
    with app.state.identity_store.sync_session() as db:
        user = db.get(User, user_id)
        original = (user.password_hash, user.authorization_version, user.email, [r.role for r in user.roles])
    clock.now += timedelta(hours=elapsed_hours)
    response = client.post(f"/api/v1/settings/users/{user_id}/invitation", headers=login(client, "admin@example.test"))
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    invitation = response.json()["data"]["invitation"]
    new_token = invitation["token"]
    assert new_token != old_token
    assert invitation["expires_at"] == (clock.now + timedelta(hours=48)).isoformat()
    with app.state.identity_store.sync_session() as db:
        user = db.get(User, user_id)
        assert (user.password_hash, user.authorization_version, user.email, [r.role for r in user.roles]) == original
        assert user.status == UserStatus.INVITED
        assert db.scalar(select(PasswordInvitation).where(PasswordInvitation.token_hash == hash_token(old_token))).expires_at <= clock.now.replace(tzinfo=None)
        audit = db.scalar(select(AuditLog).where(AuditLog.event_type == "identity.user_reinvited"))
        assert audit.actor_user_id == actor.user_id
        assert audit.resource_id == user_id
        assert new_token not in repr(audit.metadata_json)
        assert hash_token(new_token) not in repr(audit.metadata_json)
    headers = {"Origin": "https://hr.example.test"}
    assert client.post("/api/v1/auth/invitations/accept", json={"token": old_token, "password": "new secure password"}, headers=headers).json()["code"] == "invitation_invalid_or_expired"
    assert client.post("/api/v1/auth/invitations/accept", json={"token": new_token, "password": "new secure password"}, headers=headers).status_code == 200
    assert client.post(f"/api/v1/settings/users/{user_id}/invitation", headers=login(client, "admin@example.test")).json()["code"] == "user_not_invited"


def test_repeated_reissue_only_latest_link_works(management_app):
    app, client, _ = management_app
    seed_user(app, role="system_admin", email="admin@example.test")
    tokens = [invite_user(client)]
    user_id = invited_id(app)
    headers = login(client, "admin@example.test")
    for _ in range(2):
        response = client.post(f"/api/v1/settings/users/{user_id}/invitation", headers=headers)
        assert response.status_code == 200
        tokens.append(response.json()["data"]["invitation"]["token"])
    for token in tokens[:-1]:
        assert client.post("/api/v1/auth/invitations/accept", json={"token": token, "password": "new secure password"}, headers={"Origin": "https://hr.example.test"}).status_code == 422
    assert client.post("/api/v1/auth/invitations/accept", json={"token": tokens[-1], "password": "new secure password"}, headers={"Origin": "https://hr.example.test"}).status_code == 200


def test_reissued_link_expires_at_new_deadline(management_app):
    app, client, clock = management_app
    seed_user(app, role="system_admin", email="admin@example.test")
    invite_user(client)
    user_id = invited_id(app)
    clock.now += timedelta(hours=49)
    response = client.post(f"/api/v1/settings/users/{user_id}/invitation", headers=login(client, "admin@example.test"))
    assert response.status_code == 200
    token = response.json()["data"]["invitation"]["token"]
    clock.now += timedelta(hours=48)
    response = client.post("/api/v1/auth/invitations/accept", json={"token": token, "password": "new secure password"}, headers={"Origin": "https://hr.example.test"})
    assert response.status_code == 422
    assert response.json()["code"] == "invitation_invalid_or_expired"
    with app.state.identity_store.sync_session() as db:
        assert db.get(User, user_id).status == UserStatus.INVITED


@pytest.mark.parametrize("status", [UserStatus.ACTIVE, UserStatus.DISABLED])
def test_reissue_never_resets_registered_or_disabled_user(management_app, status):
    app, client, _ = management_app
    seed_user(app, role="system_admin", email="admin@example.test")
    target = seed_user(app, role="recruiter", email="target@example.test")
    with app.state.identity_store.sync_session() as db:
        user = db.get(User, target.user_id)
        user.status = status
        original_hash = user.password_hash
        db.commit()
    response = client.post(f"/api/v1/settings/users/{target.user_id}/invitation", headers=login(client, "admin@example.test"))
    assert response.status_code == 409
    assert response.json()["code"] == "user_not_invited"
    with app.state.identity_store.sync_session() as db:
        assert db.get(User, target.user_id).password_hash == original_hash
        assert db.query(PasswordInvitation).count() == 0


def test_reissue_enforces_tenant_role_and_csrf(management_app):
    app, client, _ = management_app
    seed_user(app, role="system_admin", email="admin@example.test")
    invite_user(client)
    user_id = invited_id(app)
    seed_user(app, role="system_admin", email="other@example.test", organization_slug="other")
    seed_user(app, role="recruiter", email="recruiter@example.test")
    headers = login(client, "admin@example.test")
    assert client.post(f"/api/v1/settings/users/{user_id}/invitation").status_code == 403
    assert client.post(f"/api/v1/settings/users/{uuid4()}/invitation", headers=headers).status_code == 404
    assert client.post(f"/api/v1/settings/users/{user_id}/invitation", headers=login(client, "other@example.test", "other")).status_code == 404
    assert client.post(f"/api/v1/settings/users/{user_id}/invitation", headers=login(client, "recruiter@example.test")).status_code == 403
    with app.state.identity_store.sync_session() as db:
        assert db.query(PasswordInvitation).count() == 1


@pytest.mark.parametrize("target_role", ["recruiter", "system_admin", "recruiting_admin"])
def test_recruiting_admin_reissue_respects_invitable_roles(management_app, target_role):
    app, client, _ = management_app
    seed_user(app, role="system_admin", email="admin@example.test")
    response = client.post("/api/v1/settings/users", json={"display_name": "Target", "email": "invited@example.test", "department_id": None, "role": target_role}, headers=login(client, "admin@example.test"))
    assert response.status_code == 201
    user_id = UUID(response.json()["data"]["user"]["id"])
    seed_user(app, role="recruiting_admin", email="recruiting@example.test")
    response = client.post(f"/api/v1/settings/users/{user_id}/invitation", headers=login(client, "recruiting@example.test"))
    assert response.status_code == (200 if target_role == "recruiter" else 403)
    with app.state.identity_store.sync_session() as db:
        assert db.query(PasswordInvitation).count() == (2 if target_role == "recruiter" else 1)
