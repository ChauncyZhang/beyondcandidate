import base64
import hashlib
import hmac
import uuid
from datetime import datetime, timezone

from sqlalchemy import exists, func, select

from server.app.identity.models import AuditLog, Job
from server.app.recruiting.models import Application
from server.app.recruiting.service import ResourceVersionConflict
from server.app.offers.models import Offer, OfferAccessToken, OfferApproval, OfferEvent, OfferResponse, OfferTemplate, OfferVersion, OrganizationSpecialOfferApprover
from server.app.offers.pdf import offer_pdf_storage_key, render_offer_pdf


class OfferNotFound(Exception):
    pass


class OfferVersionConflict(ResourceVersionConflict):
    pass


class OfferApprovalError(Exception):
    pass


FINAL_OFFER_STATUSES = {"withdrawn", "expired"}
ACTIVE_OFFER_STATUSES = {"draft", "pending_approval", "changes_requested", "ready_to_send", "sent"}
REVISION_SOURCE_STATUSES = {"draft", "changes_requested", "ready_to_send", "sent"}
PDF_RECEIPT_FIELDS = ("pdf_object_key", "pdf_sha256", "pdf_size_bytes", "pdf_rendered_at")


class OfferTokenCodec:
    """A purpose-separated deterministic 256-bit public capability codec."""
    def __init__(self, secret: bytes):
        self._key = hmac.new(secret, b"beyondcandidate:offer-access-token:v1", hashlib.sha256).digest()

    def raw_token(self, token_id: uuid.UUID) -> str:
        value = hmac.new(self._key, token_id.bytes, hashlib.sha256).digest()
        return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")

    def issue(self, *, organization_id, offer_id, offer_version_id, expires_at):
        row = OfferAccessToken(
            id=uuid.uuid4(),
            organization_id=uuid.UUID(str(organization_id)), offer_id=uuid.UUID(str(offer_id)),
            offer_version_id=uuid.UUID(str(offer_version_id)), expires_at=expires_at,
            token_hash="0" * 64,
        )
        raw = self.raw_token(row.id)
        row.token_hash = hashlib.sha256(raw.encode("ascii")).hexdigest()
        return row, raw

    def matches(self, row: OfferAccessToken, raw: str) -> bool:
        return hmac.compare_digest(row.token_hash, hashlib.sha256(raw.encode("ascii")).hexdigest()) and hmac.compare_digest(raw, self.raw_token(row.id))


def issue_offer_access_token(db, organization_id, offer, version, *, codec: OfferTokenCodec, now: datetime):
    """Replace every capability for an offer; the raw capability never leaves this call."""
    for previous in db.scalars(select(OfferAccessToken).where(OfferAccessToken.organization_id == organization_id, OfferAccessToken.offer_id == offer.id, OfferAccessToken.revoked_at.is_(None)).with_for_update()):
        previous.revoked_at = now
    token, raw = codec.issue(organization_id=organization_id, offer_id=offer.id, offer_version_id=version.id, expires_at=version.candidate_response_deadline)
    db.add(token)
    db.flush()
    return token, raw


def public_offer_access(db, raw_token: str, *, codec: OfferTokenCodec, now: datetime, lock: bool = False, allow_revoked: bool = False):
    digest = hashlib.sha256(raw_token.encode("ascii")).hexdigest()
    query = select(OfferAccessToken).where(OfferAccessToken.token_hash == digest)
    token = db.scalar(query.with_for_update() if lock else query)
    if token is None or not codec.matches(token, raw_token) or (token.revoked_at is not None and not allow_revoked) or token.expires_at <= now:
        raise OfferNotFound
    offer = db.scalar(select(Offer).where(Offer.organization_id == token.organization_id, Offer.id == token.offer_id).with_for_update())
    version = db.scalar(select(OfferVersion).where(OfferVersion.organization_id == token.organization_id, OfferVersion.id == token.offer_version_id, OfferVersion.offer_id == token.offer_id).with_for_update())
    if offer is None or version is None or offer.current_version_id != version.id or (offer.status != "sent" and not allow_revoked):
        raise OfferNotFound
    application = db.scalar(select(Application).where(Application.organization_id == token.organization_id, Application.id == offer.application_id).with_for_update())
    if application is None:
        raise OfferNotFound
    return token, offer, version, application


def record_public_offer_response(db, raw_token, payload, *, codec: OfferTokenCodec, now: datetime, trace_id: str):
    token, offer, version, application = public_offer_access(db, raw_token, codec=codec, now=now, lock=True, allow_revoked=True)
    canonical = f"{payload.decision}|{payload.expected_start_date.isoformat() if payload.expected_start_date else ''}|{payload.reason_text or ''}"
    request_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    existing = db.scalar(select(OfferResponse).where(OfferResponse.organization_id == offer.organization_id, OfferResponse.offer_id == offer.id).with_for_update())
    if existing is not None:
        if existing.request_hash and hmac.compare_digest(existing.request_hash, request_hash):
            return existing, True
        raise OfferVersionConflict("conflicting response")
    if token.revoked_at is not None:
        raise OfferNotFound
    from server.app.recruiting.service import apply_application_workflow_action_record
    action = "offer_accepted" if payload.decision == "accepted" else "offer_declined"
    apply_application_workflow_action_record(db, offer.organization_id, application.id, action, expected_version=application.version, actor_user_id=None, trace_id=trace_id, reason_text=payload.reason_text)
    response = OfferResponse(organization_id=offer.organization_id, offer_id=offer.id, offer_version_id=version.id, status="accepted" if action == "offer_accepted" else "declined", expected_start_date=payload.expected_start_date, reason_text=payload.reason_text, request_hash=request_hash, responded_at=now)
    db.add(response)
    offer.status = response.status; offer.version += 1; token.revoked_at = now
    _audit(db, offer, None, f"offer.{response.status}", trace_id, {"version_number": version.version_number})
    db.flush()
    return response, False


def mark_offer_delivery_sent(db, delivery, *, now: datetime):
    token = db.scalar(select(OfferAccessToken).where(OfferAccessToken.organization_id == delivery.organization_id, OfferAccessToken.id == delivery.resource_id).with_for_update())
    if token is None or token.revoked_at is not None:
        raise OfferApprovalError("offer delivery is unavailable")
    offer = db.scalar(select(Offer).where(Offer.organization_id == token.organization_id, Offer.id == token.offer_id).with_for_update())
    if offer is None or offer.status != "ready_to_send" or offer.current_version_id != token.offer_version_id:
        raise OfferApprovalError("offer delivery is unavailable")
    token.delivered_at = now
    offer.status = "sent"; offer.version += 1
    _audit(db, offer, None, "offer.sent", f"email-{str(delivery.id)[:8]}", {})


def revoke_offer_delivery_token(db, delivery, *, now: datetime):
    token = db.scalar(select(OfferAccessToken).where(OfferAccessToken.organization_id == delivery.organization_id, OfferAccessToken.id == delivery.resource_id).with_for_update())
    if token is not None and token.revoked_at is None:
        token.revoked_at = now


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


def persist_offer_version_pdf(
    db,
    organization_id,
    offer_version_id,
    template_html,
    variables,
    storage,
):
    version = db.scalar(
        select(OfferVersion)
        .where(
            OfferVersion.organization_id == organization_id,
            OfferVersion.id == offer_version_id,
        )
        .with_for_update()
    )
    if version is None:
        raise OfferNotFound
    receipt_values = tuple(getattr(version, field) for field in PDF_RECEIPT_FIELDS)
    if all(value is not None for value in receipt_values):
        return version
    if any(value is not None for value in receipt_values):
        raise OfferApprovalError("offer PDF receipt is incomplete")

    pdf_bytes = render_offer_pdf(template_html, variables)
    digest = hashlib.sha256(pdf_bytes).hexdigest()
    storage_key = offer_pdf_storage_key(version.organization_id, version.offer_id, version.id)
    storage.write_immutable(storage_key, pdf_bytes, digest)
    version.pdf_object_key = storage_key
    version.pdf_sha256 = digest
    version.pdf_size_bytes = len(pdf_bytes)
    version.pdf_rendered_at = datetime.now(timezone.utc)
    db.flush()
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
