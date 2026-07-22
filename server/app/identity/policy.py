from dataclasses import dataclass
from enum import Enum
from uuid import UUID

from sqlalchemy import select

from server.app.identity.models import AuditLog, JobCollaborator


class Permission(str, Enum):
    MANAGE_USERS = "manage_users"
    MANAGE_SYSTEM = "manage_system"
    MANAGE_AUDIT = "manage_audit"
    READ_RECRUITING = "read_recruiting"
    COMMENT = "comment"
    RECOMMEND_DECISION = "recommend_decision"
    BULK_EXPORT = "bulk_export"
    SEARCH_JOBS = "search_jobs"


GLOBAL_PERMISSIONS = {
    "system_admin": {Permission.MANAGE_USERS, Permission.MANAGE_SYSTEM, Permission.MANAGE_AUDIT},
    "recruiting_admin": {Permission.READ_RECRUITING, Permission.COMMENT, Permission.RECOMMEND_DECISION, Permission.BULK_EXPORT, Permission.SEARCH_JOBS},
}
JOB_PERMISSIONS = {
    "job_owner": {Permission.READ_RECRUITING, Permission.COMMENT, Permission.RECOMMEND_DECISION, Permission.BULK_EXPORT, Permission.SEARCH_JOBS},
    "job_recruiter": {Permission.READ_RECRUITING, Permission.COMMENT, Permission.BULK_EXPORT, Permission.SEARCH_JOBS},
    "job_manager": {Permission.READ_RECRUITING, Permission.COMMENT, Permission.RECOMMEND_DECISION},
}


@dataclass(frozen=True)
class Principal:
    user_id: UUID
    organization_id: UUID
    roles: frozenset[str]
    active: bool


@dataclass(frozen=True)
class JobGrant:
    user_id: UUID
    job_id: UUID
    organization_id: UUID
    access_role: str


def require_permission(principal: Principal, permission: Permission) -> bool:
    return principal.active and any(permission in GLOBAL_PERMISSIONS.get(role, set()) for role in principal.roles)


def require_job_access(principal: Principal, job_id: UUID, organization_id: UUID, permission: Permission, grants: list[JobGrant]) -> bool:
    if not principal.active or principal.organization_id != organization_id:
        return False
    if "recruiting_admin" in principal.roles and require_permission(principal, permission):
        return True
    return any(
        grant.user_id == principal.user_id
        and grant.job_id == job_id
        and grant.organization_id == organization_id
        and (
            ("recruiter" in principal.roles and grant.access_role in {"job_owner", "job_recruiter"})
            or ("hiring_manager" in principal.roles and grant.access_role == "job_manager")
        )
        and permission in JOB_PERMISSIONS.get(grant.access_role, set())
        for grant in grants
    )


class AuthorizationService:
    def __init__(self, store) -> None:
        self.store = store

    def require_job_access(self, principal: Principal, job_id: UUID, organization_id: UUID, permission: Permission, *, trace_id: str) -> bool:
        with self.store.sync_session() as db:
            rows = db.scalars(
                select(JobCollaborator).where(
                    JobCollaborator.user_id == principal.user_id,
                    JobCollaborator.job_id == job_id,
                    JobCollaborator.organization_id == organization_id,
                )
            ).all()
            grants = [JobGrant(row.user_id, row.job_id, row.organization_id, row.access_role) for row in rows]
            allowed = require_job_access(principal, job_id, organization_id, permission, grants)
            if not allowed:
                db.add(AuditLog(organization_id=principal.organization_id, actor_user_id=principal.user_id, event_type="authorization.denied", outcome="denied", trace_id=trace_id, metadata_json={"permission": permission.value}))
                db.commit()
            return allowed
