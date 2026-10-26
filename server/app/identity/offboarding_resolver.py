"""Resolve mutable work ownership without changing the original requester."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import DBAPIError

from server.app.identity.models import User, UserRecruitingDepartmentScope, UserRole, UserStatus, Job
from server.app.identity.policy import Principal
from server.app.queue.service import RetryableJobError
from server.app.recruiting.authorization import RecruitingAction, RecruitingAuthorizationService


class OperationalOwnerUnavailable(RetryableJobError):
    def __init__(self):
        super().__init__("operational_owner_unavailable")


def _lock_user(db, organization_id, user_id):
    try:
        return db.scalar(
            select(User)
            .where(User.organization_id == organization_id, User.id == user_id)
            .with_for_update(nowait=True)
            .execution_options(populate_existing=True)
        )
    except DBAPIError as error:
        code = getattr(error.orig, "sqlstate", None) or getattr(error.orig, "pgcode", None)
        if code in {"55P03", "40P01", "40001"}:
            # Callers can already hold candidate locks. Never wait for an
            # offboarding transaction holding User locks and waiting for them.
            raise OperationalOwnerUnavailable() from None
        raise


def resolve_operational_user_id(db, organization_id, user_id, *, job_id=None) -> UUID:
    """Hold each relevant User lock until the caller commits or rolls back.

    Offboarding must hold the same source/destination User locks before changing
    durable intent or responsibilities. NOWAIT makes candidate-first producer
    callers retry instead of reversing that lock order and deadlocking.
    """
    current_id = user_id
    visited = set()
    while current_id not in visited:
        visited.add(current_id)
        user = _lock_user(db, organization_id, current_id)
        if user is None:
            raise OperationalOwnerUnavailable()
        if user.status == UserStatus.ACTIVE:
            if current_id == user_id:
                return user.id
            roles = frozenset(db.scalars(select(UserRole.role).where(UserRole.user_id == user.id)))
            if not roles & {"recruiter", "recruiting_admin"}:
                raise OperationalOwnerUnavailable()
            if job_id is not None:
                departments = frozenset(db.scalars(
                    select(UserRecruitingDepartmentScope.department_id).where(
                        UserRecruitingDepartmentScope.organization_id == organization_id,
                        UserRecruitingDepartmentScope.user_id == user.id,
                    )
                ))
                principal = Principal(user.id, organization_id, roles, True, user.recruiting_scope_type, departments)
                allowed = db.scalar(select(Job.id).where(
                    Job.organization_id == organization_id,
                    Job.id == job_id,
                    RecruitingAuthorizationService().job_predicate(principal, RecruitingAction.MANAGE_CANDIDATE),
                ))
                if allowed is None:
                    raise OperationalOwnerUnavailable()
            return user.id
        if user.status != UserStatus.DISABLED:
            raise OperationalOwnerUnavailable()
        # Lazy import permits module loading before the offboarding model is
        # registered; inactive users still require a durable handover record.
        from server.app.identity.offboarding_models import MemberOffboarding

        intent = db.scalar(select(MemberOffboarding).where(
            MemberOffboarding.organization_id == organization_id,
            MemberOffboarding.user_id == current_id,
        ))
        if intent is None or intent.successor_user_id is None:
            raise OperationalOwnerUnavailable()
        current_id = intent.successor_user_id
    raise OperationalOwnerUnavailable()
