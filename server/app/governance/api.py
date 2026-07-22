from __future__ import annotations

import re
import json
import logging
from datetime import datetime, timedelta, timezone
from uuid import UUID

from fastapi import APIRouter, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from sqlalchemy import and_, or_, select

from server.app.governance.authorization import (
    RECRUITING,
    audit_authorization_class,
    audit_row_predicate,
    can_approve_deletion,
    can_edit_retention,
    can_manage_legal_hold,
    can_read_retention,
    can_read_candidate_governance,
    can_request_candidate_deletion,
    can_view_recruiting_resource,
)
from server.app.governance.deletion_models import DeletionRequest, LegalHold
from server.app.governance.deletion_service import (
    DeletionDomainError,
    active_application_count,
    aware as aware_deletion,
    append_failure_audit,
    approve_deletion_request_locked,
    create_deletion_request_locked,
    load_idempotency_after_lock,
    lock_candidate,
    lock_candidate_deletion_requests,
    lock_candidate_governance_rows,
    lock_approval_legal_holds,
    lock_deletion_request_context,
    lock_legal_hold_context,
    place_legal_hold_locked,
    release_legal_hold_locked,
    safe_hold_projection,
    safe_request_projection,
    store_idempotency_after_lock,
)
from server.app.governance.models import RetentionPolicy
from server.app.governance.schemas import (
    AuditCollection,
    DeletionRequestCollection,
    DeletionRequestCreate,
    DeletionRequestResource,
    DeletionTransitionCreate,
    GovernanceStatusResource,
    LegalHoldCreate,
    LegalHoldReleaseCreate,
    LegalHoldResource,
    RetentionPolicyPatch,
    RetentionPolicyResource,
    RetentionPreviewResource,
    RetentionValues,
)
from server.app.governance.service import (
    GovernanceError,
    GovernanceTokenCodec,
    InvalidGovernanceToken,
    ResourceVersionConflict,
    audit_summary,
    aware,
    policy_projection,
    preview_retention,
    resource_projection,
    safe_network_ref,
    update_retention_policy,
)
from server.app.identity.api import problem, session_token
from server.app.identity.models import AuditLog, User
from server.app.identity.service import InvalidSession
from server.app.recruiting.authorization import RecruitingAction
from server.app.recruiting.models import Candidate
from server.app.recruiting.service import IdempotencyConflict, persisted_idempotent


PROBLEM_RESPONSES = {
    status: {"content": {"application/problem+json": {}}}
    for status in (401, 404, 409, 422, 428)
}


class GovernanceRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()

        async def safe_handler(request: Request):
            try:
                return await original(request)
            except RequestValidationError:
                principal = _principal(request)
                if isinstance(principal, JSONResponse):
                    return principal
                return _rejection_response(
                    request,
                    principal,
                    "governance.request_rejected",
                    422,
                    "validation_failed",
                )
            except Exception as error:
                logging.getLogger(__name__).exception(
                    "governance_request_failed",
                    extra={
                        "context": {
                            "trace_id": getattr(request.state, "trace_id", None),
                            "error_type": type(error).__name__,
                        }
                    },
                )
                return _safe_problem(request, 500, "internal_error")

        return safe_handler


router = APIRouter(
    prefix="/api/v1", responses=PROBLEM_RESPONSES, route_class=GovernanceRoute
)
ETAG = re.compile(r'^"([1-9][0-9]*)"$')
OUTCOMES = frozenset({"success", "denied", "failure"})
DELETION_STATUSES = frozenset({"requested", "approved", "executing", "completed", "failed"})


def _response(
    content,
    *,
    status_code=200,
    etag: int | None = None,
    problem_response: bool = False,
) -> JSONResponse:
    response = JSONResponse(
        content,
        status_code=status_code,
        media_type="application/problem+json" if problem_response else "application/json",
    )
    response.headers["Cache-Control"] = "no-store"
    if etag is not None:
        response.headers["ETag"] = f'"{etag}"'
    return response


def _safe_problem(request: Request, status: int, code: str) -> JSONResponse:
    response = problem(request, status, code, "The request could not be completed.")
    response.headers["Cache-Control"] = "no-store"
    return response


def _principal(request: Request):
    token = session_token(request)
    if not token:
        return _safe_problem(request, 401, "authentication_required")
    try:
        return request.app.state.identity_service.principal(token)
    except InvalidSession:
        return _safe_problem(request, 401, "authentication_required")


def _denied(request: Request) -> JSONResponse:
    return _safe_problem(request, 404, "resource_not_found")


def _audit_cursor_codec(request: Request) -> GovernanceTokenCodec:
    return request.app.state.governance_audit_cursor


def _retention_preview_codec(request: Request) -> GovernanceTokenCodec:
    return request.app.state.governance_retention_preview


def _current_time(request: Request) -> datetime:
    return aware(request.app.state.recruiting_clock.current_time())


def _serialized(model, content):
    return model.model_validate(content).model_dump(mode="json")


def _filters_payload(**values):
    return {
        key: value.isoformat() if isinstance(value, datetime) else str(value) if isinstance(value, UUID) else value
        for key, value in values.items()
        if value is not None
    }


@router.get("/audit-logs", response_model=AuditCollection)
def list_audit_logs(
    request: Request,
    from_: datetime | None = Query(None, alias="from"),
    to_: datetime | None = Query(None, alias="to"),
    actor_id: UUID | None = None,
    event_type: str | None = Query(None, min_length=3, max_length=100),
    resource_type: str | None = Query(None, min_length=1, max_length=64),
    resource_id: UUID | None = None,
    outcome: str | None = None,
    cursor: str | None = Query(None, min_length=40, max_length=4096),
    limit: int = Query(50, ge=1, le=100),
):
    principal = _principal(request)
    if isinstance(principal, JSONResponse):
        return principal
    authorization_class = audit_authorization_class(principal)
    if not authorization_class:
        return _denied(request)
    now = _current_time(request)
    start = aware(from_) if from_ is not None else now - timedelta(days=30)
    end = aware(to_) if to_ is not None else now
    if (from_ is not None and from_.tzinfo is None) or (to_ is not None and to_.tzinfo is None):
        return _safe_problem(request, 422, "validation_failed")
    if start > end or end - start > timedelta(days=90):
        return _safe_problem(request, 422, "validation_failed")
    if outcome is not None and outcome not in OUTCOMES:
        return _safe_problem(request, 422, "validation_failed")
    if (resource_id is None) != (resource_type is None):
        return _safe_problem(request, 422, "validation_failed")
    filters = _filters_payload(
        from_=start,
        to=end,
        actor_id=actor_id,
        event_type=event_type,
        resource_type=resource_type,
        resource_id=resource_id,
        outcome=outcome,
    )
    with request.app.state.identity_store.sync_session() as db:
        if resource_id is not None:
            if resource_type == "retention_policy":
                visible = can_read_retention(principal) and db.scalar(
                    select(RetentionPolicy.id).where(
                        RetentionPolicy.organization_id == principal.organization_id,
                        RetentionPolicy.id == resource_id,
                    )
                ) is not None
            else:
                visible = can_view_recruiting_resource(db, principal, resource_type, resource_id)
            if not visible:
                return _denied(request)
        authorization = audit_row_predicate(principal, AuditLog)
        conditions = [
            AuditLog.organization_id == principal.organization_id,
            authorization,
            AuditLog.created_at >= start,
            AuditLog.created_at <= end,
        ]
        if actor_id is not None:
            conditions.append(AuditLog.actor_user_id == actor_id)
        if event_type is not None:
            conditions.append(AuditLog.event_type == event_type)
        if resource_type is not None:
            conditions.append(AuditLog.resource_type == resource_type)
            conditions.append(AuditLog.resource_id == resource_id)
        if outcome is not None:
            conditions.append(AuditLog.outcome == outcome)
        if cursor is not None:
            try:
                decoded = _audit_cursor_codec(request).decode(cursor)
                expected = {
                    "kind": "audit_cursor",
                    "organization_id": str(principal.organization_id),
                    "authorization_class": authorization_class,
                    "filters": filters,
                }
                if any(decoded.get(key) != value for key, value in expected.items()):
                    raise InvalidGovernanceToken
                cursor_time = datetime.fromisoformat(decoded["created_at"])
                cursor_id = UUID(decoded["id"])
            except (InvalidGovernanceToken, KeyError, TypeError, ValueError):
                return _safe_problem(request, 422, "validation_failed")
            conditions.append(
                or_(
                    AuditLog.created_at < cursor_time,
                    and_(AuditLog.created_at == cursor_time, AuditLog.id < cursor_id),
                )
            )
        rows = db.execute(
            select(AuditLog, User.display_name)
            .outerjoin(
                User,
                and_(
                    User.id == AuditLog.actor_user_id,
                    User.organization_id == AuditLog.organization_id,
                ),
            )
            .where(*conditions)
            .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
            .limit(limit + 1)
        ).all()
        next_cursor = None
        if len(rows) > limit:
            last = rows[limit - 1][0]
            next_cursor = _audit_cursor_codec(request).encode(
                {
                    "kind": "audit_cursor",
                    "organization_id": str(principal.organization_id),
                    "authorization_class": authorization_class,
                    "filters": filters,
                    "created_at": aware(last.created_at).isoformat(),
                    "id": str(last.id),
                }
            )
            rows = rows[:limit]
        data = [
            {
                "id": row.id,
                "created_at": aware(row.created_at),
                "actor": {
                    "id": row.actor_user_id,
                    "display_name": display_name or "Deleted user",
                },
                "category": row.category,
                "event_type": row.event_type,
                "resource": resource_projection(
                    db, principal, row.resource_type, row.resource_id
                ),
                "outcome": row.outcome,
                "network_ref": safe_network_ref(row.ip_hash),
                "trace_id": row.trace_id,
                "summary": audit_summary(row.event_type, row.metadata_json),
            }
            for row, display_name in rows
        ]
    return _response(
        _serialized(AuditCollection, {"data": data, "meta": {"next_cursor": next_cursor, "limit": limit}})
    )


@router.get("/settings/retention-policy", response_model=RetentionPolicyResource)
def get_retention_policy(request: Request):
    principal = _principal(request)
    if isinstance(principal, JSONResponse):
        return principal
    if not can_read_retention(principal):
        return _denied(request)
    with request.app.state.identity_store.sync_session() as db:
        policy = db.scalar(
            select(RetentionPolicy).where(
                RetentionPolicy.organization_id == principal.organization_id
            )
        )
        if policy is None:
            return _denied(request)
        content = _serialized(RetentionPolicyResource, {"data": policy_projection(db, policy)})
    return _response(content, etag=policy.version)


@router.post(
    "/settings/retention-policy/previews", response_model=RetentionPreviewResource
)
def preview_retention_policy(payload: RetentionValues, request: Request):
    principal = _principal(request)
    if isinstance(principal, JSONResponse):
        return principal
    if not can_edit_retention(principal):
        return _denied(request)
    with request.app.state.identity_store.sync_session() as db:
        preview = preview_retention(
            db,
            principal,
            payload.model_dump(),
            _retention_preview_codec(request),
            _current_time(request),
        )
        content = _serialized(RetentionPreviewResource, {"data": preview})
    return _response(content)


@router.patch("/settings/retention-policy", response_model=RetentionPolicyResource)
def patch_retention_policy(
    payload: RetentionPolicyPatch,
    request: Request,
    if_match: str | None = Header(None, alias="If-Match"),
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
):
    principal = _principal(request)
    if isinstance(principal, JSONResponse):
        return principal
    if not can_edit_retention(principal):
        return _denied(request)
    if if_match is None:
        return _safe_problem(request, 428, "precondition_required")
    match = ETAG.fullmatch(if_match)
    if match is None:
        return _safe_problem(request, 422, "validation_failed")
    if not idempotency_key or len(idempotency_key) > 255:
        return _safe_problem(request, 428, "idempotency_key_required")
    expected_version = int(match.group(1))
    values = payload.model_dump(exclude={"impact_token"})
    body = {**payload.model_dump(), "if_match": if_match}
    with request.app.state.identity_store.sync_session() as db:
        try:
            status, response_body = persisted_idempotent(
                db,
                principal.organization_id,
                principal.user_id,
                "retention_policy.update",
                idempotency_key,
                body,
                lambda: (
                    200,
                    _serialized(
                        RetentionPolicyResource,
                        {
                            "data": update_retention_policy(
                                db,
                                principal,
                                values,
                                payload.impact_token,
                                expected_version,
                                _retention_preview_codec(request),
                                _current_time(request),
                                request.state.trace_id,
                            )
                        },
                    ),
                ),
            )
            db.commit()
        except IdempotencyConflict:
            db.rollback()
            return _safe_problem(request, 409, "idempotency_conflict")
        except ResourceVersionConflict:
            db.rollback()
            return _safe_problem(request, 409, "resource_version_conflict")
        except GovernanceError as error:
            db.rollback()
            return _safe_problem(request, 409, error.code)
    return _response(response_body, status_code=status, etag=response_body["data"]["version"])


def _idempotency_key(request: Request, value: str | None) -> str | JSONResponse:
    if not value or len(value) > 255:
        return _safe_problem(request, 428, "idempotency_key_required")
    return value


def _expected_version(request: Request, value: str | None) -> int | JSONResponse:
    if value is None:
        return _safe_problem(request, 428, "precondition_required")
    match = ETAG.fullmatch(value)
    if match is None:
        return _safe_problem(request, 422, "validation_failed")
    return int(match.group(1))


def _problem_body(request: Request, status: int, code: str) -> dict:
    response = _safe_problem(request, status, code)
    return json.loads(response.body)


def _stored_response(record) -> JSONResponse:
    response = JSONResponse(
        record.response_json,
        status_code=record.status_code,
        media_type=(
            "application/problem+json"
            if record.status_code >= 400
            else "application/json"
        ),
    )
    response.headers["Cache-Control"] = "no-store"
    if isinstance(record.response_json, dict) and isinstance(record.response_json.get("data"), dict):
        version = record.response_json["data"].get("version")
        if isinstance(version, int):
            response.headers["ETag"] = f'"{version}"'
    return response


def _audit_rejection(
    request: Request,
    principal,
    event_type: str,
    code: str,
    *,
    resource_type: str | None = None,
    resource_id=None,
) -> bool:
    try:
        with request.app.state.identity_store.sync_session() as audit_db:
            append_failure_audit(
                audit_db,
                principal=principal,
                event_type=event_type,
                trace_id=request.state.trace_id,
                safe_error_code=code,
                resource_type=resource_type,
                resource_id=resource_id,
            )
            audit_db.commit()
        return True
    except Exception:
        logging.getLogger(__name__).exception(
            "governance_audit_rejection_failed",
            extra={"context": {"trace_id": request.state.trace_id}},
        )
        return False


def _rejection_response(request, principal, event_type: str, status: int, code: str):
    if not _audit_rejection(request, principal, event_type, code):
        return _safe_problem(request, 503, "audit_unavailable")
    return _safe_problem(request, status, code)


def _validated_rejection(request, principal, event_type: str, response: JSONResponse):
    body = json.loads(response.body)
    return _rejection_response(
        request, principal, event_type, response.status_code, body["code"]
    )


def _denied_with_audit(request: Request, principal, event_type: str) -> JSONResponse:
    return _rejection_response(
        request, principal, event_type, 404, "resource_not_found"
    )


def _domain_status(code: str) -> int:
    if code == "resource_not_found":
        return 404
    if code in {"validation_failed"}:
        return 422
    return 409


def _audit_read_success(db, principal, event_type: str, trace_id: str) -> None:
    from server.app.governance.audit import append_audit

    append_audit(
        db,
        actor=principal,
        category="governance",
        event_type=event_type,
        outcome="success",
        trace_id=trace_id,
        metadata={},
    )


@router.post(
    "/candidates/{candidate_id}/deletion-requests",
    response_model=DeletionRequestResource,
    status_code=201,
)
def create_deletion_request(
    candidate_id: UUID,
    payload: DeletionRequestCreate,
    request: Request,
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
):
    principal = _principal(request)
    if isinstance(principal, JSONResponse):
        return principal
    with request.app.state.identity_store.sync_session() as db:
        if not can_request_candidate_deletion(db, principal, candidate_id):
            return _denied_with_audit(
                request, principal, "governance.deletion_requested"
            )
        key = _idempotency_key(request, idempotency_key)
        if isinstance(key, JSONResponse):
            return _validated_rejection(
                request, principal, "governance.deletion_requested", key
            )
        candidate = lock_candidate(db, principal.organization_id, candidate_id)
        if candidate is None:
            return _denied_with_audit(
                request, principal, "governance.deletion_requested"
            )
        lock_candidate_deletion_requests(
            db, principal.organization_id, candidate_id
        )
        fingerprint = {"candidate_id": str(candidate_id), **payload.model_dump()}
        try:
            previous = load_idempotency_after_lock(
                db,
                organization_id=principal.organization_id,
                user_id=principal.user_id,
                operation="governance.deletion_request.create",
                key=key,
                body=fingerprint,
            )
            if previous is not None:
                return _stored_response(previous)
            row = create_deletion_request_locked(
                db,
                candidate=candidate,
                principal=principal,
                reason_code=payload.reason_code,
                now=_current_time(request),
                trace_id=request.state.trace_id,
            )
            body = _serialized(
                DeletionRequestResource, {"data": safe_request_projection(row)}
            )
            status = 201
        except IdempotencyConflict:
            db.rollback()
            if not _audit_rejection(
                request,
                principal,
                "governance.deletion_requested",
                "idempotency_conflict",
            ):
                return _safe_problem(request, 503, "audit_unavailable")
            return _safe_problem(request, 409, "idempotency_conflict")
        except DeletionDomainError as error:
            append_failure_audit(
                db,
                principal=principal,
                event_type="governance.deletion_requested",
                trace_id=request.state.trace_id,
                safe_error_code=error.code,
            )
            status = _domain_status(error.code)
            body = _problem_body(request, status, error.code)
        try:
            store_idempotency_after_lock(
                db,
                organization_id=principal.organization_id,
                user_id=principal.user_id,
                operation="governance.deletion_request.create",
                key=key,
                body=fingerprint,
                status_code=status,
                response_json=body,
            )
            db.commit()
        except Exception:
            db.rollback()
            raise
    if status >= 400:
        return _response(body, status_code=status, problem_response=True)
    return _response(body, status_code=status, etag=body["data"]["version"])


@router.get("/deletion-requests", response_model=DeletionRequestCollection)
def list_deletion_requests(
    request: Request,
    status: str | None = None,
    cursor: str | None = Query(None, min_length=40, max_length=4096),
    limit: int = Query(50, ge=1, le=100),
):
    principal = _principal(request)
    if isinstance(principal, JSONResponse):
        return principal
    if status is not None and status not in DELETION_STATUSES:
        return _rejection_response(
            request, principal, "governance.request_rejected", 422, "validation_failed"
        )
    system_admin = can_approve_deletion(principal)
    if not system_admin and not principal.active:
        return _denied_with_audit(
            request, principal, "governance.deletion_requests_listed"
        )
    with request.app.state.identity_store.sync_session() as db:
        query = select(DeletionRequest).where(
            DeletionRequest.organization_id == principal.organization_id
        )
        if not system_admin:
            query = (
                select(DeletionRequest)
                .join(
                    Candidate,
                    and_(
                        Candidate.organization_id == DeletionRequest.organization_id,
                        Candidate.id == DeletionRequest.candidate_id,
                    ),
                )
                .where(
                    DeletionRequest.organization_id == principal.organization_id,
                    DeletionRequest.requested_by == principal.user_id,
                    RECRUITING.candidate_predicate(
                        principal,
                        RecruitingAction.READ,
                        Candidate,
                    ),
                )
            )
        if status is not None:
            query = query.where(DeletionRequest.status == status)
        if cursor is not None:
            try:
                decoded = _audit_cursor_codec(request).decode(cursor)
                expected = {
                    "kind": "deletion_request_cursor",
                    "organization_id": str(principal.organization_id),
                    "authorization": "system" if system_admin else "requester",
                    "requester_id": None if system_admin else str(principal.user_id),
                    "status": status,
                }
                if any(decoded.get(key) != value for key, value in expected.items()):
                    raise InvalidGovernanceToken
                created_at = datetime.fromisoformat(decoded["created_at"])
                cursor_id = UUID(decoded["id"])
            except (InvalidGovernanceToken, KeyError, TypeError, ValueError):
                return _rejection_response(
                    request,
                    principal,
                    "governance.request_rejected",
                    422,
                    "validation_failed",
                )
            query = query.where(
                or_(
                    DeletionRequest.created_at < created_at,
                    and_(
                        DeletionRequest.created_at == created_at,
                        DeletionRequest.id < cursor_id,
                    ),
                )
            )
        rows = list(
            db.scalars(
                query.order_by(
                    DeletionRequest.created_at.desc(), DeletionRequest.id.desc()
                ).limit(limit + 1)
            )
        )
        next_cursor = None
        if len(rows) > limit:
            last = rows[limit - 1]
            next_cursor = _audit_cursor_codec(request).encode(
                {
                    "kind": "deletion_request_cursor",
                    "organization_id": str(principal.organization_id),
                    "authorization": "system" if system_admin else "requester",
                    "requester_id": None if system_admin else str(principal.user_id),
                    "status": status,
                    "created_at": aware_deletion(last.created_at).isoformat(),
                    "id": str(last.id),
                }
            )
            rows = rows[:limit]
        try:
            _audit_read_success(
                db, principal, "governance.deletion_requests_listed", request.state.trace_id
            )
            db.commit()
        except Exception:
            db.rollback()
            return _safe_problem(request, 503, "audit_unavailable")
        body = _serialized(
            DeletionRequestCollection,
            {
                "data": [safe_request_projection(row) for row in rows],
                "meta": {"next_cursor": next_cursor, "limit": limit},
            },
        )
    return _response(body)


def _can_read_deletion_request(db, principal, row: DeletionRequest) -> bool:
    if can_approve_deletion(principal):
        return True
    return row.requested_by == principal.user_id and can_read_candidate_governance(
        db, principal, row.candidate_id
    )


@router.get("/deletion-requests/{request_id}", response_model=DeletionRequestResource)
def get_deletion_request(request_id: UUID, request: Request):
    principal = _principal(request)
    if isinstance(principal, JSONResponse):
        return principal
    with request.app.state.identity_store.sync_session() as db:
        row = db.scalar(
            select(DeletionRequest).where(
                DeletionRequest.organization_id == principal.organization_id,
                DeletionRequest.id == request_id,
            )
        )
        if row is None or not _can_read_deletion_request(db, principal, row):
            return _denied_with_audit(
                request, principal, "governance.deletion_request_read"
            )
        body = _serialized(
            DeletionRequestResource,
            {
                "data": safe_request_projection(
                    row, active_application_count=active_application_count(db, row)
                )
            },
        )
        try:
            _audit_read_success(
                db, principal, "governance.deletion_request_read", request.state.trace_id
            )
            db.commit()
        except Exception:
            db.rollback()
            return _safe_problem(request, 503, "audit_unavailable")
    return _response(body, etag=row.version)


@router.post(
    "/deletion-requests/{request_id}/transitions",
    response_model=DeletionRequestResource,
)
def transition_deletion_request(
    request_id: UUID,
    payload: DeletionTransitionCreate,
    request: Request,
    if_match: str | None = Header(None, alias="If-Match"),
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
):
    principal = _principal(request)
    if isinstance(principal, JSONResponse):
        return principal
    if not can_approve_deletion(principal):
        return _denied_with_audit(
            request, principal, "governance.deletion_approved"
        )
    expected = _expected_version(request, if_match)
    if isinstance(expected, JSONResponse):
        return _validated_rejection(
            request, principal, "governance.deletion_approved", expected
        )
    key = _idempotency_key(request, idempotency_key)
    if isinstance(key, JSONResponse):
        return _validated_rejection(
            request, principal, "governance.deletion_approved", key
        )
    operation = "governance.deletion_request.approve"
    fingerprint = {
        "request_id": str(request_id),
        **payload.model_dump(),
        "if_match": if_match,
    }
    with request.app.state.identity_store.sync_session() as db:
        context = lock_deletion_request_context(
            db, principal.organization_id, request_id
        )
        if context is None:
            return _denied_with_audit(
                request, principal, "governance.deletion_approved"
            )
        candidate, row = context
        lock_approval_legal_holds(db, row)
        try:
            previous = load_idempotency_after_lock(
                db,
                organization_id=principal.organization_id,
                user_id=principal.user_id,
                operation=operation,
                key=key,
                body=fingerprint,
            )
            if previous is not None:
                return _stored_response(previous)
            result = approve_deletion_request_locked(
                db,
                candidate=candidate,
                row=row,
                principal=principal,
                expected_version=expected,
                terminate_active_applications=payload.terminate_active_applications,
                now=_current_time(request),
                trace_id=request.state.trace_id,
            )
            resource = _serialized(
                DeletionRequestResource,
                {
                    "data": safe_request_projection(
                        result.request,
                        active_application_count=active_application_count(
                            db, result.request
                        ),
                    )
                },
            )
            if result.enqueued:
                status = 200
                body = resource
            else:
                status = 409
                body = {
                    **_problem_body(request, 409, "stale_manifest"),
                    "data": resource["data"],
                }
        except IdempotencyConflict:
            db.rollback()
            if not _audit_rejection(
                request,
                principal,
                "governance.deletion_approved",
                "idempotency_conflict",
            ):
                return _safe_problem(request, 503, "audit_unavailable")
            return _safe_problem(request, 409, "idempotency_conflict")
        except DeletionDomainError as error:
            append_failure_audit(
                db,
                principal=principal,
                event_type="governance.deletion_approved",
                trace_id=request.state.trace_id,
                safe_error_code=error.code,
                resource_type="deletion_request",
                resource_id=row.id,
            )
            status = _domain_status(error.code)
            body = _problem_body(request, status, error.code)
        store_idempotency_after_lock(
            db,
            organization_id=principal.organization_id,
            user_id=principal.user_id,
            operation=operation,
            key=key,
            body=fingerprint,
            status_code=status,
            response_json=body,
        )
        db.commit()
    response = _response(
        body,
        status_code=status,
        etag=body.get("data", {}).get("version"),
        problem_response=status >= 400,
    )
    return response


@router.post(
    "/candidates/{candidate_id}/legal-holds",
    response_model=LegalHoldResource,
    status_code=201,
)
def place_legal_hold(
    candidate_id: UUID,
    payload: LegalHoldCreate,
    request: Request,
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
):
    principal = _principal(request)
    if isinstance(principal, JSONResponse):
        return principal
    with request.app.state.identity_store.sync_session() as db:
        if not can_manage_legal_hold(db, principal, candidate_id):
            return _denied_with_audit(
                request, principal, "governance.legal_hold_placed"
            )
        key = _idempotency_key(request, idempotency_key)
        if isinstance(key, JSONResponse):
            return _validated_rejection(
                request, principal, "governance.legal_hold_placed", key
            )
        candidate = lock_candidate(db, principal.organization_id, candidate_id)
        if candidate is None:
            return _denied_with_audit(
                request, principal, "governance.legal_hold_placed"
            )
        lock_candidate_governance_rows(
            db, principal.organization_id, candidate_id
        )
        operation = "governance.legal_hold.place"
        fingerprint = {"candidate_id": str(candidate_id), **payload.model_dump()}
        try:
            previous = load_idempotency_after_lock(
                db,
                organization_id=principal.organization_id,
                user_id=principal.user_id,
                operation=operation,
                key=key,
                body=fingerprint,
            )
            if previous is not None:
                return _stored_response(previous)
            hold = place_legal_hold_locked(
                db,
                candidate=candidate,
                principal=principal,
                reason=payload.reason,
                now=_current_time(request),
                trace_id=request.state.trace_id,
            )
            body = _serialized(
                LegalHoldResource,
                {"data": safe_hold_projection(hold, include_reason=True)},
            )
            status = 201
        except IdempotencyConflict:
            db.rollback()
            if not _audit_rejection(
                request,
                principal,
                "governance.legal_hold_placed",
                "idempotency_conflict",
            ):
                return _safe_problem(request, 503, "audit_unavailable")
            return _safe_problem(request, 409, "idempotency_conflict")
        except DeletionDomainError as error:
            append_failure_audit(
                db,
                principal=principal,
                event_type="governance.legal_hold_placed",
                trace_id=request.state.trace_id,
                safe_error_code=error.code,
            )
            status = _domain_status(error.code)
            body = _problem_body(request, status, error.code)
        store_idempotency_after_lock(
            db,
            organization_id=principal.organization_id,
            user_id=principal.user_id,
            operation=operation,
            key=key,
            body=fingerprint,
            status_code=status,
            response_json=body,
        )
        db.commit()
    if status >= 400:
        return _response(body, status_code=status, problem_response=True)
    return _response(body, status_code=status, etag=body["data"]["version"])


@router.post(
    "/legal-holds/{hold_id}/releases", response_model=LegalHoldResource
)
def release_legal_hold(
    hold_id: UUID,
    payload: LegalHoldReleaseCreate,
    request: Request,
    if_match: str | None = Header(None, alias="If-Match"),
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
):
    principal = _principal(request)
    if isinstance(principal, JSONResponse):
        return principal
    expected = _expected_version(request, if_match)
    if isinstance(expected, JSONResponse):
        return _validated_rejection(
            request, principal, "governance.legal_hold_released", expected
        )
    key = _idempotency_key(request, idempotency_key)
    if isinstance(key, JSONResponse):
        return _validated_rejection(
            request, principal, "governance.legal_hold_released", key
        )
    operation = "governance.legal_hold.release"
    fingerprint = {
        "hold_id": str(hold_id),
        **payload.model_dump(),
        "if_match": if_match,
    }
    with request.app.state.identity_store.sync_session() as db:
        candidate_id = db.scalar(
            select(LegalHold.candidate_id).where(
                LegalHold.organization_id == principal.organization_id,
                LegalHold.id == hold_id,
            )
        )
        if candidate_id is None or not can_manage_legal_hold(
            db, principal, candidate_id
        ):
            return _denied_with_audit(
                request, principal, "governance.legal_hold_released"
            )
        context = lock_legal_hold_context(db, principal.organization_id, hold_id)
        if context is None:
            return _denied_with_audit(
                request, principal, "governance.legal_hold_released"
            )
        _, hold = context
        try:
            previous = load_idempotency_after_lock(
                db,
                organization_id=principal.organization_id,
                user_id=principal.user_id,
                operation=operation,
                key=key,
                body=fingerprint,
            )
            if previous is not None:
                return _stored_response(previous)
            hold = release_legal_hold_locked(
                db,
                hold=hold,
                principal=principal,
                reason=payload.reason,
                expected_version=expected,
                now=_current_time(request),
                trace_id=request.state.trace_id,
            )
            body = _serialized(
                LegalHoldResource,
                {"data": safe_hold_projection(hold, include_reason=True)},
            )
            status = 200
        except IdempotencyConflict:
            db.rollback()
            if not _audit_rejection(
                request,
                principal,
                "governance.legal_hold_released",
                "idempotency_conflict",
            ):
                return _safe_problem(request, 503, "audit_unavailable")
            return _safe_problem(request, 409, "idempotency_conflict")
        except DeletionDomainError as error:
            append_failure_audit(
                db,
                principal=principal,
                event_type="governance.legal_hold_released",
                trace_id=request.state.trace_id,
                safe_error_code=error.code,
                resource_type="legal_hold",
                resource_id=hold.id,
            )
            status = _domain_status(error.code)
            body = _problem_body(request, status, error.code)
        store_idempotency_after_lock(
            db,
            organization_id=principal.organization_id,
            user_id=principal.user_id,
            operation=operation,
            key=key,
            body=fingerprint,
            status_code=status,
            response_json=body,
        )
        db.commit()
    if status >= 400:
        return _response(body, status_code=status, problem_response=True)
    return _response(body, status_code=status, etag=body["data"]["version"])


@router.get(
    "/candidates/{candidate_id}/governance-status",
    response_model=GovernanceStatusResource,
)
def get_candidate_governance_status(candidate_id: UUID, request: Request):
    principal = _principal(request)
    if isinstance(principal, JSONResponse):
        return principal
    with request.app.state.identity_store.sync_session() as db:
        if not can_read_candidate_governance(db, principal, candidate_id):
            return _denied_with_audit(
                request, principal, "governance.candidate_status_read"
            )
        deletion = db.scalar(
            select(DeletionRequest)
            .where(
                DeletionRequest.organization_id == principal.organization_id,
                DeletionRequest.candidate_id == candidate_id,
            )
            .order_by(DeletionRequest.created_at.desc(), DeletionRequest.id.desc())
            .limit(1)
        )
        hold = db.scalar(
            select(LegalHold).where(
                LegalHold.organization_id == principal.organization_id,
                LegalHold.candidate_id == candidate_id,
                LegalHold.released_at.is_(None),
            )
        )
        data = {
            "deletion_status": deletion.status if deletion else None,
            "deletion_request_id": deletion.id if deletion else None,
            "legal_hold_active": hold is not None,
        }
        if hold is not None and "recruiting_admin" in principal.roles:
            data["legal_hold_reason"] = hold.reason
            data["legal_hold_id"] = hold.id
            data["legal_hold_version"] = hold.version
        body = GovernanceStatusResource.model_validate({"data": data}).model_dump(
            mode="json", exclude_none=True
        )
        try:
            _audit_read_success(
                db, principal, "governance.candidate_status_read", request.state.trace_id
            )
            db.commit()
        except Exception:
            db.rollback()
            return _safe_problem(request, 503, "audit_unavailable")
    return _response(body)
