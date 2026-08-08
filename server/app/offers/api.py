import re
from uuid import UUID

from fastapi import APIRouter, Header, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select

from server.app.identity.api import problem
from server.app.identity.models import AuditLog, Job
from server.app.offers.models import Offer, OfferApproval, OfferEvent, OfferTemplate, OfferVersion, OrganizationSpecialOfferApprover
from server.app.offers.schemas import OfferApprovalDecision, OfferCommand, OfferTemplateCommand, OfferVersionCommand, SpecialOfferApproversCommand
from server.app.offers.service import (
    OfferApprovalError,
    OfferNotFound,
    OfferVersionConflict,
    create_offer,
    decide_approval,
    submit_offer,
    update_offer_version,
    withdraw_offer,
)
from server.app.recruiting.api import _idempotency, _principal
from server.app.recruiting.authorization import RecruitingAction, RecruitingAuthorizationService
from server.app.recruiting.models import Application
from server.app.recruiting.service import IdempotencyConflict, persisted_idempotent
from server.app.recruiting.service import is_eligible_offer_approver
from server.app.communications.interview_messages import CandidateEmailUnavailable, resolve_confirmed_candidate_email


router = APIRouter()
AUTH = RecruitingAuthorizationService()
ETAG = re.compile(r'^"(0|[1-9][0-9]*)"$')


def _response(data, status=200, *, meta=None, etag=None):
    body = {"data": data}
    if meta is not None:
        body["meta"] = meta
    response = JSONResponse(body, status_code=status)
    response.headers["Cache-Control"] = "no-store"
    if etag is not None:
        response.headers["ETag"] = f'"{etag}"'
    return response


def _error(request, status, code):
    response = problem(request, status, code, "The request could not be completed.")
    response.headers["Cache-Control"] = "no-store"
    return response


def _version(request, value):
    if value is None:
        return _error(request, 428, "precondition_required")
    match = ETAG.fullmatch(value)
    return int(match.group(1)) if match else _error(request, 422, "validation_failed")


def _admin(principal):
    return principal.active and "recruiting_admin" in principal.roles


def _application(db, principal, application_id, action=RecruitingAction.READ):
    return db.scalar(select(Application).join(Job, (Job.organization_id == Application.organization_id) & (Job.id == Application.job_id)).where(
        Application.organization_id == principal.organization_id,
        Application.id == application_id,
        AUTH.job_predicate(principal, action, Job),
    ))


def _offer(db, principal, offer_id, action=RecruitingAction.READ):
    return db.scalar(select(Offer).join(Job, (Job.organization_id == Offer.organization_id) & (Job.id == Offer.job_id)).where(
        Offer.organization_id == principal.organization_id, Offer.id == offer_id,
        AUTH.job_predicate(principal, action, Job),
    ))


def _can_manage(db, principal, offer):
    application = db.scalar(select(Application).where(Application.organization_id == offer.organization_id, Application.id == offer.application_id))
    if application is None:
        return False
    if _admin(principal):
        return True
    return application.owner_id == principal.user_id and db.scalar(select(Job.id).where(
        Job.organization_id == offer.organization_id,
        Job.id == offer.job_id,
        AUTH.job_predicate(principal, RecruitingAction.MANAGE_CANDIDATE, Job),
    )) is not None


def _offer_view(db, offer, principal):
    current = db.scalar(select(OfferVersion).where(OfferVersion.organization_id == offer.organization_id, OfferVersion.id == offer.current_version_id))
    can_manage = _can_manage(db, principal, offer)
    is_assignee = db.scalar(select(OfferApproval.id).where(OfferApproval.organization_id == offer.organization_id, OfferApproval.offer_id == offer.id, OfferApproval.assignee_id == principal.user_id, OfferApproval.status == "pending")) is not None
    content = current.content if can_manage else {"redacted": True}
    return {
        "id": str(offer.id), "application_id": str(offer.application_id), "job_id": str(offer.job_id),
        "status": offer.status, "version": offer.version, "current_version_id": str(offer.current_version_id),
        "current_version_number": current.version_number if current else None,
        "candidate_response_deadline": offer.candidate_response_deadline.isoformat(), "is_special": offer.is_special,
        "special_reason": offer.special_reason if can_manage else None, "content": content,
        "pdf_ready": bool(current and current.pdf_object_key),
        "allowed_actions": {
            "update": can_manage and offer.status in {"draft", "changes_requested", "ready_to_send", "sent"},
            "submit": can_manage and offer.status in {"draft", "changes_requested"},
            "withdraw": can_manage and offer.status not in {"withdrawn", "expired"},
            "send": can_manage and offer.status == "ready_to_send",
            "decide": is_assignee and offer.status == "pending_approval",
        },
    }


def _run_mutation(request, principal, operation, key, semantic, action):
    with request.app.state.identity_store.sync_session() as db:
        try:
            status, body = persisted_idempotent(db, principal.organization_id, principal.user_id, operation, key, semantic, action(db))
            db.commit()
        except IdempotencyConflict:
            db.rollback(); return _error(request, 409, "idempotency_conflict")
        except OfferNotFound:
            db.rollback(); return _error(request, 404, "resource_not_found")
        except OfferVersionConflict:
            db.rollback(); return _error(request, 409, "resource_version_conflict")
        except OfferApprovalError:
            db.rollback(); return _error(request, 409, "invalid_offer_state")
    return _response(body["data"], status, etag=body["data"].get("version"))


def _template_view(template):
    return {"id": str(template.id), "name": template.name, "content": template.content, "status": template.status, "version": template.version}


def _special_version(rows):
    if not rows:
        return 0
    return max(int(row.updated_at.timestamp() * 1_000_000) for row in rows)


def _audit_setting(db, principal, request, event_type, resource_type, resource_id, metadata):
    db.add(AuditLog(organization_id=principal.organization_id, actor_user_id=principal.user_id, category="recruiting", event_type=event_type, outcome="success", resource_type=resource_type, resource_id=resource_id, trace_id=request.state.trace_id, metadata_json=metadata))


@router.post("/api/v1/offers")
def create_offer_api(payload: OfferCommand, request: Request, idempotency_key: str | None = Header(None, alias="Idempotency-Key")):
    principal, key = _principal(request), _idempotency(request, idempotency_key)
    if isinstance(principal, JSONResponse): return principal
    if isinstance(key, JSONResponse): return key
    with request.app.state.identity_store.sync_session() as db:
        application = _application(db, principal, payload.application_id, RecruitingAction.MANAGE_CANDIDATE)
        if application is None or not (_admin(principal) or application.owner_id == principal.user_id): return _error(request, 404, "resource_not_found")
    return _run_mutation(request, principal, "offer.create", key, payload.model_dump(mode="json"), lambda db: lambda: (201, {"data": _offer_view(db, create_offer(db, principal.organization_id, principal.user_id, payload, trace_id=request.state.trace_id), principal)}))


@router.get("/api/v1/offers")
def list_offers(request: Request, application_id: UUID | None = Query(None), limit: int = Query(50, ge=1, le=100)):
    principal = _principal(request)
    if isinstance(principal, JSONResponse): return principal
    with request.app.state.identity_store.sync_session() as db:
        query = select(Offer).join(Job, (Job.organization_id == Offer.organization_id) & (Job.id == Offer.job_id)).where(Offer.organization_id == principal.organization_id, AUTH.job_predicate(principal, RecruitingAction.READ, Job))
        if application_id is not None: query = query.where(Offer.application_id == application_id)
        rows = db.scalars(query.order_by(Offer.updated_at.desc(), Offer.id.desc()).limit(limit)).all()
        return _response([_offer_view(db, row, principal) for row in rows], meta={"limit": limit})


@router.get("/api/v1/offers/{offer_id}")
def get_offer(offer_id: UUID, request: Request):
    principal = _principal(request)
    if isinstance(principal, JSONResponse): return principal
    with request.app.state.identity_store.sync_session() as db:
        offer = _offer(db, principal, offer_id)
        if offer is None: return _error(request, 404, "resource_not_found")
        return _response(_offer_view(db, offer, principal), etag=offer.version)


@router.patch("/api/v1/offers/{offer_id}")
def update_offer(offer_id: UUID, payload: OfferVersionCommand, request: Request, if_match: str | None = Header(None, alias="If-Match"), idempotency_key: str | None = Header(None, alias="Idempotency-Key")):
    principal, expected, key = _principal(request), _version(request, if_match), _idempotency(request, idempotency_key)
    if any(isinstance(item, JSONResponse) for item in (principal, expected, key)): return next(item for item in (principal, expected, key) if isinstance(item, JSONResponse))
    with request.app.state.identity_store.sync_session() as db:
        offer = _offer(db, principal, offer_id)
        if offer is None or not _can_manage(db, principal, offer): return _error(request, 404, "resource_not_found")
    semantic = {"expected_version": expected, **payload.model_dump(mode="json", exclude_unset=True)}
    return _run_mutation(request, principal, f"offer.update:{offer_id}", key, semantic, lambda db: lambda: (200, {"data": _offer_view(db, update_offer_version(db, principal.organization_id, offer_id, principal.user_id, payload, expected_version=expected, trace_id=request.state.trace_id), principal)}))


@router.post("/api/v1/offers/{offer_id}/approvals")
def submit_offer_api(offer_id: UUID, request: Request, if_match: str | None = Header(None, alias="If-Match"), idempotency_key: str | None = Header(None, alias="Idempotency-Key")):
    principal, expected, key = _principal(request), _version(request, if_match), _idempotency(request, idempotency_key)
    if any(isinstance(item, JSONResponse) for item in (principal, expected, key)): return next(item for item in (principal, expected, key) if isinstance(item, JSONResponse))
    with request.app.state.identity_store.sync_session() as db:
        offer = _offer(db, principal, offer_id)
        if offer is None or not _can_manage(db, principal, offer): return _error(request, 404, "resource_not_found")
    return _run_mutation(request, principal, f"offer.submit:{offer_id}", key, {"expected_version": expected}, lambda db: lambda: (200, {"data": _offer_view(db, submit_offer(db, principal.organization_id, offer_id, principal.user_id, expected_version=expected, trace_id=request.state.trace_id), principal)}))


@router.post("/api/v1/offer-approvals/{approval_id}/decisions")
def decide_offer_approval(approval_id: UUID, payload: OfferApprovalDecision, request: Request, if_match: str | None = Header(None, alias="If-Match"), idempotency_key: str | None = Header(None, alias="Idempotency-Key")):
    principal, expected, key = _principal(request), _version(request, if_match), _idempotency(request, idempotency_key)
    if any(isinstance(item, JSONResponse) for item in (principal, expected, key)): return next(item for item in (principal, expected, key) if isinstance(item, JSONResponse))
    with request.app.state.identity_store.sync_session() as db:
        approval = db.scalar(select(OfferApproval).where(OfferApproval.organization_id == principal.organization_id, OfferApproval.id == approval_id, OfferApproval.assignee_id == principal.user_id, OfferApproval.status == "pending"))
        if approval is None: return _error(request, 404, "resource_not_found")
    semantic = {"expected_version": expected, **payload.model_dump()}
    return _run_mutation(request, principal, f"offer.approval.decide:{approval_id}", key, semantic, lambda db: lambda: (200, {"data": _offer_view(db, decide_approval(db, principal.organization_id, approval.offer_id, principal.user_id, payload.decision, expected_version=expected, reason=payload.reason, trace_id=request.state.trace_id), principal)}))


@router.post("/api/v1/offers/{offer_id}/withdrawals")
def withdraw_offer_api(offer_id: UUID, request: Request, if_match: str | None = Header(None, alias="If-Match"), idempotency_key: str | None = Header(None, alias="Idempotency-Key")):
    principal, expected, key = _principal(request), _version(request, if_match), _idempotency(request, idempotency_key)
    if any(isinstance(item, JSONResponse) for item in (principal, expected, key)): return next(item for item in (principal, expected, key) if isinstance(item, JSONResponse))
    with request.app.state.identity_store.sync_session() as db:
        offer = _offer(db, principal, offer_id)
        if offer is None or not _can_manage(db, principal, offer): return _error(request, 404, "resource_not_found")
    return _run_mutation(request, principal, f"offer.withdraw:{offer_id}", key, {"expected_version": expected}, lambda db: lambda: (200, {"data": _offer_view(db, withdraw_offer(db, principal.organization_id, offer_id, principal.user_id, expected_version=expected, trace_id=request.state.trace_id), principal)}))


@router.get("/api/v1/offers/{offer_id}/history")
def offer_history(offer_id: UUID, request: Request):
    principal = _principal(request)
    if isinstance(principal, JSONResponse): return principal
    with request.app.state.identity_store.sync_session() as db:
        offer = _offer(db, principal, offer_id)
        if offer is None: return _error(request, 404, "resource_not_found")
        events = db.scalars(select(OfferEvent).where(OfferEvent.organization_id == principal.organization_id, OfferEvent.offer_id == offer_id).order_by(OfferEvent.created_at, OfferEvent.id)).all()
        can_manage = _can_manage(db, principal, offer)
        return _response([{"id": str(event.id), "event_type": event.event_type, "created_at": event.created_at.isoformat(), "payload": event.payload if can_manage else {}} for event in events])


@router.get("/api/v1/offer-approvals/pending")
def pending_approvals(request: Request):
    principal = _principal(request)
    if isinstance(principal, JSONResponse): return principal
    with request.app.state.identity_store.sync_session() as db:
        rows = db.scalars(select(OfferApproval).where(OfferApproval.organization_id == principal.organization_id, OfferApproval.assignee_id == principal.user_id, OfferApproval.status == "pending").order_by(OfferApproval.created_at)).all()
        return _response([{"id": str(row.id), "offer_id": str(row.offer_id), "sequence": row.sequence, "round_number": row.round_number, "version_number": row.version_number} for row in rows])


@router.post("/api/v1/offers/{offer_id}/send")
def send_offer(offer_id: UUID, request: Request, if_match: str | None = Header(None, alias="If-Match"), idempotency_key: str | None = Header(None, alias="Idempotency-Key")):
    """Task 9 owns token issuance; never change state until that capability exists."""
    principal, expected, key = _principal(request), _version(request, if_match), _idempotency(request, idempotency_key)
    if any(isinstance(item, JSONResponse) for item in (principal, expected, key)): return next(item for item in (principal, expected, key) if isinstance(item, JSONResponse))
    with request.app.state.identity_store.sync_session() as db:
        offer = _offer(db, principal, offer_id)
        if offer is None or not _can_manage(db, principal, offer): return _error(request, 404, "resource_not_found")
        if offer.version != expected: return _error(request, 409, "resource_version_conflict")
        current = db.scalar(select(OfferVersion).where(OfferVersion.organization_id == offer.organization_id, OfferVersion.id == offer.current_version_id))
        if offer.status != "ready_to_send" or current is None or not current.pdf_object_key: return _error(request, 409, "offer_not_ready_to_send")
        application = db.scalar(select(Application).where(Application.organization_id == offer.organization_id, Application.id == offer.application_id))
        try:
            resolve_confirmed_candidate_email(db, organization_id=offer.organization_id, candidate_id=application.candidate_id, contact_cipher=request.app.state.contact_cipher)
        except CandidateEmailUnavailable:
            return _error(request, 409, "candidate_email_unconfirmed")
    return _error(request, 409, "offer_send_unavailable")


@router.get("/api/v1/offer-templates")
def list_offer_templates(request: Request):
    principal = _principal(request)
    if isinstance(principal, JSONResponse): return principal
    if not _admin(principal): return _error(request, 404, "resource_not_found")
    with request.app.state.identity_store.sync_session() as db:
        rows = db.scalars(select(OfferTemplate).where(OfferTemplate.organization_id == principal.organization_id).order_by(OfferTemplate.name)).all()
        return _response([_template_view(row) for row in rows])


@router.post("/api/v1/offer-templates")
def create_offer_template(payload: OfferTemplateCommand, request: Request, idempotency_key: str | None = Header(None, alias="Idempotency-Key")):
    principal, key = _principal(request), _idempotency(request, idempotency_key)
    if isinstance(principal, JSONResponse): return principal
    if isinstance(key, JSONResponse): return key
    if not _admin(principal): return _error(request, 404, "resource_not_found")
    with request.app.state.identity_store.sync_session() as db:
        try:
            def action():
                row = OfferTemplate(organization_id=principal.organization_id, name=payload.name, content=payload.content, status=payload.status)
                db.add(row); db.flush(); _audit_setting(db, principal, request, "offer.template_created", "offer_template", row.id, {"status": row.status})
                return 201, {"data": _template_view(row)}
            status, body = persisted_idempotent(db, principal.organization_id, principal.user_id, "offer.template.create", key, payload.model_dump(mode="json"), action)
            db.commit()
        except IdempotencyConflict:
            db.rollback(); return _error(request, 409, "idempotency_conflict")
        except Exception:
            db.rollback(); return _error(request, 409, "offer_template_conflict")
    return _response(body["data"], status, etag=body["data"]["version"])


@router.put("/api/v1/offer-templates/{template_id}")
def update_offer_template(template_id: UUID, payload: OfferTemplateCommand, request: Request, if_match: str | None = Header(None, alias="If-Match"), idempotency_key: str | None = Header(None, alias="Idempotency-Key")):
    principal, expected, key = _principal(request), _version(request, if_match), _idempotency(request, idempotency_key)
    if any(isinstance(item, JSONResponse) for item in (principal, expected, key)): return next(item for item in (principal, expected, key) if isinstance(item, JSONResponse))
    if not _admin(principal): return _error(request, 404, "resource_not_found")
    with request.app.state.identity_store.sync_session() as db:
        try:
            def action():
                row = db.scalar(select(OfferTemplate).where(OfferTemplate.organization_id == principal.organization_id, OfferTemplate.id == template_id).with_for_update())
                if row is None: raise LookupError
                if row.version != expected: raise OfferVersionConflict
                row.name, row.content, row.status, row.version = payload.name, payload.content, payload.status, row.version + 1
                db.flush(); _audit_setting(db, principal, request, "offer.template_updated", "offer_template", row.id, {"status": row.status}); return 200, {"data": _template_view(row)}
            status, body = persisted_idempotent(db, principal.organization_id, principal.user_id, f"offer.template.update:{template_id}", key, {"expected_version": expected, **payload.model_dump(mode="json")}, action)
            db.commit()
        except LookupError:
            db.rollback(); return _error(request, 404, "resource_not_found")
        except OfferVersionConflict:
            db.rollback(); return _error(request, 409, "resource_version_conflict")
        except IdempotencyConflict:
            db.rollback(); return _error(request, 409, "idempotency_conflict")
    return _response(body["data"], status, etag=body["data"]["version"])


@router.get("/api/v1/settings/offer-special-approvers")
def get_special_offer_approvers(request: Request):
    principal = _principal(request)
    if isinstance(principal, JSONResponse): return principal
    if not _admin(principal): return _error(request, 404, "resource_not_found")
    with request.app.state.identity_store.sync_session() as db:
        rows = db.scalars(select(OrganizationSpecialOfferApprover).where(OrganizationSpecialOfferApprover.organization_id == principal.organization_id).order_by(OrganizationSpecialOfferApprover.position)).all()
        return _response({"approver_ids": [str(row.approver_id) for row in rows], "version": _special_version(rows)}, etag=_special_version(rows))


@router.put("/api/v1/settings/offer-special-approvers")
def put_special_offer_approvers(payload: SpecialOfferApproversCommand, request: Request, if_match: str | None = Header(None, alias="If-Match"), idempotency_key: str | None = Header(None, alias="Idempotency-Key")):
    principal, expected, key = _principal(request), _version(request, if_match), _idempotency(request, idempotency_key)
    if any(isinstance(item, JSONResponse) for item in (principal, expected, key)): return next(item for item in (principal, expected, key) if isinstance(item, JSONResponse))
    if not _admin(principal): return _error(request, 404, "resource_not_found")
    with request.app.state.identity_store.sync_session() as db:
        try:
            def action():
                rows = db.scalars(select(OrganizationSpecialOfferApprover).where(OrganizationSpecialOfferApprover.organization_id == principal.organization_id).with_for_update()).all()
                if _special_version(rows) != expected: raise OfferVersionConflict
                if any(not is_eligible_offer_approver(db, principal.organization_id, user_id) for user_id in payload.approver_ids): raise ValueError
                for row in rows: db.delete(row)
                db.flush()
                for position, user_id in enumerate(payload.approver_ids, 1): db.add(OrganizationSpecialOfferApprover(organization_id=principal.organization_id, approver_id=user_id, position=position))
                db.flush()
                updated = db.scalars(select(OrganizationSpecialOfferApprover).where(OrganizationSpecialOfferApprover.organization_id == principal.organization_id).order_by(OrganizationSpecialOfferApprover.position)).all()
                value = _special_version(updated)
                _audit_setting(db, principal, request, "offer.special_approvers_updated", "offer_special_approvers", None, {"count": len(updated)})
                return 200, {"data": {"approver_ids": [str(row.approver_id) for row in updated], "version": value}}
            status, body = persisted_idempotent(db, principal.organization_id, principal.user_id, "offer.special_approvers.put", key, {"expected_version": expected, **payload.model_dump(mode="json")}, action)
            db.commit()
        except OfferVersionConflict:
            db.rollback(); return _error(request, 409, "resource_version_conflict")
        except IdempotencyConflict:
            db.rollback(); return _error(request, 409, "idempotency_conflict")
        except ValueError:
            db.rollback(); return _error(request, 422, "validation_failed")
    return _response(body["data"], status, etag=body["data"]["version"])
