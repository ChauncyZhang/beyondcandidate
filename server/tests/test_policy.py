from uuid import uuid4

import pytest

from server.app.identity.models import AuditLog, Job, JobCollaborator, Organization, User, UserRole, UserStatus
from server.app.identity.policy import AuthorizationService, JobGrant, Permission, Principal, require_job_access, require_permission
from server.app.identity.security import PasswordService
from server.tests.test_identity import identity_app


@pytest.mark.parametrize(
    ("role", "permission", "allowed"),
    [
        ("system_admin", Permission.MANAGE_USERS, True),
        ("system_admin", Permission.READ_RECRUITING, False),
        ("recruiting_admin", Permission.READ_RECRUITING, True),
        ("recruiter", Permission.READ_RECRUITING, False),
        ("hiring_manager", Permission.BULK_EXPORT, False),
        ("interviewer", Permission.SEARCH_JOBS, False),
        ("unknown", Permission.MANAGE_USERS, False),
    ],
)
def test_global_role_matrix_fails_closed(role, permission, allowed) -> None:
    principal = Principal(uuid4(), uuid4(), frozenset({role}), True)
    assert require_permission(principal, permission) is allowed


def test_job_grants_are_organization_scoped_and_fail_closed() -> None:
    organization_id = uuid4()
    job_id = uuid4()
    principal = Principal(uuid4(), organization_id, frozenset({"recruiter"}), True)
    grants = [JobGrant(principal.user_id, job_id, organization_id, "job_recruiter")]
    assert require_job_access(principal, job_id, organization_id, Permission.READ_RECRUITING, grants)
    assert not require_job_access(principal, job_id, uuid4(), Permission.READ_RECRUITING, grants)
    assert not require_job_access(principal, uuid4(), organization_id, Permission.READ_RECRUITING, grants)
    assert not require_job_access(principal, job_id, organization_id, Permission.MANAGE_USERS, grants)


@pytest.mark.parametrize(
    ("role", "grant", "allowed"),
    [
        ("recruiting_admin", None, True),
        ("recruiter", "job_owner", True),
        ("recruiter", "job_recruiter", True),
        ("recruiter", "job_manager", False),
        ("hiring_manager", "job_manager", True),
        ("hiring_manager", "job_owner", False),
        ("interviewer", "job_manager", False),
        ("system_admin", "job_owner", False),
        ("unknown", "job_owner", False),
    ],
)
def test_explicit_role_grant_matrix(role, grant, allowed) -> None:
    principal = Principal(uuid4(), uuid4(), frozenset({role}), True)
    grants = [] if grant is None else [JobGrant(principal.user_id, uuid4(), principal.organization_id, grant)]
    job_id = grants[0].job_id if grants else uuid4()
    assert require_job_access(principal, job_id, principal.organization_id, Permission.READ_RECRUITING, grants) is allowed


def test_another_users_grant_is_denied() -> None:
    principal = Principal(uuid4(), uuid4(), frozenset({"recruiter"}), True)
    grant = JobGrant(uuid4(), uuid4(), principal.organization_id, "job_recruiter")
    assert not require_job_access(principal, grant.job_id, principal.organization_id, Permission.READ_RECRUITING, [grant])


def test_authorization_service_loads_only_principals_grant_and_audits_denial(identity_app) -> None:
    app, _, _ = identity_app
    with app.state.identity_store.sync_session() as db:
        organization = Organization(slug="policy", name="Policy", status="active")
        principal_user = User(organization=organization, email="p@x", normalized_email="p@x", display_name="P", password_hash=PasswordService().hash("x"), status=UserStatus.ACTIVE)
        principal_user.roles.append(UserRole(role="recruiter"))
        other = User(organization=organization, email="o@x", normalized_email="o@x", display_name="O", password_hash=PasswordService().hash("x"), status=UserStatus.ACTIVE)
        job = Job(organization_id=organization.id, title="J", owner_id=other.id, status="draft")
        db.add_all([principal_user, other])
        db.flush()
        job.organization_id = organization.id
        job.owner_id = other.id
        db.add(job)
        db.flush()
        db.add(JobCollaborator(organization_id=organization.id, job_id=job.id, user_id=other.id, access_role="job_recruiter"))
        db.commit()
        principal = Principal(principal_user.id, organization.id, frozenset({"recruiter"}), True)
        job_id = job.id
    service = AuthorizationService(app.state.identity_store)
    assert not service.require_job_access(principal, job_id, principal.organization_id, Permission.READ_RECRUITING, trace_id="trace-policy-denial")
    with app.state.identity_store.sync_session() as db:
        audit = db.query(AuditLog).filter_by(event_type="authorization.denied").one()
        assert audit.metadata_json == {"permission": "read_recruiting"}
