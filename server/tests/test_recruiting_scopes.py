from __future__ import annotations

from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from server.app.identity.models import (
    AuditLog,
    Department,
    Job,
    JobCollaborator,
    Organization,
    User,
    UserRecruitingDepartmentScope,
    UserRole,
    UserStatus,
)
from server.app.identity.policy import (
    JobGrant,
    Permission,
    Principal,
    require_job_access,
)
from server.app.identity.security import PasswordService
from server.app.recruiting.authorization import (
    RecruitingAction,
    RecruitingAuthorizationService,
)
from server.tests.test_identity_management import login, management_app, seed_user


AUTH = RecruitingAuthorizationService()


def _add_department(app, organization_id, name: str, *, status: str = "active"):
    with app.state.identity_store.sync_session() as db:
        department = Department(
            organization_id=organization_id,
            name=name,
            status=status,
        )
        db.add(department)
        db.commit()
        return department.id


def _add_job(app, organization_id, owner_id, title: str, department_id=None):
    with app.state.identity_store.sync_session() as db:
        job = Job(
            organization_id=organization_id,
            owner_id=owner_id,
            title=title,
            department_id=department_id,
            status="open",
        )
        db.add(job)
        db.commit()
        return job.id


def _visible_job_ids(app, principal: Principal):
    with app.state.identity_store.sync_session() as db:
        return set(
            db.scalars(
                select(Job.id).where(
                    AUTH.job_predicate(principal, RecruitingAction.READ, Job)
                )
            ).all()
        )


def test_historical_user_defaults_to_jobs_scope_in_user_listing(management_app) -> None:
    app, client, _ = management_app
    seed_user(app, role="system_admin", email="admin@example.test")
    historical = seed_user(app, role="recruiter", email="historical@example.test")
    headers = login(client, "admin@example.test")

    response = client.get("/api/v1/settings/users", headers=headers)

    assert response.status_code == 200
    row = next(item for item in response.json()["data"] if item["id"] == str(historical.user_id))
    assert row["recruiting_scope_type"] == "jobs"
    assert row["recruiting_department_ids"] == []


def test_system_admin_can_convert_existing_admin_to_department_scoped_recruiter(
    management_app,
) -> None:
    app, client, _ = management_app
    actor = seed_user(app, role="system_admin", email="admin@example.test")
    target = seed_user(app, role="recruiting_admin", email="hr@example.test")
    engineering = _add_department(app, actor.organization_id, "Engineering")
    headers = login(client, "admin@example.test")

    response = client.patch(
        f"/api/v1/settings/users/{target.user_id}/recruiting-scope",
        json={
            "role": "recruiter",
            "recruiting_scope_type": "departments",
            "recruiting_department_ids": [str(engineering)],
        },
        headers=headers,
    )

    assert response.status_code == 200
    assert response.json()["data"]["roles"] == ["recruiter"]
    assert response.json()["data"]["recruiting_scope_type"] == "departments"
    assert response.json()["data"]["recruiting_department_ids"] == [
        str(engineering)
    ]
    with app.state.identity_store.sync_session() as db:
        stored = db.scalar(
            select(User)
            .options(
                selectinload(User.roles),
                selectinload(User.recruiting_department_scopes),
            )
            .where(User.id == target.user_id)
        )
        assert [role.role for role in stored.roles] == ["recruiter"]
        assert stored.authorization_version == 2
        audit = db.query(AuditLog).filter_by(
            event_type="identity.user_access_updated"
        ).one()
        assert audit.metadata_json["previous_roles"] == ["recruiting_admin"]
        assert audit.metadata_json["roles"] == ["recruiter"]


def test_member_role_changes_require_system_admin_and_cannot_change_self(
    management_app,
) -> None:
    app, client, _ = management_app
    system_admin = seed_user(app, role="system_admin", email="system@example.test")
    recruiter = seed_user(app, role="recruiter", email="recruiter@example.test")
    recruiting_admin = seed_user(
        app, role="recruiting_admin", email="recruiting-admin@example.test"
    )

    system_headers = login(client, "system@example.test")
    self_change = client.patch(
        f"/api/v1/settings/users/{system_admin.user_id}/recruiting-scope",
        json={
            "role": "recruiter",
            "recruiting_scope_type": "organization",
            "recruiting_department_ids": [],
        },
        headers=system_headers,
    )
    assert self_change.status_code == 409
    assert self_change.json()["code"] == "self_role_change_forbidden"

    recruiting_admin_headers = login(client, "recruiting-admin@example.test")
    forbidden = client.patch(
        f"/api/v1/settings/users/{recruiter.user_id}/recruiting-scope",
        json={
            "role": "interviewer",
            "recruiting_scope_type": "jobs",
            "recruiting_department_ids": [],
        },
        headers=recruiting_admin_headers,
    )
    assert forbidden.status_code == 403
    assert forbidden.json()["code"] == "role_assignment_forbidden"


def test_system_admin_can_save_an_unchanged_member_role(management_app) -> None:
    app, client, _ = management_app
    seed_user(app, role="system_admin", email="system@example.test")
    target = seed_user(app, role="recruiting_admin", email="hr@example.test")
    headers = login(client, "system@example.test")

    response = client.patch(
        f"/api/v1/settings/users/{target.user_id}/recruiting-scope",
        json={
            "role": "recruiting_admin",
            "recruiting_scope_type": "jobs",
            "recruiting_department_ids": [],
        },
        headers=headers,
    )

    assert response.status_code == 200
    assert response.json()["data"]["roles"] == ["recruiting_admin"]


def test_job_predicate_unions_explicit_multiple_departments_and_organization_scope(
    management_app,
) -> None:
    app, client, _ = management_app
    recruiter = seed_user(app, role="recruiter", email="recruiter@example.test")
    owner = seed_user(app, role="recruiting_admin", email="owner@example.test")
    engineering = _add_department(app, recruiter.organization_id, "Engineering")
    product = _add_department(app, recruiter.organization_id, "Product")
    unrelated = _add_department(app, recruiter.organization_id, "Finance")
    engineering_job = _add_job(app, recruiter.organization_id, owner.user_id, "Backend", engineering)
    product_job = _add_job(app, recruiter.organization_id, owner.user_id, "PM", product)
    unrelated_job = _add_job(app, recruiter.organization_id, owner.user_id, "Finance", unrelated)
    explicit_job = _add_job(app, recruiter.organization_id, owner.user_id, "Explicit", unrelated)
    with app.state.identity_store.sync_session() as db:
        db.add(
            JobCollaborator(
                organization_id=recruiter.organization_id,
                job_id=explicit_job,
                user_id=recruiter.user_id,
                access_role="job_recruiter",
            )
        )
        other = Organization(slug="other", name="Other", status="active")
        db.add(other)
        db.flush()
        other_owner = User(
            organization_id=other.id,
            email="owner@other.test",
            normalized_email="owner@other.test",
            display_name="Other owner",
            password_hash=PasswordService().hash("correct horse battery"),
            status=UserStatus.ACTIVE,
        )
        other_owner.roles.append(UserRole(role="recruiting_admin"))
        db.add(other_owner)
        db.flush()
        cross_tenant_job = Job(
            organization_id=other.id,
            owner_id=other_owner.id,
            title="Other tenant",
            status="open",
        )
        db.add(cross_tenant_job)
        scoped_user = db.get(User, recruiter.user_id)
        scoped_user.recruiting_scope_type = "departments"
        scoped_user.recruiting_department_scopes = [
            UserRecruitingDepartmentScope(
                organization_id=recruiter.organization_id,
                department_id=department_id,
            )
            for department_id in (engineering, product)
        ]
        db.commit()
        cross_tenant_job_id = cross_tenant_job.id

    jobs_principal = Principal(
        recruiter.user_id,
        recruiter.organization_id,
        frozenset({"recruiter"}),
        True,
    )
    departments_principal = Principal(
        recruiter.user_id,
        recruiter.organization_id,
        frozenset({"recruiter"}),
        True,
        "departments",
        frozenset({engineering, product}),
    )
    organization_principal = Principal(
        recruiter.user_id,
        recruiter.organization_id,
        frozenset({"recruiter"}),
        True,
        "organization",
    )

    assert _visible_job_ids(app, jobs_principal) == {explicit_job}
    assert _visible_job_ids(app, departments_principal) == {
        engineering_job,
        product_job,
        explicit_job,
    }
    assert _visible_job_ids(app, organization_principal) == {
        engineering_job,
        product_job,
        unrelated_job,
        explicit_job,
    }
    assert cross_tenant_job_id not in _visible_job_ids(app, organization_principal)

    login(client, "recruiter@example.test")
    response = client.get("/api/v1/jobs")
    assert response.status_code == 200
    assert sorted(item["id"] for item in response.json()["data"]) == sorted(
        str(job_id) for job_id in (engineering_job, product_job, explicit_job)
    )


def test_legacy_job_access_entry_honors_department_and_organization_scopes() -> None:
    organization_id = uuid4()
    department_id = uuid4()
    job_id = uuid4()
    recruiter_id = uuid4()
    department_principal = Principal(
        recruiter_id,
        organization_id,
        frozenset({"recruiter"}),
        True,
        "departments",
        frozenset({department_id}),
    )
    organization_principal = Principal(
        recruiter_id,
        organization_id,
        frozenset({"recruiter"}),
        True,
        "organization",
    )
    unrelated_grant = JobGrant(uuid4(), job_id, organization_id, "job_recruiter")

    assert require_job_access(
        department_principal,
        job_id,
        organization_id,
        Permission.READ_RECRUITING,
        [unrelated_grant],
        job_department_id=department_id,
    )
    assert not require_job_access(
        department_principal,
        job_id,
        organization_id,
        Permission.READ_RECRUITING,
        [unrelated_grant],
        job_department_id=uuid4(),
    )
    assert require_job_access(
        organization_principal,
        job_id,
        organization_id,
        Permission.READ_RECRUITING,
        [],
    )
    assert not require_job_access(
        organization_principal,
        job_id,
        uuid4(),
        Permission.READ_RECRUITING,
        [],
    )


def test_invite_and_patch_recruiting_scope_validate_tenant_status_and_audit(
    management_app,
) -> None:
    app, client, _ = management_app
    admin = seed_user(app, role="system_admin", email="admin@example.test")
    recruiting_admin = seed_user(
        app, role="recruiting_admin", email="recruiting-admin@example.test"
    )
    engineering = _add_department(app, admin.organization_id, "Engineering")
    product = _add_department(app, admin.organization_id, "Product")
    inactive = _add_department(app, admin.organization_id, "Inactive", status="inactive")
    other_admin = seed_user(
        app,
        role="system_admin",
        email="other-admin@example.test",
        organization_slug="other",
    )
    other_department = _add_department(app, other_admin.organization_id, "Other")
    headers = login(client, "admin@example.test")

    invited = client.post(
        "/api/v1/settings/users",
        json={
            "display_name": "Scoped Recruiter",
            "email": "scoped@example.test",
            "department_id": None,
            "role": "recruiter",
            "recruiting_scope_type": "departments",
            "recruiting_department_ids": [str(engineering), str(product)],
        },
        headers=headers,
    )
    assert invited.status_code == 201
    invited_user = invited.json()["data"]["user"]
    assert invited_user["recruiting_scope_type"] == "departments"
    assert invited_user["recruiting_department_ids"] == sorted(
        [str(engineering), str(product)]
    )

    for invalid_department in (inactive, other_department, uuid4()):
        rejected = client.post(
            "/api/v1/settings/users",
            json={
                "display_name": "Invalid",
                "email": f"invalid-{invalid_department}@example.test",
                "department_id": None,
                "role": "recruiter",
                "recruiting_scope_type": "departments",
                "recruiting_department_ids": [str(invalid_department)],
            },
            headers=headers,
        )
        assert rejected.status_code == 422
        assert rejected.json()["code"] == "recruiting_scope_invalid"

    non_recruiter = client.post(
        "/api/v1/settings/users",
        json={
            "display_name": "Manager",
            "email": "manager@example.test",
            "department_id": None,
            "role": "hiring_manager",
            "recruiting_scope_type": "organization",
            "recruiting_department_ids": [],
        },
        headers=headers,
    )
    assert non_recruiter.status_code == 422
    assert non_recruiter.json()["code"] == "recruiting_scope_invalid"

    recruiting_admin_headers = login(client, "recruiting-admin@example.test")
    repeated = client.patch(
        f"/api/v1/settings/users/{invited_user['id']}/recruiting-scope",
        json={
            "recruiting_scope_type": "departments",
            "recruiting_department_ids": [str(engineering), str(product)],
        },
        headers=recruiting_admin_headers,
    )
    assert repeated.status_code == 200
    assert repeated.json()["data"]["recruiting_department_ids"] == sorted(
        [str(engineering), str(product)]
    )
    patched = client.patch(
        f"/api/v1/settings/users/{invited_user['id']}/recruiting-scope",
        json={
            "recruiting_scope_type": "organization",
            "recruiting_department_ids": [],
        },
        headers=recruiting_admin_headers,
    )
    assert patched.status_code == 200
    assert patched.json()["data"]["recruiting_scope_type"] == "organization"
    assert patched.json()["data"]["recruiting_department_ids"] == []

    cross_tenant = client.patch(
        f"/api/v1/settings/users/{other_admin.user_id}/recruiting-scope",
        json={
            "recruiting_scope_type": "jobs",
            "recruiting_department_ids": [],
        },
        headers=recruiting_admin_headers,
    )
    assert cross_tenant.status_code == 404
    with app.state.identity_store.sync_session() as db:
        stored = db.get(User, UUID(invited_user["id"]))
        assert stored.recruiting_scope_type == "organization"
        assert db.scalars(
            select(UserRecruitingDepartmentScope).where(
                UserRecruitingDepartmentScope.user_id == stored.id
            )
        ).all() == []
        audits = db.query(AuditLog).filter_by(
            event_type="identity.user_recruiting_scope_updated"
        ).all()
        assert len(audits) == 2
        assert all(audit.actor_user_id == recruiting_admin.user_id for audit in audits)
        assert audits[-1].metadata_json["recruiting_scope_type"] == "organization"
