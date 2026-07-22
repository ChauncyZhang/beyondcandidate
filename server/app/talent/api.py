import hashlib
import json
import re
from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Header, Query, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy import String, and_, cast, delete, exists, func, or_, select
from sqlalchemy.exc import IntegrityError

from server.app.governance.audit import AuditValidationError, _validate_metadata
from server.app.governance.retention import recalculate_candidate_retention
from server.app.identity.api import problem, session_token
from server.app.identity.models import AuditLog, Job, User, UserRole, UserStatus
from server.app.identity.policy import Principal
from server.app.identity.service import InvalidSession
from server.app.llm.models import LlmInvocation, LlmScreeningEvaluation
from server.app.recruiting.authorization import RecruitingAction, RecruitingAuthorizationService
from server.app.recruiting.models import Application, ApplicationStageEvent, Candidate, CandidateEvent, Resume
from server.app.recruiting.tasks import ensure_review_task
from server.app.recruiting.service import (
    ActiveApplicationExists,
    CandidateUnavailable,
    IdempotencyConflict,
    lock_active_candidate,
    persisted_idempotent,
)
from server.app.screening.models import ScreeningItem, ScreeningResult
from server.app.talent.models import TalentPool, TalentPoolGrant, TalentPoolMembership
from server.app.talent.service import DEFERRED_POOL_SYSTEM_KEY
from server.app.talent.schemas import (
    DataCollection,
    DataResource,
    MembershipCreate,
    MembershipPatch,
    MembershipRemoval,
    PoolCreate,
    PoolPatch,
    ReactivationInput,
    ReviewReferralInput,
)


router = APIRouter(prefix="/api/v1")
AUTH = RecruitingAuthorizationService()
ETAG = re.compile(r'^"(0|[1-9][0-9]*)"$')
TERMINAL_APPLICATION_STAGES = frozenset({"hired", "rejected", "withdrawn"})
TALENT_ROLES = frozenset({"recruiting_admin", "recruiter"})


class ReviewReferralUnavailable(Exception):
    pass


class ReviewReferralVersionConflict(Exception):
    pass


def _principal(request: Request) -> Principal | JSONResponse:
    token = session_token(request)
    if not token:
        return problem(request, 401, "authentication_required", "Authentication is required.")
    try:
        return request.app.state.identity_service.principal(token)
    except InvalidSession:
        return problem(request, 401, "authentication_required", "Authentication is required.")


def _denied(request: Request) -> JSONResponse:
    return problem(request, 404, "resource_not_found", "The requested resource is unavailable.")


def _idempotency(request: Request, value: str | None) -> str | JSONResponse:
    if not value or len(value) > 255:
        return problem(request, 428, "idempotency_key_required", "Idempotency-Key is required.")
    return value


def _expected_version(request: Request, value: str | None) -> int | JSONResponse:
    if value is None:
        return problem(request, 428, "precondition_required", "A quoted If-Match version is required.")
    matched = ETAG.fullmatch(value)
    if not matched:
        return problem(request, 422, "validation_failed", "If-Match must be a quoted integer.")
    return int(matched.group(1))


def _is_active_application_conflict(error: IntegrityError) -> bool:
    return getattr(getattr(error.orig, "diag", None), "constraint_name", None) == "uq_applications_active"


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _pool_scope(principal: Principal, *, manage: bool = False):
    if not principal.active:
        return False
    if "recruiting_admin" in principal.roles:
        return True
    grant_roles = ("manager",) if manage else ("viewer", "manager")
    granted = exists().where(
        TalentPoolGrant.organization_id == TalentPool.organization_id,
        TalentPoolGrant.pool_id == TalentPool.id,
        TalentPoolGrant.user_id == principal.user_id,
        TalentPoolGrant.access_role.in_(grant_roles),
    )
    branches = [TalentPool.owner_id == principal.user_id, granted]
    if not manage and "recruiter" in principal.roles:
        branches.append(TalentPool.visibility == "recruiting_team")
    return or_(*branches)


def _load_pool(db, principal: Principal, pool_id: UUID, *, manage: bool = False, for_update: bool = False):
    statement = select(TalentPool).where(
        TalentPool.organization_id == principal.organization_id,
        TalentPool.id == pool_id,
        _pool_scope(principal, manage=manage),
    )
    if for_update:
        statement = statement.with_for_update()
    return db.scalar(statement)


def _load_membership(db, principal: Principal, membership_id: UUID, *, manage: bool = False, for_update: bool = False):
    statement = (
        select(TalentPoolMembership)
        .join(
            TalentPool,
            and_(
                TalentPool.organization_id == TalentPoolMembership.organization_id,
                TalentPool.id == TalentPoolMembership.pool_id,
            ),
        )
        .join(
            Candidate,
            and_(
                Candidate.organization_id == TalentPoolMembership.organization_id,
                Candidate.id == TalentPoolMembership.candidate_id,
            ),
        )
        .where(
            TalentPoolMembership.organization_id == principal.organization_id,
            TalentPoolMembership.id == membership_id,
            Candidate.deleted_at.is_(None),
            _pool_scope(principal, manage=manage),
        )
    )
    if for_update:
        statement = statement.with_for_update()
    return db.scalar(statement)


def _load_review_referral_membership(db, principal: Principal, membership_id: UUID, *, for_update: bool = False):
    referral_scope = or_(
        _pool_scope(principal, manage=True),
        and_(
            TalentPool.system_key == DEFERRED_POOL_SYSTEM_KEY,
            _pool_scope(principal),
        ),
    )
    statement = (
        select(TalentPoolMembership)
        .join(
            TalentPool,
            and_(
                TalentPool.organization_id == TalentPoolMembership.organization_id,
                TalentPool.id == TalentPoolMembership.pool_id,
            ),
        )
        .join(
            Candidate,
            and_(
                Candidate.organization_id == TalentPoolMembership.organization_id,
                Candidate.id == TalentPoolMembership.candidate_id,
            ),
        )
        .where(
            TalentPoolMembership.organization_id == principal.organization_id,
            TalentPoolMembership.id == membership_id,
            Candidate.deleted_at.is_(None),
            referral_scope,
        )
    )
    if for_update:
        statement = statement.with_for_update()
    return db.scalar(statement)


def _active_user(db, organization_id: UUID, user_id: UUID):
    return db.scalar(
        select(User).where(
            User.organization_id == organization_id,
            User.id == user_id,
            User.status == UserStatus.ACTIVE,
        )
    )


def _replace_grants(db, principal: Principal, pool: TalentPool, grants) -> bool:
    user_ids = [grant.user_id for grant in grants]
    if user_ids:
        count = db.scalar(
            select(func.count(User.id)).where(
                User.organization_id == principal.organization_id,
                User.id.in_(user_ids),
                User.status == UserStatus.ACTIVE,
            )
        )
        if count != len(user_ids):
            return False
    db.execute(
        delete(TalentPoolGrant).where(
            TalentPoolGrant.organization_id == principal.organization_id,
            TalentPoolGrant.pool_id == pool.id,
        )
    )
    db.add_all(
        TalentPoolGrant(
            organization_id=principal.organization_id,
            pool_id=pool.id,
            user_id=grant.user_id,
            access_role=grant.access_role,
        )
        for grant in grants
    )
    return True


def _pool_data(db, pool: TalentPool, *, include_grants: bool = False) -> dict:
    owner = db.get(User, pool.owner_id)
    body = {
        "id": str(pool.id),
        "name": pool.name,
        "system_key": pool.system_key,
        "purpose": pool.purpose,
        "visibility": pool.visibility,
        "owner": {"id": str(pool.owner_id), "display_name": owner.display_name if owner else "Unavailable"},
        "suitable_roles": list(pool.suitable_roles or []),
        "retention_days": pool.retention_days,
        "member_count": db.scalar(
            select(func.count(TalentPoolMembership.id)).where(
                TalentPoolMembership.organization_id == pool.organization_id,
                TalentPoolMembership.pool_id == pool.id,
            )
        ),
        "version": pool.version,
        "created_at": _aware(pool.created_at).isoformat(),
        "updated_at": _aware(pool.updated_at).isoformat(),
    }
    if include_grants:
        rows = db.execute(
            select(TalentPoolGrant, User)
            .join(User, and_(User.organization_id == TalentPoolGrant.organization_id, User.id == TalentPoolGrant.user_id))
            .where(
                TalentPoolGrant.organization_id == pool.organization_id,
                TalentPoolGrant.pool_id == pool.id,
            )
            .order_by(User.display_name, User.id)
        ).all()
        body["grants"] = [
            {"user_id": str(grant.user_id), "display_name": user.display_name, "access_role": grant.access_role}
            for grant, user in rows
        ]
    return body


def _authorized_application(db, principal: Principal, application_id: UUID, candidate_id: UUID):
    return db.scalar(
        select(Application)
        .join(
            Job,
            and_(
                Job.organization_id == Application.organization_id,
                Job.id == Application.job_id,
            ),
        )
        .where(
            Application.organization_id == principal.organization_id,
            Application.id == application_id,
            Application.candidate_id == candidate_id,
            AUTH.job_predicate(principal, RecruitingAction.READ, Job),
        )
    )


def _authorized_resume(db, principal: Principal, resume_id: UUID, candidate_id: UUID):
    visible_application = exists().where(
        Application.organization_id == Resume.organization_id,
        Application.resume_id == Resume.id,
        exists().where(
            Job.organization_id == Application.organization_id,
            Job.id == Application.job_id,
            AUTH.job_predicate(principal, RecruitingAction.DOWNLOAD, Job),
        ),
    )
    return db.scalar(
        select(Resume).where(
            Resume.organization_id == principal.organization_id,
            Resume.id == resume_id,
            Resume.candidate_id == candidate_id,
            visible_application,
        )
    )


def _empty_deferred_screening() -> dict:
    return {"final_score": None, "deferred_at": None, "main_gaps": []}


def _deferred_screening_projections(db, organization_id: UUID, application_ids: set[UUID]) -> dict[UUID, dict]:
    if not application_ids:
        return {}
    rows = db.execute(
        select(
            ScreeningItem.application_id,
            ScreeningItem.id.label("item_id"),
            LlmScreeningEvaluation.id.label("evaluation_id"),
            LlmInvocation.id.label("invocation_id"),
            LlmScreeningEvaluation.score,
            LlmScreeningEvaluation.recommendation,
            LlmScreeningEvaluation.gaps,
            LlmScreeningEvaluation.created_at.label("evaluation_created_at"),
        )
        .select_from(LlmScreeningEvaluation)
        .join(
            ScreeningResult,
            and_(
                ScreeningResult.organization_id == LlmScreeningEvaluation.organization_id,
                ScreeningResult.id == LlmScreeningEvaluation.screening_result_id,
            ),
        )
        .join(
            ScreeningItem,
            and_(
                ScreeningItem.organization_id == ScreeningResult.organization_id,
                ScreeningItem.id == ScreeningResult.item_id,
                ScreeningItem.application_id == ScreeningResult.application_id,
            ),
        )
        .join(
            LlmInvocation,
            and_(
                LlmInvocation.organization_id == LlmScreeningEvaluation.organization_id,
                LlmInvocation.id == LlmScreeningEvaluation.invocation_id,
                LlmInvocation.screening_result_id == LlmScreeningEvaluation.screening_result_id,
                LlmInvocation.prompt_version_id == LlmScreeningEvaluation.prompt_version_id,
            ),
        )
        .where(
            LlmScreeningEvaluation.organization_id == organization_id,
            ScreeningItem.application_id.in_(application_ids),
            ScreeningItem.llm_status == "succeeded",
            LlmInvocation.status == "succeeded",
        )
        .order_by(
            ScreeningItem.application_id,
            LlmScreeningEvaluation.created_at.desc(),
            LlmScreeningEvaluation.id.desc(),
        )
    ).all()
    evaluations = {
        (row.application_id, row.evaluation_id, row.invocation_id): row
        for row in rows
    }

    audits = db.scalars(
        select(AuditLog)
        .where(
            AuditLog.organization_id == organization_id,
            AuditLog.category == "recruiting",
            AuditLog.event_type == "screening.terminal_routed",
            AuditLog.outcome == "success",
            AuditLog.resource_type == "application",
            AuditLog.resource_id.in_(application_ids),
        )
        .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
    ).all()
    projections = {}
    for audit in audits:
        application_id = audit.resource_id
        if application_id in projections:
            continue
        try:
            metadata = _validate_metadata("screening.terminal_routed", audit.metadata_json)
            item_id = UUID(metadata["item_id"])
            evaluation_id = UUID(metadata["evaluation_id"])
            invocation_id = UUID(metadata["invocation_id"])
        except (AuditValidationError, KeyError, TypeError, ValueError):
            continue
        evaluation = evaluations.get((application_id, evaluation_id, invocation_id))
        if evaluation is None or not 0 <= evaluation.score < 60 or evaluation.recommendation != "暂缓":
            continue
        if (
            metadata.get("application_id") != str(application_id)
            or metadata.get("from_stage") != "new"
            or metadata.get("to_stage") != "deferred"
            or metadata.get("ai_status") != "succeeded"
            or metadata.get("score") != evaluation.score
            or metadata.get("recommendation") != evaluation.recommendation
            or metadata.get("safe_error_code") is not None
            or item_id != evaluation.item_id
            or _aware(evaluation.evaluation_created_at) > _aware(audit.created_at)
        ):
            continue
        projections[application_id] = {
            "final_score": evaluation.score,
            "deferred_at": _aware(audit.created_at).isoformat(),
            "main_gaps": list(evaluation.gaps or []),
        }
    return projections


def _membership_data_batch(
    db,
    principal: Principal,
    memberships: list[TalentPoolMembership],
    *,
    pools: dict[UUID, TalentPool] | None = None,
) -> list[dict]:
    if not memberships:
        return []
    organization_id = principal.organization_id
    candidate_ids = {membership.candidate_id for membership in memberships}
    owner_ids = {membership.owner_id for membership in memberships}
    source_ids = {membership.source_application_id for membership in memberships if membership.source_application_id}
    pool_ids = {membership.pool_id for membership in memberships}

    candidates = {
        candidate.id: candidate
        for candidate in db.scalars(
            select(Candidate).where(
                Candidate.organization_id == organization_id,
                Candidate.id.in_(candidate_ids),
                Candidate.deleted_at.is_(None),
            )
        ).all()
    }
    owners = {
        owner.id: owner
        for owner in db.scalars(
            select(User).where(User.organization_id == organization_id, User.id.in_(owner_ids))
        ).all()
    }
    loaded_pools = dict(pools or {})
    missing_pool_ids = pool_ids - loaded_pools.keys()
    if missing_pool_ids:
        loaded_pools.update(
            {
                pool.id: pool
                for pool in db.scalars(
                    select(TalentPool).where(
                        TalentPool.organization_id == organization_id,
                        TalentPool.id.in_(missing_pool_ids),
                    )
                ).all()
            }
        )

    visible_sources = {}
    if source_ids:
        source_rows = db.execute(
            select(Application, Job)
            .join(
                Job,
                and_(Job.organization_id == Application.organization_id, Job.id == Application.job_id),
            )
            .where(
                Application.organization_id == organization_id,
                Application.id.in_(source_ids),
                AUTH.job_predicate(principal, RecruitingAction.READ, Job),
            )
        ).all()
        visible_sources = {source.id: (source, job) for source, job in source_rows}

    deferred_source_ids = {
        membership.source_application_id
        for membership in memberships
        if membership.source_application_id in visible_sources
        and loaded_pools.get(membership.pool_id) is not None
        and loaded_pools[membership.pool_id].system_key == DEFERRED_POOL_SYSTEM_KEY
        and visible_sources[membership.source_application_id][0].candidate_id == membership.candidate_id
    }
    deferred_projections = _deferred_screening_projections(db, organization_id, deferred_source_ids)

    data = []
    for membership in memberships:
        candidate = candidates.get(membership.candidate_id)
        owner = owners.get(membership.owner_id)
        visible_source = visible_sources.get(membership.source_application_id)
        if visible_source is not None and visible_source[0].candidate_id != membership.candidate_id:
            visible_source = None
        if membership.source_application_id is None:
            source_data = None
        elif visible_source is None:
            source_data = {"id": str(membership.source_application_id), "redacted": True}
        else:
            source, source_job = visible_source
            source_data = {
                "id": str(source.id),
                "job_id": str(source.job_id),
                "job_title": source_job.title,
                "stage": source.stage,
                "human_conclusion": source.human_conclusion,
            }
        pool = loaded_pools.get(membership.pool_id)
        deferred_screening = None
        if pool is not None and pool.system_key == DEFERRED_POOL_SYSTEM_KEY and visible_source is not None:
            deferred_screening = deferred_projections.get(
                membership.source_application_id,
                _empty_deferred_screening(),
            )
        data.append({
            "id": str(membership.id),
            "pool_id": str(membership.pool_id),
            "candidate": {
                "id": str(membership.candidate_id),
                "display_name": candidate.display_name if candidate else "Unavailable",
                "current_title": candidate.current_title if candidate else None,
                "location": candidate.location if candidate else None,
            },
            "source_application": source_data,
            "deferred_screening": deferred_screening,
            "owner": {"id": str(membership.owner_id), "display_name": owner.display_name if owner else "Unavailable"},
            "suitable_roles": list(membership.suitable_roles or []),
            "tags": list(membership.tags or []),
            "reason": membership.reason,
            "next_contact_at": _aware(membership.next_contact_at).isoformat() if membership.next_contact_at else None,
            "retention_until": _aware(membership.retention_until).isoformat(),
            "status": membership.status,
            "version": membership.version,
            "created_at": _aware(membership.created_at).isoformat(),
            "updated_at": _aware(membership.updated_at).isoformat(),
        })
    return data


def _membership_data(db, principal: Principal, membership: TalentPoolMembership) -> dict:
    return _membership_data_batch(db, principal, [membership])[0]


def _application_data(application: Application) -> dict:
    return {
        "id": str(application.id),
        "candidate_id": str(application.candidate_id),
        "job_id": str(application.job_id),
        "resume_id": str(application.resume_id),
        "owner_id": str(application.owner_id),
        "stage": application.stage,
        "source": application.source,
        "source_application_id": str(application.source_application_id) if application.source_application_id else None,
        "version": application.version,
    }


@router.get("/talent-pools", response_model=DataCollection)
def list_talent_pools(
    request: Request,
    q: str | None = None,
    visibility: str | None = None,
    cursor: str | None = None,
    limit: int = Query(50, ge=1, le=100),
):
    principal = _principal(request)
    if isinstance(principal, JSONResponse):
        return principal
    if not principal.active or not (principal.roles & TALENT_ROLES or "hiring_manager" in principal.roles):
        return _denied(request)
    scope_hash = hashlib.sha256(json.dumps({"q": q or None, "visibility": visibility}, sort_keys=True).encode()).hexdigest()
    cursor_sort = f"talent-pools:updated_at:{scope_hash}"
    with request.app.state.identity_store.sync_session() as db:
        statement = select(TalentPool).where(
            TalentPool.organization_id == principal.organization_id,
            _pool_scope(principal),
        )
        if q:
            pattern = f"%{q.strip()}%"
            statement = statement.where(or_(TalentPool.name.ilike(pattern), TalentPool.purpose.ilike(pattern)))
        if visibility:
            if visibility not in {"private", "recruiting_team", "granted"}:
                return problem(request, 422, "validation_failed", "The request could not be completed.")
            statement = statement.where(TalentPool.visibility == visibility)
        if cursor:
            try:
                decoded = request.app.state.recruiting_cursor.decode(cursor, str(principal.organization_id), cursor_sort)
                updated_at = datetime.fromisoformat(decoded["value"])
                statement = statement.where(
                    or_(
                        TalentPool.updated_at < updated_at,
                        and_(TalentPool.updated_at == updated_at, TalentPool.id < UUID(decoded["id"])),
                    )
                )
            except Exception:
                return problem(request, 422, "validation_failed", "The request could not be completed.")
        rows = db.scalars(statement.order_by(TalentPool.updated_at.desc(), TalentPool.id.desc()).limit(limit + 1)).all()
        next_cursor = None
        if len(rows) > limit:
            last = rows[limit - 1]
            next_cursor = request.app.state.recruiting_cursor.encode(
                str(principal.organization_id), cursor_sort, _aware(last.updated_at).isoformat(), str(last.id)
            )
            rows = rows[:limit]
        response = JSONResponse({"data": [_pool_data(db, row) for row in rows], "meta": {"limit": limit, "next_cursor": next_cursor}})
        response.headers["Cache-Control"] = "no-store"
        return response


@router.post("/talent-pools", response_model=DataResource, status_code=201)
def create_talent_pool(payload: PoolCreate, request: Request, idempotency_key: str | None = Header(None, alias="Idempotency-Key")):
    principal = _principal(request)
    if isinstance(principal, JSONResponse):
        return principal
    key = _idempotency(request, idempotency_key)
    if isinstance(key, JSONResponse):
        return key
    if not principal.active or not principal.roles & TALENT_ROLES:
        return _denied(request)
    if "recruiting_admin" not in principal.roles and payload.owner_id != principal.user_id:
        return _denied(request)
    with request.app.state.identity_store.sync_session() as db:
        if _active_user(db, principal.organization_id, payload.owner_id) is None:
            return _denied(request)

        def action():
            pool = TalentPool(
                organization_id=principal.organization_id,
                name=payload.name,
                purpose=payload.purpose,
                visibility=payload.visibility,
                owner_id=payload.owner_id,
                suitable_roles=payload.suitable_roles,
                retention_days=payload.retention_days,
            )
            db.add(pool)
            db.flush()
            if not _replace_grants(db, principal, pool, payload.grants):
                raise LookupError("invalid grant")
            db.add(AuditLog(
                organization_id=principal.organization_id,
                actor_user_id=principal.user_id,
                event_type="talent_pool.created",
                outcome="success",
                trace_id=request.state.trace_id,
                metadata_json={"pool_id": str(pool.id)},
            ))
            db.flush()
            return 201, {"data": _pool_data(db, pool, include_grants=True)}

        try:
            status, body = persisted_idempotent(
                db, principal.organization_id, principal.user_id, "talent_pool.create", key, payload.model_dump(), action
            )
            db.commit()
        except LookupError:
            db.rollback()
            return _denied(request)
        except IntegrityError:
            db.rollback()
            return problem(request, 409, "talent_pool_name_exists", "A talent pool with this name already exists.")
        except IdempotencyConflict:
            db.rollback()
            return problem(request, 409, "idempotency_conflict", "The idempotency key was already used.")
        response = JSONResponse(body, status_code=status)
        response.headers["ETag"] = f'"{body["data"]["version"]}"'
        response.headers["Cache-Control"] = "no-store"
        return response


@router.get("/talent-pools/{pool_id}", response_model=DataResource)
def get_talent_pool(pool_id: UUID, request: Request):
    principal = _principal(request)
    if isinstance(principal, JSONResponse):
        return principal
    with request.app.state.identity_store.sync_session() as db:
        pool = _load_pool(db, principal, pool_id)
        if pool is None:
            return _denied(request)
        response = JSONResponse({"data": _pool_data(db, pool, include_grants=True)})
        response.headers["ETag"] = f'"{pool.version}"'
        response.headers["Cache-Control"] = "no-store"
        return response


@router.patch("/talent-pools/{pool_id}", response_model=DataResource)
def patch_talent_pool(pool_id: UUID, payload: PoolPatch, request: Request, if_match: str | None = Header(None)):
    principal = _principal(request)
    if isinstance(principal, JSONResponse):
        return principal
    expected = _expected_version(request, if_match)
    if isinstance(expected, JSONResponse):
        return expected
    with request.app.state.identity_store.sync_session() as db:
        pool = _load_pool(db, principal, pool_id, manage=True, for_update=True)
        if pool is None:
            return _denied(request)
        if pool.version != expected:
            return problem(request, 409, "resource_version_conflict", "The resource changed. Reload and try again.")
        changes = payload.model_dump(exclude_unset=True, exclude={"grants"})
        if "owner_id" in changes and _active_user(db, principal.organization_id, changes["owner_id"]) is None:
            return _denied(request)
        for field, value in changes.items():
            setattr(pool, field, value)
        if payload.grants is not None and not _replace_grants(db, principal, pool, payload.grants):
            return _denied(request)
        pool.version += 1
        pool.updated_at = datetime.now(timezone.utc)
        db.add(AuditLog(
            organization_id=principal.organization_id,
            actor_user_id=principal.user_id,
            event_type="talent_pool.updated",
            outcome="success",
            trace_id=request.state.trace_id,
            metadata_json={"pool_id": str(pool.id), "changed_fields": sorted(payload.model_fields_set)},
        ))
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            return problem(request, 409, "talent_pool_name_exists", "A talent pool with this name already exists.")
        response = JSONResponse({"data": _pool_data(db, pool, include_grants=True)})
        response.headers["ETag"] = f'"{pool.version}"'
        response.headers["Cache-Control"] = "no-store"
        return response


@router.get("/talent-pools/{pool_id}/memberships", response_model=DataCollection)
def list_talent_pool_memberships(
    pool_id: UUID,
    request: Request,
    q: str | None = None,
    skills: str | None = None,
    city: str | None = None,
    next_contact_before: datetime | None = None,
    cursor: str | None = None,
    limit: int = Query(50, ge=1, le=100),
):
    principal = _principal(request)
    if isinstance(principal, JSONResponse):
        return principal
    with request.app.state.identity_store.sync_session() as db:
        pool = _load_pool(db, principal, pool_id)
        if pool is None:
            return _denied(request)
        scope_hash = hashlib.sha256(json.dumps({"q": q, "skills": skills, "city": city, "next_contact_before": str(next_contact_before)}, sort_keys=True).encode()).hexdigest()
        cursor_sort = f"talent-memberships:updated_at:{pool_id}:{scope_hash}"
        statement = (
            select(TalentPoolMembership)
            .join(Candidate, and_(Candidate.organization_id == TalentPoolMembership.organization_id, Candidate.id == TalentPoolMembership.candidate_id))
            .where(
                TalentPoolMembership.organization_id == principal.organization_id,
                TalentPoolMembership.pool_id == pool_id,
                Candidate.deleted_at.is_(None),
            )
        )
        if q:
            pattern = f"%{q.strip()}%"
            statement = statement.where(or_(Candidate.display_name.ilike(pattern), Candidate.current_title.ilike(pattern)))
        if city:
            statement = statement.where(Candidate.location == city)
        if skills:
            for skill in [value.strip() for value in skills.split(",") if value.strip()]:
                statement = statement.where(cast(TalentPoolMembership.tags, String).ilike(f"%{skill}%"))
        if next_contact_before:
            statement = statement.where(TalentPoolMembership.next_contact_at <= next_contact_before)
        if cursor:
            try:
                decoded = request.app.state.recruiting_cursor.decode(cursor, str(principal.organization_id), cursor_sort)
                updated_at = datetime.fromisoformat(decoded["value"])
                statement = statement.where(or_(TalentPoolMembership.updated_at < updated_at, and_(TalentPoolMembership.updated_at == updated_at, TalentPoolMembership.id < UUID(decoded["id"]))))
            except Exception:
                return problem(request, 422, "validation_failed", "The request could not be completed.")
        rows = db.scalars(statement.order_by(TalentPoolMembership.updated_at.desc(), TalentPoolMembership.id.desc()).limit(limit + 1)).all()
        next_cursor = None
        if len(rows) > limit:
            last = rows[limit - 1]
            next_cursor = request.app.state.recruiting_cursor.encode(str(principal.organization_id), cursor_sort, _aware(last.updated_at).isoformat(), str(last.id))
            rows = rows[:limit]
        response = JSONResponse({
            "data": _membership_data_batch(db, principal, rows, pools={pool.id: pool}),
            "meta": {"limit": limit, "next_cursor": next_cursor},
        })
        response.headers["Cache-Control"] = "no-store"
        return response


@router.post("/talent-pools/{pool_id}/memberships", response_model=DataResource, status_code=201)
def create_talent_pool_membership(pool_id: UUID, payload: MembershipCreate, request: Request, idempotency_key: str | None = Header(None, alias="Idempotency-Key")):
    principal = _principal(request)
    if isinstance(principal, JSONResponse):
        return principal
    key = _idempotency(request, idempotency_key)
    if isinstance(key, JSONResponse):
        return key
    with request.app.state.identity_store.sync_session() as db:
        pool = _load_pool(db, principal, pool_id, manage=True)
        if pool is None or _active_user(db, principal.organization_id, payload.owner_id) is None:
            return _denied(request)
        candidate = db.scalar(select(Candidate).where(Candidate.organization_id == principal.organization_id, Candidate.id == payload.candidate_id, AUTH.candidate_predicate(principal, RecruitingAction.READ, Candidate)))
        if candidate is None:
            return _denied(request)
        source = None
        if payload.source_application_id:
            source = _authorized_application(
                db,
                principal,
                payload.source_application_id,
                payload.candidate_id,
            )
            if source is None:
                return _denied(request)

        def action():
            lock_active_candidate(db, principal.organization_id, candidate.id)
            membership = TalentPoolMembership(
                organization_id=principal.organization_id,
                pool_id=pool.id,
                candidate_id=candidate.id,
                source_application_id=source.id if source else None,
                owner_id=payload.owner_id,
                suitable_roles=payload.suitable_roles,
                tags=payload.tags,
                reason=payload.reason,
                next_contact_at=payload.next_contact_at,
                retention_until=payload.retention_until,
            )
            db.add(membership)
            db.flush()
            recalculate_candidate_retention(db, principal.organization_id, candidate.id)
            db.add(AuditLog(organization_id=principal.organization_id, actor_user_id=principal.user_id, event_type="talent_pool.member_added", outcome="success", trace_id=request.state.trace_id, metadata_json={"pool_id": str(pool.id), "membership_id": str(membership.id), "candidate_id": str(candidate.id)}))
            db.flush()
            return 201, {"data": _membership_data(db, principal, membership)}

        try:
            status, body = persisted_idempotent(db, principal.organization_id, principal.user_id, "talent_pool.member_add", key, {"pool_id": pool_id, **payload.model_dump()}, action)
            db.commit()
        except IntegrityError:
            db.rollback()
            return problem(request, 409, "talent_pool_membership_exists", "The candidate is already in this talent pool.")
        except IdempotencyConflict:
            db.rollback()
            return problem(request, 409, "idempotency_conflict", "The idempotency key was already used.")
        except CandidateUnavailable:
            db.rollback()
            return _denied(request)
        response = JSONResponse(body, status_code=status)
        response.headers["ETag"] = f'"{body["data"]["version"]}"'
        response.headers["Cache-Control"] = "no-store"
        return response


@router.patch("/talent-pool-memberships/{membership_id}", response_model=DataResource)
def patch_talent_pool_membership(membership_id: UUID, payload: MembershipPatch, request: Request, if_match: str | None = Header(None)):
    principal = _principal(request)
    if isinstance(principal, JSONResponse):
        return principal
    expected = _expected_version(request, if_match)
    if isinstance(expected, JSONResponse):
        return expected
    with request.app.state.identity_store.sync_session() as db:
        membership = _load_membership(db, principal, membership_id, manage=True)
        if membership is None:
            return _denied(request)
        candidate_id = membership.candidate_id
        changes = payload.model_dump(exclude_unset=True)
        if "owner_id" in changes and _active_user(db, principal.organization_id, changes["owner_id"]) is None:
            return _denied(request)
        try:
            candidate = lock_active_candidate(db, principal.organization_id, candidate_id)
        except CandidateUnavailable:
            return _denied(request)
        membership = _load_membership(
            db, principal, membership_id, manage=True, for_update=True
        )
        if membership is None or membership.candidate_id != candidate_id:
            return _denied(request)
        if membership.version != expected:
            return problem(request, 409, "resource_version_conflict", "The resource changed. Reload and try again.")
        for field, value in changes.items():
            setattr(membership, field, value)
        membership.version += 1
        membership.updated_at = datetime.now(timezone.utc)
        db.add(AuditLog(organization_id=principal.organization_id, actor_user_id=principal.user_id, event_type="talent_pool.member_updated", outcome="success", trace_id=request.state.trace_id, metadata_json={"membership_id": str(membership.id), "changed_fields": sorted(payload.model_fields_set)}))
        recalculate_candidate_retention(db, principal.organization_id, membership.candidate_id)
        db.commit()
        response = JSONResponse({"data": _membership_data(db, principal, membership)})
        response.headers["ETag"] = f'"{membership.version}"'
        response.headers["Cache-Control"] = "no-store"
        return response


@router.delete("/talent-pool-memberships/{membership_id}", status_code=204)
def delete_talent_pool_membership(membership_id: UUID, payload: MembershipRemoval, request: Request, if_match: str | None = Header(None)):
    principal = _principal(request)
    if isinstance(principal, JSONResponse):
        return principal
    expected = _expected_version(request, if_match)
    if isinstance(expected, JSONResponse):
        return expected
    with request.app.state.identity_store.sync_session() as db:
        membership = _load_membership(db, principal, membership_id, manage=True)
        if membership is None:
            return _denied(request)
        candidate_id = membership.candidate_id
        try:
            candidate = lock_active_candidate(db, principal.organization_id, candidate_id)
        except CandidateUnavailable:
            return _denied(request)
        membership = _load_membership(
            db, principal, membership_id, manage=True, for_update=True
        )
        if membership is None or membership.candidate_id != candidate_id:
            return _denied(request)
        if membership.version != expected:
            return problem(request, 409, "resource_version_conflict", "The resource changed. Reload and try again.")
        db.add(AuditLog(organization_id=principal.organization_id, actor_user_id=principal.user_id, event_type="talent_pool.member_removed", outcome="success", trace_id=request.state.trace_id, metadata_json={"membership_id": str(membership.id), "pool_id": str(membership.pool_id), "candidate_id": str(membership.candidate_id), "reason_provided": True}))
        db.delete(membership)
        db.flush()
        recalculate_candidate_retention(db, principal.organization_id, candidate_id)
        db.commit()
        return Response(status_code=204)


@router.post("/talent-pool-memberships/{membership_id}/reactivations", response_model=DataResource, status_code=201)
def reactivate_talent_pool_membership(membership_id: UUID, payload: ReactivationInput, request: Request, idempotency_key: str | None = Header(None, alias="Idempotency-Key")):
    principal = _principal(request)
    if isinstance(principal, JSONResponse):
        return principal
    key = _idempotency(request, idempotency_key)
    if isinstance(key, JSONResponse):
        return key
    with request.app.state.identity_store.sync_session() as db:
        membership = _load_membership(db, principal, membership_id, manage=True)
        if membership is None:
            return _denied(request)
        candidate_id = membership.candidate_id
        try:
            candidate = lock_active_candidate(db, principal.organization_id, candidate_id)
        except CandidateUnavailable:
            return _denied(request)
        membership = _load_membership(
            db, principal, membership_id, manage=True, for_update=True
        )
        if (
            membership is None
            or membership.candidate_id != candidate_id
            or membership.status != "active"
            or membership.source_application_id is None
        ):
            return _denied(request)
        source = db.scalar(select(Application).where(Application.organization_id == principal.organization_id, Application.id == membership.source_application_id, Application.candidate_id == membership.candidate_id))
        target_job = db.scalar(select(Job).where(Job.organization_id == principal.organization_id, Job.id == payload.job_id, Job.status == "open", AUTH.job_predicate(principal, RecruitingAction.TRANSITION, Job)))
        if source is None or source.stage not in TERMINAL_APPLICATION_STAGES or target_job is None:
            return _denied(request)
        resume_id = payload.resume_id or source.resume_id
        resume = _authorized_resume(db, principal, resume_id, candidate.id)
        if resume is None:
            return _denied(request)

        def action():
            active = db.scalar(select(Application.id).where(Application.organization_id == principal.organization_id, Application.candidate_id == candidate.id, Application.job_id == target_job.id, Application.stage.not_in(TERMINAL_APPLICATION_STAGES)))
            if active is not None:
                raise ActiveApplicationExists
            application = Application(
                organization_id=principal.organization_id,
                candidate_id=candidate.id,
                job_id=target_job.id,
                resume_id=resume.id,
                source_application_id=source.id,
                owner_id=target_job.owner_id,
                stage="new",
                source="talent_pool_reactivation",
            )
            db.add(application)
            db.flush()
            recalculate_candidate_retention(
                db, principal.organization_id, candidate.id
            )
            event_payload = {"source_application_id": str(source.id), "membership_id": str(membership.id), "pool_id": str(membership.pool_id)}
            db.add_all([
                ApplicationStageEvent(organization_id=principal.organization_id, application_id=application.id, actor_user_id=principal.user_id, event_type="application.reactivated", payload=event_payload),
                CandidateEvent(organization_id=principal.organization_id, candidate_id=candidate.id, actor_user_id=principal.user_id, event_type="candidate.reactivated", payload={**event_payload, "application_id": str(application.id), "job_id": str(target_job.id)}),
                AuditLog(organization_id=principal.organization_id, actor_user_id=principal.user_id, event_type="talent_pool.member_reactivated", outcome="success", trace_id=request.state.trace_id, metadata_json={"membership_id": str(membership.id), "application_id": str(application.id), "source_application_id": str(source.id)}),
            ])
            db.flush()
            recalculate_candidate_retention(db, principal.organization_id, candidate.id)
            return 201, {"data": _application_data(application)}

        try:
            status, body = persisted_idempotent(db, principal.organization_id, principal.user_id, "talent_pool.reactivate", key, {"membership_id": membership_id, **payload.model_dump()}, action)
            db.commit()
        except ActiveApplicationExists:
            db.rollback()
            return problem(request, 409, "active_application_exists", "An active application already exists for this job.")
        except IntegrityError as error:
            db.rollback()
            if _is_active_application_conflict(error):
                return problem(request, 409, "active_application_exists", "An active application already exists for this job.")
            raise
        except IdempotencyConflict:
            db.rollback()
            return problem(request, 409, "idempotency_conflict", "The idempotency key was already used.")
        response = JSONResponse(body, status_code=status)
        response.headers["ETag"] = f'"{body["data"]["version"]}"'
        response.headers["Cache-Control"] = "no-store"
        return response


@router.post("/talent-pool-memberships/{membership_id}/review-referrals", response_model=DataResource)
def refer_deferred_membership_to_review(
    membership_id: UUID,
    payload: ReviewReferralInput,
    request: Request,
    if_match: str | None = Header(None, alias="If-Match"),
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
):
    principal=_principal(request); expected=_expected_version(request,if_match); key=_idempotency(request,idempotency_key)
    if isinstance(principal,JSONResponse): return principal
    if isinstance(expected,JSONResponse): return expected
    if isinstance(key,JSONResponse): return key
    operation="talent_pool.review_referral"
    fingerprint={"membership_id":membership_id,"expected_version":expected,**payload.model_dump()}
    with request.app.state.identity_store.sync_session() as db:
        def action():
            visible=_load_review_referral_membership(db,principal,membership_id)
            if visible is None: raise ReviewReferralUnavailable
            try:
                candidate=lock_active_candidate(db,principal.organization_id,visible.candidate_id)
            except CandidateUnavailable as error:
                raise ReviewReferralUnavailable from error
            membership=_load_review_referral_membership(db,principal,membership_id,for_update=True)
            if membership is None or membership.candidate_id!=candidate.id or membership.status!="active" or membership.source_application_id is None:
                raise ReviewReferralUnavailable
            if membership.version!=expected: raise ReviewReferralVersionConflict
            source=db.scalar(select(Application).where(Application.organization_id==principal.organization_id,Application.id==membership.source_application_id,Application.candidate_id==candidate.id).with_for_update())
            if source is None or source.stage!="deferred": raise ReviewReferralUnavailable
            job=db.scalar(select(Job).where(Job.organization_id==principal.organization_id,Job.id==source.job_id,Job.status=="open",AUTH.job_predicate(principal,RecruitingAction.TRANSITION,Job)).with_for_update())
            if job is None: raise ReviewReferralUnavailable
            assignee_id=job.hiring_owner_id or job.owner_id
            if _active_user(db,principal.organization_id,assignee_id) is None: raise ReviewReferralUnavailable
            if job.hiring_owner_id is not None and not db.scalar(select(exists().where(UserRole.user_id==assignee_id,UserRole.role=="hiring_manager"))): raise ReviewReferralUnavailable
            source.stage="review"; source.version+=1; source.updated_at=datetime.now(timezone.utc)
            event_payload={"membership_id":str(membership.id),"pool_id":str(membership.pool_id),"from_stage":"deferred","to_stage":"review"}
            db.add(ApplicationStageEvent(organization_id=principal.organization_id,application_id=source.id,actor_user_id=principal.user_id,event_type="application.stage_changed",payload=event_payload))
            db.add(AuditLog(organization_id=principal.organization_id,actor_user_id=principal.user_id,event_type="talent_pool.review_referred",outcome="success",trace_id=request.state.trace_id,metadata_json={"membership_id":str(membership.id),"application_id":str(source.id)}))
            ensure_review_task(db,application=source,job=job,ai_status="succeeded")
            db.flush(); recalculate_candidate_retention(db,principal.organization_id,candidate.id)
            return 200,{"data":{"application":_application_data(source),"membership":_membership_data(db,principal,membership)}}
        try:
            status,body=persisted_idempotent(db,principal.organization_id,principal.user_id,operation,key,fingerprint,action)
            db.commit()
        except ReviewReferralVersionConflict:
            db.rollback(); return problem(request,409,"version_conflict","The resource version changed.")
        except ReviewReferralUnavailable:
            db.rollback(); return _denied(request)
        except IdempotencyConflict:
            db.rollback(); return problem(request,409,"idempotency_conflict","The idempotency key was already used.")
        response=JSONResponse(body,status_code=status); response.headers["ETag"]=f'"{body["data"]["membership"]["version"]}"'; response.headers["Cache-Control"]="no-store"; return response
