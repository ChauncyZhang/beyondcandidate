from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from server.app.identity.models import AuditLog, Base, User, UserSession
from server.app.identity.security import PasswordService, hash_token
from server.tests.test_identity_management import login, management_app, seed_user


def invite_user(client: TestClient, *, email: str = "invited@example.test") -> str:
    response = client.post(
        "/api/v1/settings/users",
        json={
            "display_name": "Invited User",
            "email": email,
            "department_id": None,
            "role": "recruiter",
        },
        headers={**login(client, "admin@example.test"), "Idempotency-Key": "ignored-key"},
    )
    assert response.status_code == 201
    return response.json()["data"]["invitation"]["token"]


def test_password_invitation_model_uses_only_a_sha256_token_digest() -> None:
    table = Base.metadata.tables.get("password_invitations")

    assert table is not None
    assert {column.name for column in table.columns} == {
        "id",
        "organization_id",
        "user_id",
        "token_hash",
        "expires_at",
        "used_at",
        "created_at",
    }
    assert table.c.token_hash.type.length == 64
    assert table.c.token_hash.unique


def test_invited_user_cannot_login_and_public_accept_requires_only_allowed_origin(
    management_app,
) -> None:
    app, client, _ = management_app
    seed_user(app, role="system_admin", email="admin@example.test")
    token = invite_user(client)

    denied_login = client.post(
        "/api/v1/auth/login",
        json={
            "organization_slug": "acme",
            "email": "invited@example.test",
            "password": token,
        },
        headers={"Origin": "https://hr.example.test"},
    )
    disallowed_origin = client.post(
        "/api/v1/auth/invitations/accept",
        json={"token": token, "password": "new secure password"},
        headers={"Origin": "https://evil.test"},
    )
    accepted = client.post(
        "/api/v1/auth/invitations/accept",
        json={"token": token, "password": "new secure password"},
        headers={"Origin": "https://hr.example.test"},
    )

    assert denied_login.status_code == 401
    assert disallowed_origin.status_code == 403
    assert accepted.status_code == 200
    assert accepted.json() == {
        "data": {"email": "invited@example.test", "organization_slug": "acme"}
    }

    with app.state.identity_store.sync_session() as db:
        user = db.query(User).filter_by(normalized_email="invited@example.test").one()
        assert user.status.value == "active"
        assert PasswordService().verify(user.password_hash, "new secure password")
        invitation = db.execute(
            select(app.state.identity_store.PasswordInvitationModel)
        ).scalar_one()
        assert invitation.used_at is not None
        audit = db.query(AuditLog).filter_by(
            event_type="identity.password_invitation_accepted"
        ).one()
        rendered = repr(audit.metadata_json)
        assert audit.category == "system"
        assert "invited@example.test" not in rendered
        assert token not in rendered
        assert hash_token(token) not in rendered

    reused = client.post(
        "/api/v1/auth/invitations/accept",
        json={"token": token, "password": "another secure password"},
        headers={"Origin": "https://hr.example.test"},
    )
    invalid = client.post(
        "/api/v1/auth/invitations/accept",
        json={"token": "unknown-token-value-with-enough-length", "password": "another secure password"},
        headers={"Origin": "https://hr.example.test"},
    )
    for response in (reused, invalid):
        assert response.status_code == 422
        assert response.json()["code"] == "invitation_invalid_or_expired"


def test_expired_invitation_has_the_same_safe_error(management_app) -> None:
    app, client, clock = management_app
    seed_user(app, role="system_admin", email="admin@example.test")
    token = invite_user(client, email="expired@example.test")
    clock.now += timedelta(hours=49)

    response = client.post(
        "/api/v1/auth/invitations/accept",
        json={"token": token, "password": "new secure password"},
        headers={"Origin": "https://hr.example.test"},
    )

    assert response.status_code == 422
    assert response.json()["code"] == "invitation_invalid_or_expired"


@pytest.mark.parametrize("length", [11, 129])
def test_invitation_password_requires_12_to_128_characters(
    management_app, length: int
) -> None:
    app, client, _ = management_app
    seed_user(app, role="system_admin", email="admin@example.test")
    token = invite_user(client)

    response = client.post(
        "/api/v1/auth/invitations/accept",
        json={"token": token, "password": "x" * length},
        headers={"Origin": "https://hr.example.test"},
    )

    assert response.status_code == 422
    with app.state.identity_store.sync_session() as db:
        assert db.query(User).filter_by(normalized_email="invited@example.test").one().status.value == "invited"


def test_password_change_keeps_current_session_and_revokes_other_sessions(
    management_app,
) -> None:
    app, client, _ = management_app
    seeded = seed_user(app, role="recruiting_admin", email="admin@example.test")
    first_headers = login(client, "admin@example.test")
    first_cookie = client.cookies.get("hr_session")
    with TestClient(app) as other_client:
        login(other_client, "admin@example.test")

        response = client.post(
            "/api/v1/me/password",
            json={
                "current_password": "correct horse battery",
                "new_password": "new secure password",
            },
            headers=first_headers,
        )

        assert response.status_code == 204
        assert client.get(
            "/api/v1/me", headers={"Sec-Fetch-Site": "same-origin"}
        ).status_code == 200
        assert other_client.get(
            "/api/v1/me", headers={"Sec-Fetch-Site": "same-origin"}
        ).status_code == 401

    with app.state.identity_store.sync_session() as db:
        user = db.get(User, seeded.user_id)
        assert user.authorization_version == 2
        current = db.scalar(
            select(UserSession).where(UserSession.token_hash == hash_token(first_cookie))
        )
        assert current.authorization_version == 2
        assert current.revoked_at is None
        other = db.scalar(
            select(UserSession).where(UserSession.id != current.id)
        )
        assert other.revoked_at is not None
        assert other.revocation_reason == "password_changed"
        audit = db.query(AuditLog).filter_by(event_type="authentication.password_changed").one()
        rendered = repr(audit.metadata_json)
        assert audit.category == "system"
        assert "correct horse battery" not in rendered
        assert "new secure password" not in rendered

    assert client.post(
        "/api/v1/auth/login",
        json={
            "organization_slug": "acme",
            "email": "admin@example.test",
            "password": "correct horse battery",
        },
        headers={"Origin": "https://hr.example.test"},
    ).status_code == 401


def test_password_change_rejects_wrong_current_and_same_password(management_app) -> None:
    app, client, _ = management_app
    seed_user(app, role="recruiting_admin", email="admin@example.test")
    headers = login(client, "admin@example.test")

    wrong = client.post(
        "/api/v1/me/password",
        json={"current_password": "wrong password", "new_password": "new secure password"},
        headers=headers,
    )
    same = client.post(
        "/api/v1/me/password",
        json={
            "current_password": "correct horse battery",
            "new_password": "correct horse battery",
        },
        headers=headers,
    )

    assert wrong.status_code == 422
    assert wrong.json()["code"] == "current_password_invalid"
    assert same.status_code == 422
    assert same.json()["code"] == "password_unchanged"
