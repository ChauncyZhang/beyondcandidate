import uuid
from datetime import datetime, timezone

from sqlalchemy import func, select

from server.app.identity.models import AuditLog, Job
from server.app.recruiting.models import Application
from server.app.recruiting.service import ResourceVersionConflict
from server.app.offers.models import Offer, OfferApproval, OfferEvent, OfferResponse, OfferTemplate, OfferVersion, OrganizationSpecialOfferApprover


class OfferNotFound(Exception):
    pass


class OfferVersionConflict(ResourceVersionConflict):
    pass


class OfferApprovalError(Exception):
    pass


FINAL_OFFER_STATUSES = {"withdrawn", "expired"}
EDITABLE_OFFER_STATUSES = {"draft", "changes_requested"}


def _audit(db, offer, actor_user_id, event_type, trace_id, payload):
    safe_payload = {key: str(value) if hasattr(value, "hex") else value for key, value in payload.items()}
    db.add(OfferEvent(organization_id=offer.organization_id, offer_id=offer.id, actor_user_id=actor_user_id, event_type=event_type, payload=safe_payload))
    db.add(AuditLog(organization_id=offer.organization_id, actor_user_id=actor_user_id, event_type=event_type, outcome="success", resource_type="offer", resource_id=offer.id, trace_id=trace_id, metadata_json={key: value for key, value in safe_payload.items() if key != "reason"}))


def _offer(db, organization_id, offer_id, *, lock=True):
    statement = select(Offer).where(Offer.organization_id == organization_id, Offer.id == offer_id)
    offer = db.scalar(statement.with_for_update() if lock else statement)
    if offer is None:
        raise OfferNotFound
    return offer


def _require_version(offer, expected_version):
    if expected_version != offer.version:
        raise OfferVersionConflict


def _current_version(db, offer):
    version = db.scalar(select(OfferVersion).where(OfferVersion.organization_id == offer.organization_id, OfferVersion.offer_id == offer.id, OfferVersion.id == offer.current_version_id).with_for_update())
    if version is None:
        raise OfferApprovalError("offer has no current version")
    return version


def create_offer(db, organization_id, actor_user_id, command, *, trace_id):
    application = db.scalar(select(Application).where(Application.organization_id == organization_id, Application.id == command.application_id))
    if application is None:
        raise OfferNotFound
    job = db.scalar(select(Job).where(Job.organization_id == organization_id, Job.id == application.job_id))
    if job is None:
        raise OfferNotFound
    template_id = command.template_id if command.template_id is not None else job.offer_template_id
    if template_id is not None and db.scalar(select(OfferTemplate.id).where(OfferTemplate.organization_id == organization_id, OfferTemplate.id == template_id)) is None:
        raise OfferNotFound
    offer_id, version_id = uuid.uuid4(), uuid.uuid4()
    offer = Offer(id=offer_id, organization_id=organization_id, application_id=application.id, job_id=job.id, template_id=template_id, current_version_id=version_id, candidate_response_deadline=command.candidate_response_deadline, is_special=command.is_special, special_reason=command.special_reason)
    version = OfferVersion(id=version_id, organization_id=organization_id, offer_id=offer_id, version_number=1, content=command.content, template_id=template_id, candidate_response_deadline=command.candidate_response_deadline, is_special=command.is_special, special_reason=command.special_reason, created_by=actor_user_id)
    db.add_all([offer, version])
    db.add(OfferResponse(organization_id=organization_id, offer_id=offer.id, status="pending"))
    _audit(db, offer, actor_user_id, "offer.created", trace_id, {"application_id": application.id, "version_number": 1})
    db.flush()
    return offer


def update_offer_version(db, organization_id, offer_id, actor_user_id, command, *, expected_version, trace_id):
    offer = _offer(db, organization_id, offer_id)
    _require_version(offer, expected_version)
    if offer.status not in EDITABLE_OFFER_STATUSES:
        raise OfferVersionConflict
    current = _current_version(db, offer)
    next_number = current.version_number + 1
    next_version = OfferVersion(id=uuid.uuid4(), organization_id=organization_id, offer_id=offer.id, version_number=next_number, content=command.content, template_id=offer.template_id, candidate_response_deadline=offer.candidate_response_deadline, is_special=offer.is_special, special_reason=offer.special_reason, created_by=actor_user_id)
    db.add(next_version)
    offer.current_version_id = next_version.id
    offer.version += 1
    _audit(db, offer, actor_user_id, "offer.version_created", trace_id, {"version_number": next_number})
    db.flush()
    return offer


def submit_offer(db, organization_id, offer_id, actor_user_id, *, expected_version, trace_id):
    offer = _offer(db, organization_id, offer_id)
    _require_version(offer, expected_version)
    if offer.status not in EDITABLE_OFFER_STATUSES:
        raise OfferApprovalError("offer is not editable")
    job = db.scalar(select(Job).where(Job.organization_id == organization_id, Job.id == offer.job_id))
    if job is None or job.offer_approver_id is None:
        raise OfferApprovalError("job default approver is required")
    current = _current_version(db, offer)
    assignees = [job.offer_approver_id]
    if offer.is_special:
        assignees.extend(db.scalars(select(OrganizationSpecialOfferApprover.approver_id).where(OrganizationSpecialOfferApprover.organization_id == organization_id).order_by(OrganizationSpecialOfferApprover.position)))
    deduplicated = list(dict.fromkeys(assignees))
    round_number = (db.scalar(select(func.coalesce(func.max(OfferApproval.round_number), 0)).where(OfferApproval.organization_id == organization_id, OfferApproval.offer_id == offer.id)) or 0) + 1
    for sequence, assignee_id in enumerate(deduplicated, start=1):
        db.add(OfferApproval(organization_id=organization_id, offer_id=offer.id, offer_version_id=current.id, round_number=round_number, version_number=current.version_number, sequence=sequence, assignee_id=assignee_id, status="pending" if sequence == 1 else "waiting"))
    offer.status = "pending_approval"
    offer.version += 1
    _audit(db, offer, actor_user_id, "offer.submitted", trace_id, {"round_number": round_number, "version_number": current.version_number, "approver_count": len(deduplicated)})
    db.flush()
    return offer


def decide_approval(db, organization_id, offer_id, actor_user_id, decision, *, expected_version, reason=None, trace_id):
    offer = _offer(db, organization_id, offer_id)
    _require_version(offer, expected_version)
    if offer.status != "pending_approval" or decision not in {"approved", "rejected"}:
        raise OfferApprovalError
    approval = db.scalar(select(OfferApproval).where(OfferApproval.organization_id == organization_id, OfferApproval.offer_id == offer.id, OfferApproval.assignee_id == actor_user_id, OfferApproval.status == "pending").order_by(OfferApproval.round_number.desc(), OfferApproval.sequence))
    if approval is None:
        raise OfferApprovalError("approval is not pending for actor")
    if decision == "rejected" and not (reason and reason.strip()):
        raise OfferApprovalError("rejection reason is required")
    approval.status = decision
    approval.reason = reason.strip() if reason else None
    approval.decided_at = datetime.now(timezone.utc)
    if decision == "rejected":
        offer.status = "changes_requested"
        event_type = "offer.approval_rejected"
        payload = {"round_number": approval.round_number, "sequence": approval.sequence, "reason": approval.reason}
    else:
        next_approval = db.scalar(select(OfferApproval).where(OfferApproval.organization_id == organization_id, OfferApproval.offer_id == offer.id, OfferApproval.round_number == approval.round_number, OfferApproval.sequence == approval.sequence + 1))
        if next_approval is None:
            offer.status = "ready_to_send"
        else:
            next_approval.status = "pending"
        event_type = "offer.approval_approved"
        payload = {"round_number": approval.round_number, "sequence": approval.sequence}
    offer.version += 1
    _audit(db, offer, actor_user_id, event_type, trace_id, payload)
    db.flush()
    return offer


def withdraw_offer(db, organization_id, offer_id, actor_user_id, *, trace_id, expected_version):
    offer = _offer(db, organization_id, offer_id)
    _require_version(offer, expected_version)
    if offer.status in FINAL_OFFER_STATUSES:
        raise OfferApprovalError("offer is final")
    offer.status = "withdrawn"
    offer.version += 1
    _audit(db, offer, actor_user_id, "offer.withdrawn", trace_id, {})
    db.flush()
    return offer


def expire_due_offers(db, *, now, trace_id):
    due_query = select(Offer).join(OfferResponse, (OfferResponse.organization_id == Offer.organization_id) & (OfferResponse.offer_id == Offer.id)).where(Offer.status == "sent", Offer.candidate_response_deadline <= now, OfferResponse.status == "pending")
    due = list(db.scalars(due_query.with_for_update(skip_locked=db.get_bind().dialect.name == "postgresql")))
    for offer in due:
        offer.status = "expired"
        offer.version += 1
        _audit(db, offer, None, "offer.expired", trace_id, {"deadline": offer.candidate_response_deadline.isoformat()})
    db.flush()
    return len(due)
