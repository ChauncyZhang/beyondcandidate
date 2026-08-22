import hashlib
import re
from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Header, Query, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy import exists, or_, select

from server.app.identity.api import problem
from server.app.identity.models import AuditLog, Job, Organization, User, UserRole, UserStatus
from server.app.offers.models import Offer, OfferAccessToken, OfferApproval, OfferEvent, OfferResponse, OfferTemplate, OfferVersion, OrganizationSpecialOfferApprover
from server.app.offers.schemas import OfferApprovalDecision, OfferCommand, OfferDefaultApproverCommand, OfferTemplateCommand, OfferVersionCommand, ProxyOfferResponse, PublicOfferResponse, SpecialOfferApproversCommand
from server.app.offers.service import (
    OfferApprovalError,
    OfferNotFound,
    OfferVersionConflict,
    _audit,
    create_offer,
    decide_approval,
    submit_offer,
    update_offer_version,
    withdraw_offer,
    issue_offer_access_token,
    public_offer_access,
    record_proxy_offer_response,
    record_public_offer_response,
)
from server.app.recruiting.api import _idempotency, _principal
from server.app.recruiting.authorization import RecruitingAction, RecruitingAuthorizationService
from server.app.recruiting.models import Application, Candidate, JobJdVersion
from server.app.recruiting.service import IdempotencyConflict, persisted_idempotent
from server.app.recruiting.service import is_eligible_offer_approver
from server.app.communications.interview_messages import CandidateEmailUnavailable, resolve_confirmed_candidate_email
from server.app.communications.service import DeliveryCommand, EmailConfigurationUnavailable, SenderPolicy, enqueue_delivery
from server.app.notifications.service import create_user_notification
from server.app.onboarding.service import public_onboarding_prefill


router = APIRouter()
AUTH = RecruitingAuthorizationService()
ETAG = re.compile(r'^"(0|[1-9][0-9]*)"$')


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _offer_content_ready(content: dict | None) -> bool:
    return isinstance(content, dict) and all(
        isinstance(content.get(field), str) and content[field].strip()
        for field in ("title", "body", "compensation")
    )


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


def _offer(db, principal, offer_id, action=RecruitingAction.READ, *, lock=False):
    job_scope = AUTH.job_predicate(principal, action, Job)
    if action == RecruitingAction.READ:
        approval_scope = exists().where(
            OfferApproval.organization_id == Offer.organization_id,
            OfferApproval.offer_id == Offer.id,
            OfferApproval.assignee_id == principal.user_id,
        )
        job_scope = or_(job_scope, approval_scope)
    query = select(Offer).join(Job, (Job.organization_id == Offer.organization_id) & (Job.id == Offer.job_id)).where(
        Offer.organization_id == principal.organization_id, Offer.id == offer_id,
        job_scope,
    )
    return db.scalar(query.with_for_update() if lock else query)


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


def _offer_response_view(response):
    if response is None:
        return None
    return {
        "id": str(response.id), "offer_id": str(response.offer_id),
        "offer_version_id": str(response.offer_version_id) if response.offer_version_id else None,
        "version_number": response.version_number, "decision": response.status,
        "source": response.source, "actor_user_id": str(response.actor_user_id) if response.actor_user_id else None,
        "expected_start_date": response.expected_start_date.isoformat() if response.expected_start_date else None,
        "reason_text": response.reason_text, "communication_channel": response.communication_channel,
        "communicated_at": response.communicated_at.isoformat() if response.communicated_at else None,
        "note": response.note, "responded_at": response.responded_at.isoformat(),
    }


def _offer_view(db, offer, principal):
    current = db.scalar(select(OfferVersion).where(OfferVersion.organization_id == offer.organization_id, OfferVersion.id == offer.current_version_id))
    application = db.scalar(select(Application).where(Application.organization_id == offer.organization_id, Application.id == offer.application_id))
    candidate = db.scalar(select(Candidate).where(Candidate.organization_id == offer.organization_id, Candidate.id == application.candidate_id)) if application else None
    job = db.scalar(select(Job).where(Job.organization_id == offer.organization_id, Job.id == offer.job_id))
    can_manage = _can_manage(db, principal, offer)
    can_view_sensitive = can_manage or _is_approval_participant(db, principal, offer)
    pending_approval_id = db.scalar(select(OfferApproval.id).where(OfferApproval.organization_id == offer.organization_id, OfferApproval.offer_id == offer.id, OfferApproval.assignee_id == principal.user_id, OfferApproval.status == "pending"))
    is_assignee = pending_approval_id is not None
    response = db.scalar(select(OfferResponse).where(OfferResponse.organization_id == offer.organization_id, OfferResponse.offer_id == offer.id))
    content = current.content if current and can_view_sensitive else {"redacted": True}
    content_ready = bool(current and _offer_content_ready(current.content))
    send_queued = bool(current and offer.status == "ready_to_send" and db.scalar(select(OfferAccessToken.id).where(
        OfferAccessToken.organization_id == offer.organization_id,
        OfferAccessToken.offer_id == offer.id,
        OfferAccessToken.offer_version_id == current.id,
        OfferAccessToken.revoked_at.is_(None),
    )) is not None)
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
        "content_ready": content_ready, "send_queued": send_queued,
        "pending_approval_id": str(pending_approval_id) if pending_approval_id else None,
        "response": _offer_response_view(response),
        "allowed_actions": {
            "update": can_manage and offer.status in {"draft", "changes_requested", "ready_to_send", "sent"},
            "submit": can_manage and offer.status in {"draft", "changes_requested"},
            "withdraw": can_manage and offer.status not in {"withdrawn", "expired"},
            "send": can_manage and offer.status == "ready_to_send" and current is not None and content_ready and not send_queued,
            "decide": is_assignee and offer.status == "pending_approval",
            "proxy_response": can_manage and offer.status == "sent" and current is not None and current.id == offer.current_version_id,
        },
    }


def _queue_offer_response_confirmation(db, request, offer, response):
    application = db.scalar(select(Application).where(
        Application.organization_id == offer.organization_id,
        Application.id == offer.application_id,
    ))
    if application is None:
        raise OfferNotFound
    recipient = None
    try:
        recipient = resolve_confirmed_candidate_email(
            db, organization_id=offer.organization_id, candidate_id=application.candidate_id,
            contact_cipher=request.app.state.contact_cipher,
        )
        decision_text = "接受" if response.status == "accepted" else "拒绝"
        start_text = f"，预计到岗日期为 {response.expected_start_date.isoformat()}" if response.expected_start_date else ""
        enqueue_delivery(
            db,
            DeliveryCommand(
                organization_id=offer.organization_id, recipient=recipient,
                subject="Offer 确认结果", body=f"您好，系统已记录您{decision_text} Offer 的决定{start_text}。如有疑问，请联系负责 HR。",
                resource_type="offer_response", resource_id=response.id,
                idempotency_key=str(response.id), operation=f"offer.response_confirmation:{response.id}",
                created_by=application.owner_id, trace_id=request.state.trace_id,
            ),
            cipher=request.app.state.email_secret_cipher,
            sender_policy=SenderPolicy(request.app.state.settings.email_from_address, request.app.state.settings.email_from_name),
        )
    except (CandidateEmailUnavailable, EmailConfigurationUnavailable) as error:
        safe_code = "candidate_email_unconfirmed" if isinstance(error, CandidateEmailUnavailable) else "email_not_configured"
        masked = request.app.state.email_secret_cipher.mask_email(recipient) if recipient else ""
        create_user_notification(
            db, organization_id=offer.organization_id, user_id=application.owner_id,
            event_type="email_delivery_failed", resource_type="offer_response", resource_id=response.id,
            recipient_masked=masked, safe_error_code=safe_code,
        )
        db.add(AuditLog(
            organization_id=offer.organization_id, actor_user_id=response.actor_user_id,
            category="recruiting", event_type="offer.response_confirmation_email_unavailable", outcome="failure",
            resource_type="offer_response", resource_id=response.id, trace_id=request.state.trace_id,
            metadata_json={"safe_error_code": safe_code, "recipient": masked},
        ))


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
        except OfferApprovalError as error:
            db.rollback(); return _error(request, 409, error.code)
    return _response(body["data"], status, etag=body["data"].get("version"))


def _template_view(template):
    return {"id": str(template.id), "name": template.name, "content": template.content, "status": template.status, "version": template.version}


def _special_version(rows):
    canonical = ",".join(str(row.approver_id) for row in sorted(rows, key=lambda item: item.position))
    return int(hashlib.sha256(canonical.encode("ascii")).hexdigest()[:13], 16) if canonical else 0


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


@router.get("/api/v1/offers/{offer_id}/approver-options")
def list_offer_approver_options(offer_id: UUID, request: Request):
    principal = _principal(request)
    if isinstance(principal, JSONResponse):
        return principal
    with request.app.state.identity_store.sync_session() as db:
        offer = _offer(db, principal, offer_id)
        if offer is None or not _can_manage(db, principal, offer):
            return _error(request, 404, "resource_not_found")
        job = db.scalar(select(Job).where(
            Job.organization_id == principal.organization_id,
            Job.id == offer.job_id,
        ))
        if job is None:
            return _error(request, 404, "resource_not_found")
        rows = db.execute(select(User.id, User.display_name).where(
            User.organization_id == principal.organization_id,
            User.status == UserStatus.ACTIVE,
            exists().where(
                UserRole.user_id == User.id,
                UserRole.role.in_(("recruiting_admin", "hiring_manager")),
            ),
        ).order_by(User.display_name.asc(), User.id.asc())).all()
        return _response(
            [{"id": str(user_id), "name": display_name} for user_id, display_name in rows],
            meta={"count": len(rows), "job_version": job.version},
            etag=job.version,
        )


@router.put("/api/v1/offers/{offer_id}/default-approver")
def set_offer_default_approver(
    offer_id: UUID,
    payload: OfferDefaultApproverCommand,
    request: Request,
    if_match: str | None = Header(None, alias="If-Match"),
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
):
    principal, expected_job_version, key = _principal(request), _version(request, if_match), _idempotency(request, idempotency_key)
    if any(isinstance(item, JSONResponse) for item in (principal, expected_job_version, key)):
        return next(item for item in (principal, expected_job_version, key) if isinstance(item, JSONResponse))

    def action(db):
        def configure():
            offer = _offer(db, principal, offer_id)
            if offer is None or not _can_manage(db, principal, offer):
                raise OfferNotFound
            job = db.scalar(select(Job).where(
                Job.organization_id == principal.organization_id,
                Job.id == offer.job_id,
            ).with_for_update())
            if job is None:
                raise OfferNotFound
            if job.version != expected_job_version:
                raise OfferApprovalError("job changed while configuring offer approver", code="job_version_conflict")
            offer = _offer(db, principal, offer_id, lock=True)
            if offer is None or not _can_manage(db, principal, offer):
                raise OfferNotFound
            if offer.version != payload.offer_version:
                raise OfferVersionConflict
            if offer.status not in {"draft", "changes_requested"}:
                raise OfferApprovalError("offer is not editable")
            if not is_eligible_offer_approver(db, principal.organization_id, payload.approver_id):
                raise OfferApprovalError("job default approver is not eligible", code="offer_approver_ineligible")
            if job.offer_approver_id != payload.approver_id:
                previous_id = job.offer_approver_id
                job.offer_approver_id = payload.approver_id
                job.version += 1
                db.add(AuditLog(
                    organization_id=principal.organization_id,
                    actor_user_id=principal.user_id,
                    category="recruiting",
                    event_type="job.offer_approver_updated",
                    outcome="success",
                    resource_type="job",
                    resource_id=job.id,
                    trace_id=request.state.trace_id,
                    metadata_json={
                        "previous_approver_id": str(previous_id) if previous_id else None,
                        "approver_id": str(payload.approver_id),
                        "source": "offer_submission",
                    },
                ))
            db.flush()
            return 200, {"data": {
                "offer_id": str(offer.id),
                "job_id": str(job.id),
                "approver_id": str(job.offer_approver_id),
                "job_version": job.version,
                "offer_version": offer.version,
                "version": job.version,
            }}
        return configure

    return _run_mutation(
        request,
        principal,
        f"offer.default_approver:{offer_id}",
        key,
        {
            "expected_job_version": expected_job_version,
            "offer_version": payload.offer_version,
            "approver_id": str(payload.approver_id),
        },
        action,
    )


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


@router.post("/api/v1/offers/{offer_id}/proxy-responses")
def respond_to_offer_by_proxy(offer_id: UUID, payload: ProxyOfferResponse, request: Request, if_match: str | None = Header(None, alias="If-Match"), idempotency_key: str | None = Header(None, alias="Idempotency-Key")):
    principal, expected, key = _principal(request), _version(request, if_match), _idempotency(request, idempotency_key)
    if any(isinstance(item, JSONResponse) for item in (principal, expected, key)):
        return next(item for item in (principal, expected, key) if isinstance(item, JSONResponse))
    semantic = {"expected_version": expected, **payload.model_dump(mode="json")}
    with request.app.state.identity_store.sync_session() as db:
        try:
            def action():
                offer = _offer(db, principal, offer_id, lock=True)
                if offer is None or not _can_manage(db, principal, offer):
                    raise OfferNotFound
                if offer.version != expected:
                    raise OfferVersionConflict
                response, duplicate = record_proxy_offer_response(
                    db, offer, payload, actor_user_id=principal.user_id,
                    onboarding_cipher=request.app.state.onboarding_pii_cipher,
                    now=datetime.now(timezone.utc), trace_id=request.state.trace_id,
                )
                if not duplicate:
                    _queue_offer_response_confirmation(db, request, offer, response)
                return 200, {"data": _offer_view(db, offer, principal)}
            status, body = persisted_idempotent(
                db, principal.organization_id, principal.user_id,
                f"offer.proxy_response:{offer_id}", key, semantic, action,
            )
            db.commit()
        except OfferNotFound:
            db.rollback(); return _error(request, 404, "resource_not_found")
        except OfferVersionConflict:
            db.rollback(); return _error(request, 409, "resource_version_conflict")
        except IdempotencyConflict:
            db.rollback(); return _error(request, 409, "idempotency_conflict")
    return _response(body["data"], status, etag=body["data"]["version"])


@router.get("/api/v1/offers/{offer_id}/history")
def offer_history(offer_id: UUID, request: Request):
    principal = _principal(request)
    if isinstance(principal, JSONResponse): return principal
    with request.app.state.identity_store.sync_session() as db:
        offer = _offer(db, principal, offer_id)
        if offer is None: return _error(request, 404, "resource_not_found")
        versions = db.scalars(select(OfferVersion).where(OfferVersion.organization_id == principal.organization_id, OfferVersion.offer_id == offer_id).order_by(OfferVersion.version_number)).all()
        approvals = db.scalars(select(OfferApproval).where(OfferApproval.organization_id == principal.organization_id, OfferApproval.offer_id == offer_id).order_by(OfferApproval.round_number, OfferApproval.sequence)).all()
        responses = db.scalars(select(OfferResponse).where(OfferResponse.organization_id == principal.organization_id, OfferResponse.offer_id == offer_id).order_by(OfferResponse.responded_at, OfferResponse.id)).all()
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
            "responses": [_offer_response_view(response) for response in responses],
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
                offer = _offer(db, principal, offer_id, lock=True)
                if offer is None or not _can_manage(db, principal, offer): raise OfferNotFound
                if offer.version != expected: raise OfferVersionConflict
                current = db.scalar(select(OfferVersion).where(OfferVersion.organization_id == offer.organization_id, OfferVersion.id == offer.current_version_id).with_for_update())
                now = datetime.now(timezone.utc)
                if offer.status != "ready_to_send" or current is None or not _offer_content_ready(current.content) or _utc(current.candidate_response_deadline) <= now:
                    raise OfferApprovalError
                if request.app.state.settings.offer_public_base_url is None:
                    raise RuntimeError("offer public base URL is not configured")
                application = db.scalar(select(Application).where(Application.organization_id == offer.organization_id, Application.id == offer.application_id).with_for_update())
                if application is None:
                    raise OfferNotFound
                candidate = db.scalar(select(Candidate).where(Candidate.organization_id == offer.organization_id, Candidate.id == application.candidate_id))
                job = db.scalar(select(Job).where(Job.organization_id == offer.organization_id, Job.id == offer.job_id))
                organization = db.scalar(select(Organization).where(Organization.id == offer.organization_id))
                if candidate is None or job is None:
                    raise OfferNotFound
                active_token = db.scalar(select(OfferAccessToken.id).where(
                    OfferAccessToken.organization_id == offer.organization_id,
                    OfferAccessToken.offer_id == offer.id,
                    OfferAccessToken.offer_version_id == current.id,
                    OfferAccessToken.revoked_at.is_(None),
                ))
                if active_token is not None:
                    raise OfferApprovalError("offer send is already queued")
                recipient = resolve_confirmed_candidate_email(db, organization_id=offer.organization_id, candidate_id=application.candidate_id, contact_cipher=request.app.state.contact_cipher)
                token, _ = issue_offer_access_token(db, offer.organization_id, offer, current, codec=request.app.state.offer_token_codec, now=now)
                # The worker reconstructs the capability from token row ID.  Delivery storage holds no link or raw token.
                brand_name = organization.name if organization else request.app.state.settings.email_from_name
                deadline = _utc(current.candidate_response_deadline).strftime("%Y-%m-%d %H:%M UTC")
                message_body = (
                    f"{candidate.display_name}，您好：\n\n"
                    f"感谢您参与 {brand_name} 的招聘流程。经过综合评估，我们诚挚邀请您加入，担任 {job.title}。\n\n"
                    f"回复截止：{deadline}\n\n"
                    "请点击以下链接查看并确认 Offer：\n{{offer_public_link}}\n\n"
                    "如对录用内容或入职安排有疑问，请联系招聘负责人。"
                )
                enqueue_delivery(db, DeliveryCommand(organization_id=offer.organization_id, recipient=recipient, subject=f"{brand_name} 录用通知｜{job.title}", body=message_body, resource_type="offer_access_token", resource_id=token.id, idempotency_key=key, operation=f"offer.send:{offer.id}", created_by=principal.user_id, trace_id=request.state.trace_id), cipher=request.app.state.email_secret_cipher, sender_policy=SenderPolicy(request.app.state.settings.email_from_address, request.app.state.settings.email_from_name))
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
        except RuntimeError:
            db.rollback(); return _error(request, 409, "offer_send_unavailable")
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
            access, offer, version, application = public_offer_access(db, token, codec=request.app.state.offer_token_codec, now=datetime.now(timezone.utc), allow_inactive=True)
        except (OfferNotFound, ValueError):
            return _public_error(request)
        candidate = db.get(Candidate, application.candidate_id); job = db.get(Job, offer.job_id); organization = db.get(Organization, offer.organization_id)
        hr = db.get(User, application.owner_id)
        jd = db.scalar(select(JobJdVersion).where(
            JobJdVersion.organization_id == offer.organization_id,
            JobJdVersion.job_id == offer.job_id,
        ).order_by(JobJdVersion.version_number.desc(), JobJdVersion.id.desc()).limit(1))
        expiry = access.expires_at.replace(tzinfo=timezone.utc) if access.expires_at.tzinfo is None else access.expires_at
        if offer.status in {"accepted", "declined", "withdrawn"}: status = offer.status
        elif offer.current_version_id != version.id or access.revoked_at is not None: status = "superseded"
        elif expiry <= datetime.now(timezone.utc): status = "expired"
        elif offer.status == "sent" and access.delivered_at is not None: status = "sent"
        else: return _public_error(request)
        location = jd.content.get("location") if jd and isinstance(jd.content, dict) else None
        hr_contact = " · ".join(value for value in (hr.display_name if hr else None, hr.email if hr else None) if value)
        pdf_available = status == "sent" and all(getattr(version, field) is not None for field in ("pdf_object_key", "pdf_sha256", "pdf_size_bytes", "pdf_rendered_at"))
        onboarding_prefill = public_onboarding_prefill(
            db,
            offer.organization_id,
            application,
            contact_cipher=request.app.state.contact_cipher,
        ) if status == "sent" else None
        return _public_response({"status": status, "company_name": organization.name if organization else None, "candidate_name": candidate.display_name if candidate else None, "job_title": job.title if job else None, "location": location, "hr_contact": hr_contact or None, "content": version.content, "candidate_response_deadline": version.candidate_response_deadline.isoformat(), "pdf_available": pdf_available, "onboarding_prefill": onboarding_prefill})


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
            response, duplicate = record_public_offer_response(
                db,
                token,
                payload,
                codec=request.app.state.offer_token_codec,
                onboarding_cipher=request.app.state.onboarding_pii_cipher,
                now=datetime.now(timezone.utc),
                trace_id=request.state.trace_id,
            )
            if not duplicate:
                offer = db.scalar(select(Offer).where(Offer.organization_id == response.organization_id, Offer.id == response.offer_id))
                if offer is None:
                    raise OfferNotFound
                _queue_offer_response_confirmation(db, request, offer, response)
            db.commit()
        except OfferVersionConflict:
            db.rollback(); return _public_response({"code": "offer_response_conflict"}, 409)
        except OfferApprovalError as error:
            db.rollback(); return _public_response({"code": error.code}, 409)
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
