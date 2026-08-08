import uuid
from datetime import datetime, timezone

from sqlalchemy import exists, func, select

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
ACTIVE_OFFER_STATUSES = {"draft", "pending_approval", "changes_requested", "ready_to_send", "sent"}
REVISION_SOURCE_STATUSES = {"draft", "changes_requested", "ready_to_send", "sent"}


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


def _require_active_template(db, organization_id, template_id):
    if template_id is None:
        return
    template = db.scalar(select(OfferTemplate).where(
        OfferTemplate.organization_id == organization_id,
        OfferTemplate.id == template_id,
    ))
    if template is None:
        raise OfferNotFound
    if template.status != "active":
        raise OfferApprovalError("offer template must be active")


def create_offer(db, organization_id, actor_user_id, command, *, trace_id):
    application = db.scalar(select(Application).where(
        Application.organization_id == organization_id,
        Application.id == command.application_id,
    ).with_for_update())
    if application is None:
        raise OfferNotFound
    if application.stage != "passed":
        raise OfferApprovalError("application must be passed")
    if db.scalar(select(exists().where(
        Offer.organization_id == organization_id,
        Offer.application_id == application.id,
        Offer.status.in_(ACTIVE_OFFER_STATUSES),
    ))):
        raise OfferApprovalError("application already has an active workflow")
    job = db.scalar(select(Job).where(Job.organization_id == organization_id, Job.id == application.job_id))
    if job is None:
        raise OfferNotFound
    template_id = command.template_id if command.template_id is not None else job.offer_template_id
    _require_active_template(db, organization_id, template_id)
    offer_id, version_id = uuid.uuid4(), uuid.uuid4()
    offer = Offer(id=offer_id, organization_id=organization_id, application_id=application.id, job_id=job.id, template_id=template_id, current_version_id=version_id, candidate_response_deadline=command.candidate_response_deadline, is_special=command.is_special, special_reason=command.special_reason)
    version = OfferVersion(id=version_id, organization_id=organization_id, offer_id=offer_id, version_number=1, content=command.content, template_id=template_id, candidate_response_deadline=command.candidate_response_deadline, is_special=command.is_special, special_reason=command.special_reason, created_by=actor_user_id)
    db.add_all([offer, version])
    _audit(db, offer, actor_user_id, "offer.created", trace_id, {"application_id": application.id, "version_number": 1})
    db.flush()
    return offer


def update_offer_version(db, organization_id, offer_id, actor_user_id, command, *, expected_version, trace_id):
    offer = _offer(db, organization_id, offer_id)
    _require_version(offer, expected_version)
    if offer.status not in REVISION_SOURCE_STATUSES:
        raise OfferVersionConflict
    current = _current_version(db, offer)
    fields = command.model_fields_set
    content = command.content if "content" in fields else current.content
    template_id = command.template_id if "template_id" in fields else current.template_id
    deadline = command.candidate_response_deadline if "candidate_response_deadline" in fields else current.candidate_response_deadline
    is_special = command.is_special if "is_special" in fields else current.is_special
    special_reason = command.special_reason if "special_reason" in fields else current.special_reason
    if is_special:
        if not special_reason:
            raise OfferApprovalError("special reason is required")
    elif special_reason is not None:
        if "is_special" in fields and command.is_special is False and "special_reason" not in fields:
            special_reason = None
        else:
            raise OfferApprovalError("special reason is only allowed for special offers")
    _require_active_template(db, organization_id, template_id)
    snapshot = (content, template_id, deadline, is_special, special_reason)
    current_snapshot = (current.content, current.template_id, current.candidate_response_deadline, current.is_special, current.special_reason)
    if snapshot == current_snapshot:
        raise OfferVersionConflict("no changes")
    next_number = current.version_number + 1
    next_version = OfferVersion(id=uuid.uuid4(), organization_id=organization_id, offer_id=offer.id, version_number=next_number, content=content, template_id=template_id, candidate_response_deadline=deadline, is_special=is_special, special_reason=special_reason, created_by=actor_user_id)
    db.add(next_version)
    offer.current_version_id = next_version.id
    offer.template_id = template_id
    offer.candidate_response_deadline = deadline
    offer.is_special = is_special
    offer.special_reason = special_reason
    offer.status = "draft"
    offer.version += 1
    _audit(db, offer, actor_user_id, "offer.version_created", trace_id, {"version_number": next_number})
    db.flush()
    return offer


def submit_offer(db, organization_id, offer_id, actor_user_id, *, expected_version, trace_id):
    offer = _offer(db, organization_id, offer_id)
    _require_version(offer, expected_version)
    if offer.status not in {"draft", "changes_requested"}:
        raise OfferApprovalError("offer is not editable")
    job = db.scalar(select(Job).where(Job.organization_id == organization_id, Job.id == offer.job_id))
    if job is None or job.offer_approver_id is None:
        raise OfferApprovalError("job default approver is required")
    from server.app.recruiting.service import is_eligible_offer_approver
    if not is_eligible_offer_approver(db, organization_id, job.offer_approver_id):
        raise OfferApprovalError("job default approver is not eligible")
    current = _current_version(db, offer)
    _require_active_template(db, organization_id, current.template_id)
    if offer.status == "changes_requested" and db.scalar(select(exists().where(
        OfferApproval.organization_id == organization_id,
        OfferApproval.offer_id == offer.id,
        OfferApproval.offer_version_id == current.id,
        OfferApproval.status == "rejected",
    ))):
        raise OfferApprovalError("a new version is required after rejection")
    assignees = [job.offer_approver_id]
    if offer.is_special:
        configured = list(db.scalars(select(OrganizationSpecialOfferApprover.approver_id).where(OrganizationSpecialOfferApprover.organization_id == organization_id).order_by(OrganizationSpecialOfferApprover.position)))
        special_assignees = [
            assignee_id for assignee_id in configured
            if assignee_id != job.offer_approver_id and is_eligible_offer_approver(db, organization_id, assignee_id)
        ]
        if not special_assignees:
            raise OfferApprovalError("an eligible special approver is required")
        assignees.extend(special_assignees)
    deduplicated = list(dict.fromkeys(assignees))
    round_number = (db.scalar(select(func.coalesce(func.max(OfferApproval.round_number), 0)).where(OfferApproval.organization_id == organization_id, OfferApproval.offer_id == offer.id)) or 0) + 1
    for sequence, assignee_id in enumerate(deduplicated, start=1):
        db.add(OfferApproval(organization_id=organization_id, offer_id=offer.id, offer_version_id=current.id, round_number=round_number, version_number=current.version_number, sequence=sequence, assignee_id=assignee_id, status="pending" if sequence == 1 else "waiting"))
    current.submitted_at = current.submitted_at or datetime.now(timezone.utc)
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
    due_query = select(Offer).where(
        Offer.status == "sent",
        Offer.candidate_response_deadline <= now,
        ~exists().where(
            OfferResponse.organization_id == Offer.organization_id,
            OfferResponse.offer_id == Offer.id,
        ),
    )
    due = list(db.scalars(due_query.with_for_update(skip_locked=db.get_bind().dialect.name == "postgresql")))
    for offer in due:
        offer.status = "expired"
        offer.version += 1
        _audit(db, offer, None, "offer.expired", trace_id, {"deadline": offer.candidate_response_deadline.isoformat()})
    db.flush()
    return len(due)
