from __future__ import annotations

from uuid import UUID

from sqlalchemy import and_, exists, or_, select

from server.app.identity.models import (
    JobCollaborator,
    User,
    UserRecruitingDepartmentScope,
    UserRole,
    UserStatus,
)


def review_manager_user_ids(db, job) -> tuple[UUID, ...]:
    """Return the explicit manager first, followed by scoped hiring managers."""
    explicit_ids = set(
        db.scalars(
            select(JobCollaborator.user_id).where(
                JobCollaborator.organization_id == job.organization_id,
                JobCollaborator.job_id == job.id,
                JobCollaborator.access_role == "job_manager",
            )
        )
    )
    if job.hiring_owner_id is not None:
        explicit_ids.add(job.hiring_owner_id)

    role_exists = exists().where(
        UserRole.user_id == User.id,
        UserRole.role.in_(("hiring_manager", "recruiting_admin")),
    )
    explicit_users = []
    if explicit_ids:
        explicit_users = list(
            db.scalars(
                select(User).where(
                    User.organization_id == job.organization_id,
                    User.id.in_(explicit_ids),
                    User.status == UserStatus.ACTIVE,
                    role_exists,
                )
            )
        )

    scope_conditions = [User.recruiting_scope_type == "organization"]
    if job.department_id is not None:
        scope_conditions.append(
            and_(
                User.recruiting_scope_type == "departments",
                exists().where(
                    UserRecruitingDepartmentScope.organization_id
                    == User.organization_id,
                    UserRecruitingDepartmentScope.user_id == User.id,
                    UserRecruitingDepartmentScope.department_id
                    == job.department_id,
                ),
            )
        )
    scoped_users = list(
        db.scalars(
            select(User)
            .where(
                User.organization_id == job.organization_id,
                User.status == UserStatus.ACTIVE,
                exists().where(
                    UserRole.user_id == User.id,
                    UserRole.role == "hiring_manager",
                ),
                or_(*scope_conditions),
            )
            .order_by(User.created_at.asc(), User.id.asc())
        )
    )

    ordered_ids: list[UUID] = []
    if job.hiring_owner_id is not None and any(
        user.id == job.hiring_owner_id for user in explicit_users
    ):
        ordered_ids.append(job.hiring_owner_id)
    ordered_ids.extend(
        user.id
        for user in sorted(explicit_users, key=lambda user: (user.created_at, user.id))
        if user.id not in ordered_ids
    )
    ordered_ids.extend(
        user.id for user in scoped_users if user.id not in ordered_ids
    )
    return tuple(ordered_ids)


def explicit_review_manager_user_ids(db, job) -> tuple[UUID, ...]:
    manager_ids = tuple(
        db.scalars(
            select(JobCollaborator.user_id)
            .where(
                JobCollaborator.organization_id == job.organization_id,
                JobCollaborator.job_id == job.id,
                JobCollaborator.access_role == "job_manager",
            )
            .order_by(JobCollaborator.created_at.asc(), JobCollaborator.id.asc())
        )
    )
    if job.hiring_owner_id is None:
        return manager_ids
    return (job.hiring_owner_id, *(
        manager_id for manager_id in manager_ids
        if manager_id != job.hiring_owner_id
    ))


def review_notification_user_ids(db, job) -> tuple[UUID, ...]:
    eligible_ids = review_manager_user_ids(db, job)
    if job.hiring_owner_id in eligible_ids:
        return (job.hiring_owner_id,)
    return (job.owner_id,)
