"""Application-scoped stop boundary; no provider calls or history deletion."""

import hashlib
import json
from datetime import datetime, timezone
from types import SimpleNamespace

from sqlalchemy import or_, select

from server.app.communications.models import EmailDelivery
from server.app.governance.retention import recalculate_candidate_retention
from server.app.identity.models import AuditLog
from server.app.integrations.feishu.models import FeishuInterviewSync
from server.app.interviews.models import Interview, InterviewParticipant
from server.app.offers.models import Offer, OfferAccessToken, OfferApproval, OfferEvent, OfferResponse
from server.app.onboarding.models import OnboardingRecord
from server.app.queue.models import OutboxEvent
from server.app.recruiting.models import Application, ApplicationReviewTask, ApplicationStageEvent, CandidateEvent, now
from server.app.recruiting.service import lock_active_candidate
from server.app.recruiting.termination_models import ApplicationTermination


UNFINISHED_INTERVIEWS = {"draft", "scheduled", "confirmed", "completed", "pending_feedback", "rescheduled"}
EFFECTIVE_OFFERS = {"draft", "pending_approval", "changes_requested", "ready_to_send", "sent", "accepted"}
FACT_STATES = {"in_flight", "unknown", "needs_action", "resolved"}


class TerminationError(ValueError):
    def __init__(self, code, status=409):
        self.code, self.status = code, status
        super().__init__(code)


def application_is_terminated(db, organization_id, application_id) -> bool:
    # This predicate deliberately takes no locks and has no implicit flush.
    with db.no_autoflush:
        return db.scalar(select(ApplicationTermination.id).where(
            ApplicationTermination.organization_id == organization_id,
            ApplicationTermination.application_id == application_id,
        )) is not None or db.scalar(select(Application.id).where(
            Application.organization_id == organization_id, Application.id == application_id,
            Application.stage == "withdrawn",
        )) is not None


def lock_application(db, organization_id, application_id):
    candidate_id = db.scalar(select(Application.candidate_id).where(
        Application.organization_id == organization_id, Application.id == application_id,
    ))
    if candidate_id is None:
        return None
    lock_active_candidate(db, organization_id, candidate_id)
    return db.scalar(select(Application).where(
        Application.organization_id == organization_id, Application.id == application_id,
    ).with_for_update().execution_options(populate_existing=True))


def termination_record(db, application, *, lock=False):
    query = select(ApplicationTermination).where(
        ApplicationTermination.organization_id == application.organization_id,
        ApplicationTermination.application_id == application.id,
    )
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    return db.scalar(query)


def _identity(item):
    return tuple(item.get(key) for key in ("kind", "resource_id", "reference", "generation"))


def _apply_fact(record, *, kind, resource_id, state, reference=None, generation=None):
    if state not in FACT_STATES:
        raise ValueError("invalid external fact state")
    item = {"kind": str(kind), "resource_id": str(resource_id), "state": state,
            "reference": str(reference) if reference is not None else None,
            "generation": str(generation) if generation is not None else None}
    items = [dict(value) for value in record.external_items]
    # Replayed, already-attested facts must not reopen cleanup.
    if state != "resolved" and any(_identity(previous) == _identity(item)
            for history in record.resolution_history for previous in history.get("items", [])):
        return False
    index = next((i for i, value in enumerate(items) if _identity(value) == _identity(item)), None)
    if index is None:
        # A definite response replaces the unresolved request, not other generations
        # or other known remote instances from that same request.
        index = next((i for i, value in enumerate(items)
            if value["kind"] == item["kind"] and value["resource_id"] == item["resource_id"]
            and value.get("generation") == item["generation"] and value.get("reference") is None
            and value["state"] in {"unknown", "in_flight"}), None)
    if index is not None:
        previous = items[index]
        if previous == item:
            return False
        if previous["state"] in {"needs_action", "resolved"} and state in {"unknown", "in_flight"}:
            return False
        items[index] = item
    else:
        items.append(item)
    record.external_items = items
    record.external_status = "pending" if any(value["state"] != "resolved" for value in items) else "resolved"
    record.version += 1
    return True


def record_external_fact(db, organization_id, application_id, *, kind, resource_id, state, reference=None, generation=None):
    if not application_is_terminated(db, organization_id, application_id):
        return None
    application = lock_application(db, organization_id, application_id)
    if application is None:
        return None
    record = termination_record(db, application, lock=True)
    if record is not None:
        _apply_fact(record, kind=kind, resource_id=resource_id, state=state, reference=reference, generation=generation)
        db.flush()
    return record


def dependent_resources(db, application, *, lock=False):
    org = application.organization_id
    def rows(model, condition):
        query = select(model).where(model.organization_id == org, condition).order_by(model.id)
        if lock:
            query = query.with_for_update().execution_options(populate_existing=True)
        return list(db.scalars(query))
    reviews = rows(ApplicationReviewTask, ApplicationReviewTask.application_id == application.id)
    interviews = rows(Interview, Interview.application_id == application.id)
    interview_ids = [item.id for item in interviews]
    participants = rows(InterviewParticipant, InterviewParticipant.interview_id.in_(interview_ids))
    offers = rows(Offer, Offer.application_id == application.id)
    offer_ids = [item.id for item in offers]
    approvals = rows(OfferApproval, OfferApproval.offer_id.in_(offer_ids))
    tokens = rows(OfferAccessToken, OfferAccessToken.offer_id.in_(offer_ids))
    responses = rows(OfferResponse, OfferResponse.offer_id.in_(offer_ids))
    messages = rows(EmailDelivery, or_(
        (EmailDelivery.resource_type.in_(("interview_invitation", "interview_rescheduled", "interview_cancelled"))) & EmailDelivery.resource_id.in_(interview_ids),
        (EmailDelivery.resource_type == "offer_access_token") & EmailDelivery.resource_id.in_([item.id for item in tokens]),
        (EmailDelivery.resource_type == "offer_response") & EmailDelivery.resource_id.in_([item.id for item in responses]),
    ))
    onboardings = rows(OnboardingRecord, OnboardingRecord.application_id == application.id)
    calendars = rows(FeishuInterviewSync, FeishuInterviewSync.interview_id.in_(interview_ids))
    onboarding_events = rows(OutboxEvent, (OutboxEvent.topic == "feishu.approval.onboarding.create")
        & OutboxEvent.aggregate_id.in_([item.id for item in onboardings]))
    return dict(review_tasks=reviews, interviews=interviews, feedback_tasks=participants, offers=offers,
                approvals=approvals, tokens=tokens, responses=responses, queued_messages=messages,
                onboarding=onboardings, calendars=calendars, onboarding_events=onboarding_events)


def preview_version(application, resources):
    # Only opaque IDs, state/version and timing facts enter the fingerprint.
    fields = ("status", "task_status", "version", "closed_at", "revoked_at", "delivered_at", "generation",
              "feishu_instance_code", "sync_status", "desired_action", "external_event_id", "idempotency_key",
              "updated_at", "attempts", "safe_error_code", "last_error_code", "payload")
    snapshot = {key: [{"id": str(row.id), **{field: getattr(row, field) for field in fields if hasattr(row, field)}}
                      for row in values] for key, values in resources.items()}
    snapshot["application"] = [str(application.id), application.stage, application.version]
    def canonical(value):
        if isinstance(value, datetime):
            return value.replace(tzinfo=value.tzinfo or timezone.utc).astimezone(timezone.utc).isoformat()
        return str(value)
    return hashlib.sha256(json.dumps(snapshot, sort_keys=True, default=canonical, separators=(",", ":")).encode()).hexdigest()


def _counts(resources):
    return {
        "review_tasks": sum(row.status == "open" for row in resources["review_tasks"]),
        "interviews": sum(row.status in UNFINISHED_INTERVIEWS for row in resources["interviews"]),
        "feedback_tasks": sum(row.task_status == "ready" for row in resources["feedback_tasks"]),
        "offers": sum(row.status in EFFECTIVE_OFFERS for row in resources["offers"]),
        "approvals": sum(row.status in {"waiting", "pending"} for row in resources["approvals"]),
        "queued_messages": sum(row.status == "queued" for row in resources["queued_messages"]),
        "onboarding": len(resources["onboarding"]),
    }


def _onboarding_external_facts(resources):
    for onboarding in resources["onboarding"]:
        events = [event for event in resources["onboarding_events"] if event.aggregate_id == onboarding.id]
        generations = {event.payload.get("generation") for event in events}
        if onboarding.generation is not None:
            generations.add(str(onboarding.generation))
        for generation in sorted(generations, key=str):
            generation_events = [event for event in events if event.payload.get("generation") == generation]
            references = {event.payload["instance_code"] for event in generation_events if event.payload.get("instance_code")}
            if str(onboarding.generation) == generation and onboarding.feishu_instance_code:
                references.add(onboarding.feishu_instance_code)
            for reference in sorted(references):
                yield dict(kind="feishu_onboarding", resource_id=onboarding.id,
                    state="needs_action", reference=reference, generation=generation)
            unresolved = [event for event in generation_events if not event.payload.get("instance_code")
                and event.payload.get("submission_state") not in {"preflight", "prepared", "succeeded"}
                and (event.payload.get("submission_state") in {"attempted", "unknown"}
                    or event.payload.get("submission_fingerprint") or event.attempts > 0)]
            if unresolved:
                yield dict(kind="feishu_onboarding", resource_id=onboarding.id,
                    state="unknown" if any(event.payload.get("submission_state") != "attempted" for event in unresolved) else "in_flight",
                    generation=generation)
            elif not generation_events and not references:
                # Legacy generations without a durable request ledger are not
                # proof that an external create never happened.
                yield dict(kind="feishu_onboarding", resource_id=onboarding.id,
                    state="unknown", generation=generation)


def _uncertain_calendar_create(calendar):
    return (calendar.desired_action in {"create", "update"} and not calendar.external_event_id
        and (calendar.attempts > 0 or calendar.last_error_code or calendar.sync_status == "syncing"))


def preview_external_items(resources):
    """Derive opaque external impact without modifying ORM rows or issuing writes."""
    snapshot = SimpleNamespace(external_items=[], resolution_history=[], external_status="resolved", version=1)
    for fact in _onboarding_external_facts(resources):
        _apply_fact(snapshot, **fact)
    for calendar in resources["calendars"]:
        if _uncertain_calendar_create(calendar):
            _apply_fact(snapshot, kind="calendar", resource_id=calendar.interview_id,
                state="unknown", generation=calendar.idempotency_key)
        elif calendar.external_event_id and calendar.sync_status != "cancelled":
            _apply_fact(snapshot, kind="calendar", resource_id=calendar.interview_id,
                state="in_flight" if calendar.sync_status == "syncing" else "needs_action",
                reference=calendar.external_event_id, generation=calendar.idempotency_key)
    for delivery in resources["queued_messages"]:
        if delivery.status == "queued" and delivery.safe_error_code == "email_smtp_in_flight":
            _apply_fact(snapshot, kind="email", resource_id=delivery.id, state="in_flight", generation=delivery.attempts)
    return snapshot.external_items


def termination_projection(db, application, *, resources=None):
    resources = resources if resources is not None else dependent_resources(db, application)
    record = termination_record(db, application)
    pending = record is not None and record.external_status == "pending"
    return {"application_id": str(application.id), "candidate_id": str(application.candidate_id),
        "job_id": str(application.job_id), "version": application.version, "stage": application.stage,
        "preview_version": preview_version(application, resources), "counts": _counts(resources),
        "external_items": record.external_items if record is not None else preview_external_items(resources),
        "requires_not_onboarded_confirmation": application.stage == "hired",
        "allowed_actions": {"terminate": record is None and application.stage not in {"rejected", "withdrawn"},
            "reconcile_external": pending and any(item["kind"] == "feishu_onboarding"
                and item["state"] in {"unknown", "in_flight"} for item in record.external_items),
            "confirm_external": pending and not any(item["state"] in {"unknown", "in_flight"} for item in record.external_items)},
        "termination": None if record is None else {"id": str(record.id), "version": record.version,
            "source_stage": record.source_stage, "reason_text": record.reason_text,
            "terminated_at": record.terminated_at.isoformat(), "external_status": record.external_status,
            "external_items": record.external_items, "resolution_history": record.resolution_history}}


def _audit(db, application, actor_user_id, trace_id, event_type):
    db.add(AuditLog(organization_id=application.organization_id, actor_user_id=actor_user_id,
        category="recruiting", event_type=event_type, outcome="success", resource_type="application",
        resource_id=application.id, trace_id=trace_id, metadata_json={"application_id": str(application.id)}))


def terminate_application(db, application, *, expected_version, expected_preview, reason_text,
                          confirm_not_onboarded, actor_user_id, trace_id, request=None):
    if application.version != expected_version:
        raise TerminationError("resource_version_conflict")
    if application.stage in {"rejected", "withdrawn"} or termination_record(db, application) is not None:
        raise TerminationError("application_already_terminal")
    resources = dependent_resources(db, application, lock=True)
    if preview_version(application, resources) != expected_preview:
        raise TerminationError("termination_preview_changed")
    if application.stage == "hired" and not confirm_not_onboarded:
        raise TerminationError("not_onboarded_confirmation_required", 422)
    source_stage = application.stage
    timestamp = now()
    record = ApplicationTermination(organization_id=application.organization_id, application_id=application.id,
        candidate_id=application.candidate_id, job_id=application.job_id, actor_user_id=actor_user_id,
        source_stage=source_stage, reason_text=reason_text, terminated_at=timestamp, version=1,
        external_status="resolved", external_items=[], resolution_history=[])
    db.add(record)
    application.stage = "withdrawn"
    application.version += 1
    db.flush()
    for task in resources["review_tasks"]:
        if task.status == "open":
            task.status, task.closed_at = "cancelled", timestamp
    for calendar in resources["calendars"]:
        if _uncertain_calendar_create(calendar):
            # Cancellation rotates the sync generation. Preserve the uncertain
            # original create before the helper overwrites that request state.
            _apply_fact(record, kind="calendar", resource_id=calendar.interview_id,
                state="unknown", generation=calendar.idempotency_key)
    for interview in resources["interviews"]:
        if interview.status in UNFINISHED_INTERVIEWS:
            from server.app.interviews.cancellation import cancel_interview_record
            cancel_interview_record(db, interview, actor_user_id=actor_user_id, trace_id=trace_id,
                reason_text=reason_text, notify_candidate=False, request=request)
    for offer in resources["offers"]:
        if offer.status in EFFECTIVE_OFFERS:
            offer.status = "withdrawn"
            offer.version += 1
            db.add(OfferEvent(organization_id=application.organization_id, offer_id=offer.id,
                actor_user_id=actor_user_id, event_type="application_terminated", payload={"reason": reason_text}))
    for approval in resources["approvals"]:
        if approval.status in {"waiting", "pending"}:
            approval.status = "cancelled"
    for token in resources["tokens"]:
        if token.revoked_at is None:
            token.revoked_at = timestamp
    for delivery in resources["queued_messages"]:
        if delivery.status == "queued":
            if delivery.safe_error_code == "email_smtp_in_flight":
                _apply_fact(record, kind="email", resource_id=delivery.id, state="in_flight", generation=delivery.attempts)
            else:
                delivery.status, delivery.safe_error_code = "cancelled", "application_terminated"
                delivery.version += 1
    for fact in _onboarding_external_facts(resources):
        _apply_fact(record, **fact)
    # Re-read calendar records changed by the cancellation helper.
    db.flush()
    for calendar in dependent_resources(db, application)["calendars"]:
        if calendar.external_event_id and calendar.sync_status != "cancelled":
            _apply_fact(record, kind="calendar", resource_id=calendar.interview_id,
                state="in_flight" if calendar.sync_status == "syncing" else "needs_action",
                reference=calendar.external_event_id, generation=calendar.idempotency_key)
    payload = {"from_stage": source_stage, "to_stage": "withdrawn", "reason_text": reason_text}
    db.add(ApplicationStageEvent(organization_id=application.organization_id, application_id=application.id,
        actor_user_id=actor_user_id, event_type="application_terminated", payload=payload))
    db.add(CandidateEvent(organization_id=application.organization_id, candidate_id=application.candidate_id,
        actor_user_id=actor_user_id, event_type="application_terminated", payload={"application_id": str(application.id), **payload}))
    _audit(db, application, actor_user_id, trace_id, "application.terminated")
    db.flush()
    recalculate_candidate_retention(db, application.organization_id, application.candidate_id)
    return record


def confirm_external_resolution(db, application, *, expected_version, reason_text, actor_user_id, trace_id):
    record = termination_record(db, application, lock=True)
    if record is None:
        raise TerminationError("resource_not_found", 404)
    if record.version != expected_version:
        raise TerminationError("resource_version_conflict")
    if any(item["state"] in {"in_flight", "unknown"} for item in record.external_items):
        raise TerminationError("external_resolution_pending")
    if record.external_status != "pending":
        raise TerminationError("external_resolution_not_pending")
    record.resolution_history = [*record.resolution_history, {"actor_user_id": str(actor_user_id),
        "reason_text": reason_text, "resolved_at": now().isoformat(), "version": record.version,
        "items": record.external_items}]
    record.external_items = [{**item, "state": "resolved"} for item in record.external_items]
    record.external_status = "resolved"
    record.version += 1
    _audit(db, application, actor_user_id, trace_id, "application.external_resolution_confirmed")
    db.flush()
    return record
