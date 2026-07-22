from datetime import datetime, timedelta, timezone
from uuid import uuid5

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from server.app.governance.models import RetentionPolicy
from server.app.identity.models import User, UserRole, UserStatus
from server.app.recruiting.models import Candidate
from server.app.talent.models import TalentPool, TalentPoolMembership


DEFERRED_POOL_SYSTEM_KEY = "ai_screening_deferred"
DEFERRED_POOL_DISPLAY_NAME = "AI 初筛暂缓"
DEFERRED_POOL_CREATE_MAX_ATTEMPTS = 5


class DeferredPoolUnavailableError(RuntimeError):
    pass


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _load_system_pool(db, organization_id, *, lock: bool):
    statement = select(TalentPool).where(
        TalentPool.organization_id == organization_id,
        TalentPool.system_key == DEFERRED_POOL_SYSTEM_KEY,
    )
    return db.scalar(statement.with_for_update() if lock else statement)


def _pool_owner_id(db, organization_id, fallback_id):
    return db.scalar(
        select(User.id)
        .join(UserRole, UserRole.user_id == User.id)
        .where(
            User.organization_id == organization_id,
            User.status == UserStatus.ACTIVE,
            UserRole.role == "recruiting_admin",
        )
        .order_by(User.id)
        .limit(1)
    ) or fallback_id


def _system_pool_names(organization_id):
    yield DEFERRED_POOL_DISPLAY_NAME
    for attempt in range(1, DEFERRED_POOL_CREATE_MAX_ATTEMPTS):
        suffix = uuid5(
            organization_id,
            f"{DEFERRED_POOL_SYSTEM_KEY}:{attempt}",
        ).hex[:16]
        yield f"{DEFERRED_POOL_DISPLAY_NAME} [{suffix}]"


def _refresh_pool_retention(pool, retention_days):
    if pool.retention_days != retention_days:
        pool.retention_days = retention_days
        pool.version += 1
        pool.updated_at = datetime.now(timezone.utc)
    return pool


def _ensure_system_pool(db, organization_id, fallback_owner_id):
    retention_days = db.scalar(
        select(RetentionPolicy.talent_pool_days).where(
            RetentionPolicy.organization_id == organization_id
        )
    )
    if retention_days is None:
        raise ValueError("retention_policy_unavailable")
    pool = _load_system_pool(db, organization_id, lock=True)
    if pool is not None:
        return _refresh_pool_retention(pool, retention_days)

    owner_id = _pool_owner_id(db, organization_id, fallback_owner_id)
    for name in _system_pool_names(organization_id):
        pool = TalentPool(
            organization_id=organization_id,
            name=name,
            purpose="保存 LLM 初筛分低于 60 的候选人",
            visibility="recruiting_team",
            owner_id=owner_id,
            system_key=DEFERRED_POOL_SYSTEM_KEY,
            suitable_roles=[],
            retention_days=retention_days,
        )
        try:
            with db.begin_nested():
                db.add(pool)
                db.flush()
            return pool
        except IntegrityError:
            existing = _load_system_pool(db, organization_id, lock=True)
            if existing is not None:
                return _refresh_pool_retention(existing, retention_days)
            name_taken = db.scalar(
                select(TalentPool.id).where(
                    TalentPool.organization_id == organization_id,
                    TalentPool.name == name,
                )
            )
            if name_taken is None:
                raise
    raise DeferredPoolUnavailableError("deferred_pool_name_exhausted")


def ensure_deferred_membership(
    db,
    *,
    application,
    candidate,
    job,
    run,
    score: int,
    transferable_capabilities=(),
):
    if (
        candidate.organization_id != application.organization_id
        or application.candidate_id != candidate.id
        or job.organization_id != application.organization_id
        or application.job_id != job.id
        or run.organization_id != application.organization_id
        or run.job_id != job.id
        or not 0 <= score < 60
    ):
        raise ValueError("deferred_membership_context_mismatch")
    locked_candidate = db.scalar(
        select(Candidate)
        .where(
            Candidate.organization_id == application.organization_id,
            Candidate.id == candidate.id,
            Candidate.deleted_at.is_(None),
        )
        .with_for_update()
    )
    if locked_candidate is None:
        raise ValueError("candidate_unavailable")

    pool = _ensure_system_pool(db, application.organization_id, run.created_by)
    membership = db.scalar(
        select(TalentPoolMembership)
        .where(
            TalentPoolMembership.organization_id == application.organization_id,
            TalentPoolMembership.pool_id == pool.id,
            TalentPoolMembership.candidate_id == locked_candidate.id,
        )
        .with_for_update()
    )
    tags = [job.title, f"LLM {score}分", *list(transferable_capabilities)[:5]]
    retention_until = datetime.now(timezone.utc) + timedelta(days=pool.retention_days)
    if membership is None:
        membership = TalentPoolMembership(
            organization_id=application.organization_id,
            pool_id=pool.id,
            candidate_id=locked_candidate.id,
            source_application_id=application.id,
            owner_id=run.created_by,
            suitable_roles=[],
            tags=tags,
            reason="LLM 初筛分低于 60",
            retention_until=retention_until,
            status="active",
        )
        db.add(membership)
        return membership

    membership.source_application_id = application.id
    membership.owner_id = run.created_by
    membership.tags = tags
    membership.reason = "LLM 初筛分低于 60"
    if _aware(membership.retention_until) < retention_until:
        membership.retention_until = retention_until
    membership.updated_at = datetime.now(timezone.utc)
    membership.version += 1
    return membership
