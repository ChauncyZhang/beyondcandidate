import hashlib
import re
from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Header, Query, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy import exists, or_, select

from server.app.identity.api import problem
from server.app.identity.models import AuditLog, Job
from server.app.offers.models import Offer, OfferAccessToken, OfferApproval, OfferEvent, OfferTemplate, OfferVersion, OrganizationSpecialOfferApprover
from server.app.offers.schemas import OfferApprovalDecision, OfferCommand, OfferTemplateCommand, OfferVersionCommand, PublicOfferResponse, SpecialOfferApproversCommand
from server.app.offers.service import (
    OfferApprovalError,
    OfferNotFound,
    OfferVersionConflict,
    create_offer,
    decide_approval,
    submit_offer,
    update_offer_version,
    withdraw_offer,
    issue_offer_access_token,
    public_offer_access,
    record_public_offer_response,
)
from server.app.recruiting.api import _idempotency, _principal
from server.app.recruiting.authorization import RecruitingAction, RecruitingAuthorizationService
from server.app.recruiting.models import Application, Candidate
from server.app.recruiting.service import IdempotencyConflict, persisted_idempotent
from server.app.recruiting.service import is_eligible_offer_approver
from server.app.communications.interview_messages import CandidateEmailUnavailable, resolve_confirmed_candidate_email
from server.app.communications.service import DeliveryCommand, SenderPolicy, enqueue_delivery


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


def _can_use_offer_templates(principal):
    return principal.active and bool(principal.roles.intersection({"recruiting_admin", "recruiter"}))


def _application(db, principal, application_id, action=RecruitingAction.READ):
    return db.scalar(select(Application).join(Job, (Job.organization_id == Application.organization_id) & (Job.id == Application.job_id)).where(
        Application.organization_id == principal.organization_id,
        Application.id == application_id,
        AUTH.job_predicate(principal, action, Job),
    ))


def _offer(db, principal, offer_id, action=RecruitingAction.READ):
    job_scope = AUTH.job_predicate(principal, action, Job)
    if action == RecruitingAction.READ:
        approval_scope = exists().where(
            OfferApproval.organization_id == Offer.organization_id,
            OfferApproval.offer_id == Offer.id,
            OfferApproval.assignee_id == principal.user_id,
        )
        job_scope = or_(job_scope, approval_scope)
    return db.scalar(select(Offer).join(Job, (Job.organization_id == Offer.organization_id) & (Job.id == Offer.job_id)).where(
        Offer.organization_id == principal.organization_id, Offer.id == offer_id,
        job_scope,
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


def _is_approval_participant(db, principal, offer):
    return db.scalar(select(OfferApproval.id).where(
        OfferApproval.organization_id == offer.organization_id,
        OfferApproval.offer_id == offer.id,
        OfferApproval.assignee_id == principal.user_id,
    ).limit(1)) is not None


def _offer_view(db, offer, principal):
    current = db.scalar(select(OfferVersion).where(OfferVersion.organization_id == offer.organization_id, OfferVersion.id == offer.current_version_id))
    application = db.scalar(select(Application).where(Application.organization_id == offer.organization_id, Application.id == offer.application_id))
    candidate = db.scalar(select(Candidate).where(Candidate.organization_id == offer.organization_id, Candidate.id == application.candidate_id)) if application else None
    job = db.scalar(select(Job).where(Job.organization_id == offer.organization_id, Job.id == offer.job_id))
    can_manage = _can_manage(db, principal, offer)
    can_view_sensitive = can_manage or _is_approval_participant(db, principal, offer)
    is_assignee = db.scalar(select(OfferApproval.id).where(OfferApproval.organization_id == offer.organization_id, OfferApproval.offer_id == offer.id, OfferApproval.assignee_id == principal.user_id, OfferApproval.status == "pending")) is not None
    content = current.content if current and can_view_sensitive else {"redacted": True}
    return {
        "id": str(offer.id), "application_id": str(offer.application_id), "job_id": str(offer.job_id),
        "candidate_id": str(candidate.id) if candidate else None,
        "candidate_name": candidate.display_name if candidate else None,
        "job_title": job.title if job else None,
        "template_id": str(current.template_id) if current and current.template_id else None,
        "status": offer.status, "version": offer.version, "current_version_id": str(offer.current_version_id),
        "current_version_number": current.version_number if current else None,
        "candidate_response_deadline": offer.candidate_response_deadline.isoformat(), "is_special": offer.is_special,
        "special_reason": offer.special_reason if can_view_sensitive else None, "content": content,
        "can_view_sensitive_content": can_view_sensitive,
        "pdf_ready": bool(current and current.pdf_object_key),
        "allowed_actions": {
            "update": can_manage and offer.status in {"draft", "changes_requested", "ready_to_send", "sent"},
            "submit": can_manage and offer.status in {"draft", "changes_requested"},
            "withdraw": can_manage and offer.status not in {"withdrawn", "expired"},
            "send": False,
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
    canonical = ",".join(str(row.approver_id) for row in sorted(rows, key=lambda item: item.position))
    return int(hashlib.sha256(canonical.encode("ascii")).hexdigest()[:15], 16) if canonical else 0


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
        versions = db.scalars(select(OfferVersion).where(OfferVersion.organization_id == principal.organization_id, OfferVersion.offer_id == offer_id).order_by(OfferVersion.version_number)).all()
        approvals = db.scalars(select(OfferApproval).where(OfferApproval.organization_id == principal.organization_id, OfferApproval.offer_id == offer_id).order_by(OfferApproval.round_number, OfferApproval.sequence)).all()
        events = db.scalars(select(OfferEvent).where(OfferEvent.organization_id == principal.organization_id, OfferEvent.offer_id == offer_id).order_by(OfferEvent.created_at, OfferEvent.id)).all()
        can_view_sensitive = _can_manage(db, principal, offer) or _is_approval_participant(db, principal, offer)
        return _response({
            "versions": [{
                "id": str(version.id),
                "version_number": version.version_number,
                "content": version.content if can_view_sensitive else {"redacted": True},
                "template_id": str(version.template_id) if version.template_id else None,
                "candidate_response_deadline": version.candidate_response_deadline.isoformat(),
                "is_special": version.is_special,
                "special_reason": version.special_reason if can_view_sensitive else None,
                "submitted_at": version.submitted_at.isoformat() if version.submitted_at else None,
                "pdf_ready": bool(version.pdf_object_key),
                "created_at": version.created_at.isoformat(),
            } for version in versions],
            "approvals": [{
                "id": str(approval.id),
                "version_number": approval.version_number,
                "round_number": approval.round_number,
                "sequence": approval.sequence,
                "assignee_id": str(approval.assignee_id),
                "status": approval.status,
                "reason": approval.reason if can_view_sensitive else None,
                "decided_at": approval.decided_at.isoformat() if approval.decided_at else None,
            } for approval in approvals] if can_view_sensitive else [],
            "events": [{
                "id": str(event.id),
                "event_type": event.event_type,
                "created_at": event.created_at.isoformat(),
                "payload": event.payload if can_view_sensitive else {},
            } for event in events],
        })


@router.get("/api/v1/offer-approvals/pending")
def pending_approvals(request: Request):
    principal = _principal(request)
    if isinstance(principal, JSONResponse): return principal
    with request.app.state.identity_store.sync_session() as db:
        rows = db.execute(
            select(OfferApproval, Offer, Application, Job, Candidate)
            .join(Offer, (Offer.organization_id == OfferApproval.organization_id) & (Offer.id == OfferApproval.offer_id))
            .join(Application, (Application.organization_id == Offer.organization_id) & (Application.id == Offer.application_id))
            .join(Job, (Job.organization_id == Offer.organization_id) & (Job.id == Offer.job_id))
            .join(Candidate, (Candidate.organization_id == Application.organization_id) & (Candidate.id == Application.candidate_id))
            .where(
                OfferApproval.organization_id == principal.organization_id,
                OfferApproval.assignee_id == principal.user_id,
                OfferApproval.status == "pending",
            )
            .order_by(OfferApproval.created_at)
        ).all()
        return _response([{
            "id": str(approval.id),
            "offer_id": str(offer.id),
            "application_id": str(application.id),
            "candidate_id": str(candidate.id),
            "candidate_name": candidate.display_name,
            "job_id": str(job.id),
            "job_title": job.title,
            "offer_status": offer.status,
            "offer_version": offer.version,
            "candidate_response_deadline": offer.candidate_response_deadline.isoformat(),
            "sequence": approval.sequence,
            "round_number": approval.round_number,
            "version_number": approval.version_number,
        } for approval, offer, application, job, candidate in rows])


@router.post("/api/v1/offers/{offer_id}/send")
def send_offer(offer_id: UUID, request: Request, if_match: str | None = Header(None, alias="If-Match"), idempotency_key: str | None = Header(None, alias="Idempotency-Key")):
    principal, expected, key = _principal(request), _version(request, if_match), _idempotency(request, idempotency_key)
    if any(isinstance(item, JSONResponse) for item in (principal, expected, key)): return next(item for item in (principal, expected, key) if isinstance(item, JSONResponse))
    with request.app.state.identity_store.sync_session() as db:
        try:
            def action():
                offer = _offer(db, principal, offer_id)
                if offer is None or not _can_manage(db, principal, offer): raise OfferNotFound
                if offer.version != expected: raise OfferVersionConflict
                current = db.scalar(select(OfferVersion).where(OfferVersion.organization_id == offer.organization_id, OfferVersion.id == offer.current_version_id).with_for_update())
                now = datetime.now(timezone.utc)
                if offer.status != "ready_to_send" or current is None or any(getattr(current, field) is None for field in ("pdf_object_key", "pdf_sha256", "pdf_size_bytes", "pdf_rendered_at")) or current.candidate_response_deadline <= now:
                    raise OfferApprovalError
                application = db.scalar(select(Application).where(Application.organization_id == offer.organization_id, Application.id == offer.application_id).with_for_update())
                recipient = resolve_confirmed_candidate_email(db, organization_id=offer.organization_id, candidate_id=application.candidate_id, contact_cipher=request.app.state.contact_cipher)
                token, _ = issue_offer_access_token(db, offer.organization_id, offer, current, codec=request.app.state.offer_token_codec, now=now)
                # The worker reconstructs the capability from token row ID.  Delivery storage holds no link or raw token.
                enqueue_delivery(db, DeliveryCommand(organization_id=offer.organization_id, recipient=recipient, subject="Your offer is ready", body="Your secure offer link: {{offer_public_link}}", resource_type="offer_access_token", resource_id=token.id, idempotency_key=key, operation=f"offer.send:{offer.id}", created_by=principal.user_id, trace_id=request.state.trace_id), cipher=request.app.state.email_secret_cipher, sender_policy=SenderPolicy(request.app.state.settings.email_from_address, request.app.state.settings.email_from_name))
                _audit(db, offer, principal.user_id, "offer.send_queued", request.state.trace_id, {"version_number": current.version_number})
                return 202, {"data": _offer_view(db, offer, principal)}
            status, body = persisted_idempotent(db, principal.organization_id, principal.user_id, f"offer.send:{offer_id}", key, {"expected_version": expected}, action)
            db.commit()
        except CandidateEmailUnavailable:
            db.rollback(); return _error(request, 409, "candidate_email_unconfirmed")
        except OfferNotFound:
            db.rollback(); return _error(request, 404, "resource_not_found")
        except OfferVersionConflict:
            db.rollback(); return _error(request, 409, "resource_version_conflict")
        except OfferApprovalError:
            db.rollback(); return _error(request, 409, "offer_not_ready_to_send")
        except IdempotencyConflict:
            db.rollback(); return _error(request, 409, "idempotency_conflict")
    return _response(body["data"], status, etag=body["data"]["version"])


def _public_response(data, status=200):
    response = JSONResponse({"data": data}, status_code=status)
    response.headers.update({"Cache-Control": "no-store", "Referrer-Policy": "no-referrer", "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"})
    return response


def _public_error(request):
    response = problem(request, 404, "offer_link_invalid", "The offer link is invalid or unavailable.")
    response.headers.update({"Cache-Control": "no-store", "Referrer-Policy": "no-referrer", "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"})
    return response


@router.get("/api/public/v1/offers/{token}")
def get_public_offer(token: str, request: Request):
    with request.app.state.identity_store.sync_session() as db:
        try:
            _, offer, version, _ = public_offer_access(db, token, codec=request.app.state.offer_token_codec, now=datetime.now(timezone.utc))
        except (OfferNotFound, ValueError):
            return _public_error(request)
        return _public_response({"status": offer.status, "content": version.content, "candidate_response_deadline": version.candidate_response_deadline.isoformat(), "pdf_available": True})


@router.get("/api/public/v1/offers/{token}/pdf")
def get_public_offer_pdf(token: str, request: Request):
    with request.app.state.identity_store.sync_session() as db:
        try:
            _, _, version, _ = public_offer_access(db, token, codec=request.app.state.offer_token_codec, now=datetime.now(timezone.utc))
            expected_key = f"offers/{version.organization_id}/offers/{version.offer_id}/versions/{version.id}.pdf"
            if version.pdf_object_key != expected_key or not version.pdf_sha256 or request.app.state.offer_pdf_storage is None:
                raise OfferNotFound
            content = request.app.state.offer_pdf_storage.read_verified(version.pdf_object_key, version.pdf_sha256)
        except Exception:
            return _public_error(request)
    response = Response(content, media_type="application/pdf")
    response.headers.update({"Cache-Control": "no-store", "Referrer-Policy": "no-referrer", "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'; base-uri 'none'", "Content-Disposition": "inline; filename=offer.pdf"})
    return response


@router.post("/api/public/v1/offers/{token}/responses")
def respond_public_offer(token: str, payload: PublicOfferResponse, request: Request):
    if request.headers.get("origin") not in request.app.state.settings.cors_origins:
        return _public_error(request)
    with request.app.state.identity_store.sync_session() as db:
        try:
            response, duplicate = record_public_offer_response(db, token, payload, codec=request.app.state.offer_token_codec, now=datetime.now(timezone.utc), trace_id=request.state.trace_id)
            db.commit()
        except OfferVersionConflict:
            db.rollback(); return _public_response({"code": "offer_response_conflict"}, 409)
        except (OfferNotFound, ValueError):
            db.rollback(); return _public_error(request)
    return _public_response({"status": response.status, "duplicate": duplicate})


@router.get("/api/v1/offer-templates")
def list_offer_templates(request: Request):
    principal = _principal(request)
    if isinstance(principal, JSONResponse): return principal
    if not _can_use_offer_templates(principal): return _error(request, 404, "resource_not_found")
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
