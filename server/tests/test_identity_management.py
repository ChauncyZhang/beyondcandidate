from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from server.app.core.settings import Settings
from server.app.identity.models import (
    AuditLog,
    Department,
    Job,
    Organization,
    User,
    UserRole,
    UserStatus,
)
from server.app.identity.security import PasswordService, hash_token
from server.app.identity.service import Clock, TokenSource
from server.app.main import create_app


class Probe:
    async def check(self) -> None:
        pass


class FrozenClock(Clock):
    def __init__(self) -> None:
        self.now = datetime(2026, 7, 16, 8, tzinfo=timezone.utc)

    def current_time(self) -> datetime:
        return self.now


class SequenceTokens(TokenSource):
    def __init__(self) -> None:
        self.values = iter(f"token-{index:064d}" for index in range(100))

    def new_token(self) -> str:
        return next(self.values)


@dataclass(frozen=True)
class SeededIdentity:
    user_id: object
    organization_id: object


@pytest.fixture
def management_app(tmp_path):
    clock = FrozenClock()
    app = create_app(
        settings=Settings(
            environment="test",
            database_url=f"sqlite+aiosqlite:///{tmp_path / 'identity-management.db'}",
            cors_origins=["https://hr.example.test"],
        ),
        database_probe=Probe(),
        storage_probe=Probe(),
        clock=clock,
        token_source=SequenceTokens(),
        initialize_identity_schema=True,
    )
    with TestClient(app) as client:
        yield app, client, clock


def seed_user(
    app,
    *,
    role: str,
    email: str,
    organization_slug: str = "acme",
    department: Department | None = None,
) -> SeededIdentity:
    with app.state.identity_store.sync_session() as db:
        organization = db.query(Organization).filter_by(slug=organization_slug).one_or_none()
        if organization is None:
            organization = Organization(
                slug=organization_slug,
                name=organization_slug.title(),
                status="active",
            )
            db.add(organization)
            db.flush()
        user = User(
            organization_id=organization.id,
            department_id=department.id if department else None,
            email=email,
            normalized_email=email.casefold(),
            display_name=role.replace("_", " ").title(),
            password_hash=PasswordService().hash("correct horse battery"),
            status=UserStatus.ACTIVE,
        )
        user.roles.append(UserRole(role=role))
        db.add(user)
        db.commit()
        return SeededIdentity(user.id, organization.id)


def login(client: TestClient, email: str, organization_slug: str = "acme") -> dict[str, str]:
    response = client.post(
        "/api/v1/auth/login",
        json={
            "organization_slug": organization_slug,
            "email": email,
            "password": "correct horse battery",
        },
        headers={"Origin": "https://hr.example.test"},
    )
    assert response.status_code == 200
    return {
        "Origin": "https://hr.example.test",
        "X-CSRF-Token": response.headers["X-CSRF-Token"],
    }


@pytest.mark.parametrize("role", ["system_admin", "recruiting_admin"])
def test_admin_roles_create_and_list_tenant_departments(management_app, role: str) -> None:
    app, client, _ = management_app
    actor = seed_user(app, role=role, email=f"{role}@example.test")
    with app.state.identity_store.sync_session() as db:
        other = Organization(slug="other", name="Other", status="active")
        db.add(other)
        db.flush()
        db.add(Department(organization_id=other.id, name="Other tenant"))
        db.commit()

    headers = login(client, f"{role}@example.test")
    created = client.post(
        "/api/v1/settings/departments",
        json={"name": "Engineering", "parent_id": None},
        headers=headers,
    )

    assert created.status_code == 201
    assert created.headers["Cache-Control"] == "no-store"
    department = created.json()["data"]
    assert department == {
        "id": department["id"],
        "name": "Engineering",
        "parent_id": None,
        "status": "active",
        "member_count": 0,
        "job_count": 0,
    }

    with app.state.identity_store.sync_session() as db:
        member = User(
            organization_id=actor.organization_id,
            department_id=UUID(department["id"]),
            email="member@example.test",
            normalized_email="member@example.test",
            display_name="Member",
            password_hash=PasswordService().hash("member password value"),
        )
        member.roles.append(UserRole(role="recruiter"))
        db.add(member)
        db.flush()
        db.add(
            Job(
                organization_id=actor.organization_id,
                department_id=UUID(department["id"]),
                title="Backend Engineer",
                owner_id=actor.user_id,
                status="open",
            )
        )
        db.commit()

    listed = client.get("/api/v1/settings/departments")
    assert listed.status_code == 200
    assert listed.headers["Cache-Control"] == "no-store"
    assert listed.json() == {
        "data": [
            {
                **department,
                "member_count": 1,
                "job_count": 1,
            }
        ]
    }
    with app.state.identity_store.sync_session() as db:
        audit = db.query(AuditLog).filter_by(event_type="organization.department_created").one()
        assert audit.organization_id == actor.organization_id
        assert audit.category == "system"
        assert audit.metadata_json == {}


def test_admin_can_view_rename_disable_and_reenable_department(management_app) -> None:
    app, client, _ = management_app
    actor = seed_user(app, role="recruiting_admin", email="admin@example.test")
    with app.state.identity_store.sync_session() as db:
        department = Department(organization_id=actor.organization_id, name="Engineering")
        db.add(department)
        db.flush()
        member = User(
            organization_id=actor.organization_id,
            department_id=department.id,
            email="member@example.test",
            normalized_email="member@example.test",
            display_name="Member",
            password_hash=PasswordService().hash("member password value"),
        )
        member.roles.append(UserRole(role="hiring_manager"))
        db.add(member)
        db.add(Job(
            organization_id=actor.organization_id,
            department_id=department.id,
            title="Backend Engineer",
            owner_id=actor.user_id,
            status="open",
        ))
        db.commit()
        department_id = department.id

    headers = login(client, "admin@example.test")
    detail = client.get(f"/api/v1/settings/departments/{department_id}", headers=headers)
    assert detail.status_code == 200
    assert detail.json()["data"] == {
        "id": str(department_id),
        "name": "Engineering",
        "parent_id": None,
        "status": "active",
        "member_count": 1,
        "job_count": 1,
        "members": [{
            "id": detail.json()["data"]["members"][0]["id"],
            "name": "Member",
            "roles": ["hiring_manager"],
            "status": "active",
        }],
        "jobs": [{
            "id": detail.json()["data"]["jobs"][0]["id"],
            "title": "Backend Engineer",
            "status": "open",
        }],
    }

    disabled = client.patch(
        f"/api/v1/settings/departments/{department_id}",
        json={"name": "Platform", "status": "inactive"},
        headers=headers,
    )
    assert disabled.status_code == 200
    assert disabled.json()["data"]["name"] == "Platform"
    assert disabled.json()["data"]["status"] == "inactive"

    rejected_invite = client.post(
        "/api/v1/settings/users",
        json={
            "display_name": "New Member",
            "email": "new-member@example.test",
            "department_id": str(department_id),
            "role": "hiring_manager",
        },
        headers=headers,
    )
    assert rejected_invite.status_code == 422
    assert rejected_invite.json()["code"] == "department_invalid"

    rejected_job = client.post(
        "/api/v1/jobs",
        json={
            "title": "Inactive Department Job",
            "department_id": str(department_id),
            "headcount": 1,
            "priority": "normal",
            "hiring_owner_id": None,
        },
        headers=headers,
    )
    assert rejected_job.status_code == 422
    assert rejected_job.json()["code"] == "department_invalid"

    enabled = client.patch(
        f"/api/v1/settings/departments/{department_id}",
        json={"status": "active"},
        headers=headers,
    )
    assert enabled.status_code == 200
    assert enabled.json()["data"]["status"] == "active"
    with app.state.identity_store.sync_session() as db:
        audit = db.query(AuditLog).filter_by(event_type="organization.department_updated").all()
        assert [row.metadata_json["status"] for row in audit] == ["inactive", "active"]


def test_department_duplicate_and_invalid_parent_have_stable_errors(management_app) -> None:
    app, client, _ = management_app
    seed_user(app, role="system_admin", email="admin@example.test")
    headers = login(client, "admin@example.test")

    first = client.post(
        "/api/v1/settings/departments",
        json={"name": "Engineering", "parent_id": None},
        headers=headers,
    )
    duplicate = client.post(
        "/api/v1/settings/departments",
        json={"name": "Engineering", "parent_id": None},
        headers=headers,
    )
    invalid_parent = client.post(
        "/api/v1/settings/departments",
        json={"name": "Platform", "parent_id": "00000000-0000-4000-8000-000000000001"},
        headers=headers,
    )

    assert first.status_code == 201
    assert duplicate.status_code == 409
    assert duplicate.json()["code"] == "department_already_exists"
    assert duplicate.headers["Cache-Control"] == "no-store"
    assert invalid_parent.status_code == 422
    assert invalid_parent.json()["code"] == "department_invalid"


def test_recruiter_can_read_organization_directory_but_cannot_manage_it(management_app) -> None:
    app, client, _ = management_app
    actor = seed_user(app, role="recruiter", email="recruiter@example.test")
    with app.state.identity_store.sync_session() as db:
        db.add(Department(organization_id=actor.organization_id, name="Engineering"))
        db.commit()
    headers = login(client, "recruiter@example.test")

    departments = client.get("/api/v1/settings/departments")
    users = client.get("/api/v1/settings/users")
    create_department = client.post(
        "/api/v1/settings/departments",
        json={"name": "Forbidden", "parent_id": None},
        headers=headers,
    )
    invite_user = client.post(
        "/api/v1/settings/users",
        json={
            "display_name": "Forbidden",
            "email": "forbidden@example.test",
            "department_id": None,
            "role": "recruiter",
        },
        headers=headers,
    )

    assert departments.status_code == 200
    assert [item["name"] for item in departments.json()["data"]] == ["Engineering"]
    assert users.status_code == 200
    assert [item["email"] for item in users.json()["data"]] == ["recruiter@example.test"]
    assert create_department.status_code == 403
    assert invite_user.status_code == 403


def test_user_invitation_is_once_only_and_user_listing_is_tenant_scoped(management_app) -> None:
    app, client, clock = management_app
    actor = seed_user(app, role="system_admin", email="admin@example.test")
    with app.state.identity_store.sync_session() as db:
        department = Department(organization_id=actor.organization_id, name="Engineering")
        db.add(department)
        db.commit()
        department_id = department.id
    headers = login(client, "admin@example.test")

    response = client.post(
        "/api/v1/settings/users",
        json={
            "display_name": "Invited Recruiter",
            "email": "Invite@Example.Test",
            "department_id": str(department_id),
            "role": "recruiter",
        },
        headers={**headers, "Idempotency-Key": "accepted-but-not-replayed"},
    )

    assert response.status_code == 201
    assert response.headers["Cache-Control"] == "no-store"
    body = response.json()["data"]
    token = body["invitation"]["token"]
    assert len(token) >= 43
    assert body["invitation"]["expires_at"] == "2026-07-18T08:00:00+00:00"
    assert body["user"] == {
        "id": body["user"]["id"],
        "display_name": "Invited Recruiter",
        "email": "Invite@Example.Test",
        "department_id": str(department_id),
        "department_name": "Engineering",
        "roles": ["recruiter"],
        "status": "invited",
    }

    with app.state.identity_store.sync_session() as db:
        invited = db.query(User).filter_by(normalized_email="invite@example.test").one()
        invitation = db.execute(
            app.state.identity_store.PasswordInvitationModel.__table__.select()
        ).mappings().one()
        assert invited.status.value == "invited"
        assert PasswordService().verify(invited.password_hash, token) is False
        assert invitation["token_hash"] == hash_token(token)
        assert token not in repr(invitation)
        audit = db.query(AuditLog).filter_by(event_type="identity.user_invited").one()
        rendered = repr(audit.metadata_json)
        assert audit.actor_user_id == actor.user_id
        assert audit.category == "system"
        assert "Invite@Example.Test" not in rendered
        assert "Invited Recruiter" not in rendered
        assert token not in rendered
        assert hash_token(token) not in rendered

    listed = client.get("/api/v1/settings/users")
    assert listed.status_code == 200
    assert listed.headers["Cache-Control"] == "no-store"
    assert body["user"] in listed.json()["data"]

    duplicate = client.post(
        "/api/v1/settings/users",
        json={
            "display_name": "Invited Recruiter",
            "email": "Invite@Example.Test",
            "department_id": str(department_id),
            "role": "recruiter",
        },
        headers={**headers, "Idempotency-Key": "accepted-but-not-replayed"},
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["code"] == "user_email_already_exists"
    with app.state.identity_store.sync_session() as db:
        assert db.query(User).filter_by(normalized_email="invite@example.test").count() == 1
        assert db.query(app.state.identity_store.PasswordInvitationModel).count() == 1


@pytest.mark.parametrize("forbidden_role", ["system_admin", "recruiting_admin"])
def test_recruiting_admin_cannot_grant_admin_roles(management_app, forbidden_role: str) -> None:
    app, client, _ = management_app
    seed_user(app, role="recruiting_admin", email="admin@example.test")
    headers = login(client, "admin@example.test")

    response = client.post(
        "/api/v1/settings/users",
        json={
            "display_name": "Escalation",
            "email": f"{forbidden_role}@example.test",
            "department_id": None,
            "role": forbidden_role,
        },
        headers=headers,
    )

    assert response.status_code == 403
    assert response.json()["code"] == "role_assignment_forbidden"
    assert response.headers["Cache-Control"] == "no-store"


def test_me_includes_current_tenant_department(management_app) -> None:
    app, client, _ = management_app
    with app.state.identity_store.sync_session() as db:
        organization = Organization(slug="acme", name="Acme", status="active")
        db.add(organization)
        db.flush()
        department = Department(organization_id=organization.id, name="Engineering")
        db.add(department)
        db.flush()
        department_id = department.id
        db.commit()
    seed_user(
        app,
        role="recruiting_admin",
        email="admin@example.test",
        department=department,
    )
    login(client, "admin@example.test")

    response = client.get("/api/v1/me", headers={"Sec-Fetch-Site": "same-origin"})

    assert response.status_code == 200
    assert response.json()["data"]["department"] == {
        "id": str(department_id),
        "name": "Engineering",
    }


@pytest.mark.parametrize("department_kind", ["missing", "other_tenant"])
def test_new_job_rejects_invalid_department_with_stable_422(
    management_app, department_kind: str
) -> None:
    app, _, _ = management_app
    seed_user(app, role="recruiting_admin", email="admin@example.test")
    department_id = "00000000-0000-4000-8000-000000000001"
    if department_kind == "other_tenant":
        with app.state.identity_store.sync_session() as db:
            other = Organization(slug="other", name="Other", status="active")
            db.add(other)
            db.flush()
            department = Department(organization_id=other.id, name="Other tenant")
            db.add(department)
            db.commit()
            department_id = str(department.id)

    with TestClient(app, raise_server_exceptions=False) as client:
        headers = login(client, "admin@example.test")
        response = client.post(
            "/api/v1/jobs",
            json={
                "title": "Backend Engineer",
                "department_id": department_id,
                "headcount": 1,
                "priority": "normal",
                "hiring_owner_id": None,
            },
            headers=headers,
        )

    assert response.status_code == 422
    assert response.json()["code"] == "department_invalid"


def test_new_job_definition_rejects_invalid_department_with_stable_422(
    management_app,
) -> None:
    app, _, _ = management_app
    seed_user(app, role="recruiting_admin", email="admin@example.test")
    with TestClient(app, raise_server_exceptions=False) as client:
        headers = login(client, "admin@example.test")
        response = client.post(
            "/api/v1/job-definitions",
            json={
                "title": "Platform Engineer",
                "department_id": "00000000-0000-4000-8000-000000000001",
                "headcount": 1,
                "priority": "normal",
                "hiring_owner_id": None,
                "description": "Build systems.",
                "location": "Shanghai",
                "process_template": "standard",
                "llm_enabled": False,
                "must_have": [],
                "nice_to_have": [],
                "publish": False,
            },
            headers={**headers, "Idempotency-Key": "invalid-department"},
        )

    assert response.status_code == 422
    assert response.json()["code"] == "department_invalid"
