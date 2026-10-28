import uuid

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select

from server.app.identity.api import problem
from server.app.identity.models import AuditLog, Job
from server.app.onboarding.models import OnboardingRecord
from server.app.queue.models import OutboxEvent
from server.app.queue.repository import QueueRepository
from server.app.recruiting.api import _principal
from server.app.recruiting.authorization import RecruitingAction, RecruitingAuthorizationService
from server.app.recruiting.models import Application, Candidate
from server.app.recruiting.service import CandidateUnavailable, IdempotencyConflict, persisted_idempotent
from server.app.recruiting.termination import (
    TerminationError, confirm_external_resolution, lock_application,
    terminate_application, termination_projection,
)
from server.app.recruiting.termination_models import ApplicationTermination


router = APIRouter(prefix="/api/v1")
AUTH = RecruitingAuthorizationService()


class ReasonCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason_text: str = Field(min_length=1, max_length=2000)

    @field_validator("reason_text", mode="before")
    @classmethod
    def trimmed_reason(cls, value):
        return value.strip() if isinstance(value, str) else value


class TerminateCommand(ReasonCommand):
    preview_version: str = Field(min_length=64, max_length=64)
    confirm_not_onboarded: bool = False


def _response(data, *, etag=None):
    response = JSONResponse({"data": data})
    response.headers["Cache-Control"] = "no-store"
    if etag is not None:
        response.headers["ETag"] = f'"{etag}"'
    return response


def _scoped_query(principal):
    return (select(Application)
        .join(Job, (Job.organization_id == Application.organization_id) & (Job.id == Application.job_id))
        .join(Candidate, (Candidate.organization_id == Application.organization_id) & (Candidate.id == Application.candidate_id))
        .where(Application.organization_id == principal.organization_id, Candidate.deleted_at.is_(None),
               AUTH.job_predicate(principal, RecruitingAction.MANAGE_CANDIDATE, Job)))


def _authorized(db, principal, application_id, *, lock=False):
    application = db.scalar(_scoped_query(principal).where(Application.id == application_id))
    if application is None:
        return None
    if lock:
        application = lock_application(db, principal.organization_id, application_id)
        if db.scalar(_scoped_query(principal).where(Application.id == application_id)) is None:
            return None
    return application


@router.get("/applications/{application_id}/termination")
def get_termination(application_id: uuid.UUID, request: Request):
    principal = _principal(request)
    if isinstance(principal, JSONResponse):
        return principal
    with request.app.state.identity_store.sync_session() as db:
        application = _authorized(db, principal, application_id)
        if application is None:
            return problem(request, 404, "resource_not_found", "The resource was not found.")
        return _response(termination_projection(db, application), etag=application.version)


def _queue_reconciliation(db, application, *, expected_version, reason_text, actor_user_id, trace_id):
    stopped = db.scalar(select(ApplicationTermination).where(
        ApplicationTermination.organization_id == application.organization_id,
        ApplicationTermination.application_id == application.id,
    ).with_for_update().execution_options(populate_existing=True))
    if stopped is None:
        raise TerminationError("resource_not_found", 404)
    if stopped.version != expected_version:
        raise TerminationError("resource_version_conflict")
    requested = set()
    for item in stopped.external_items:
        if item.get("kind") != "feishu_onboarding" or item.get("state") not in {"unknown", "in_flight"}:
            continue
        try:
            onboarding_id, generation = uuid.UUID(item["resource_id"]), uuid.UUID(item["generation"])
        except (KeyError, TypeError, ValueError):
            continue
        record = db.scalar(select(OnboardingRecord).where(
            OnboardingRecord.organization_id == application.organization_id,
            OnboardingRecord.application_id == application.id, OnboardingRecord.id == onboarding_id,
        ))
        if record is None:
            continue
        originals = list(db.scalars(select(OutboxEvent).where(
            OutboxEvent.organization_id == application.organization_id,
            OutboxEvent.aggregate_id == record.id,
            OutboxEvent.aggregate_type == "feishu_onboarding",
            OutboxEvent.topic == "feishu.approval.onboarding.create",
        )))
        if any(original.payload.get("generation") == str(generation)
                and original.payload.get("organization_id") == str(application.organization_id)
                and original.payload.get("onboarding_id") == str(record.id) for original in originals):
            requested.add((record.id, generation))
    if not requested:
        raise TerminationError("external_reconciliation_unavailable")
    for onboarding_id, generation in sorted(requested, key=lambda value: (str(value[0]), str(value[1]))):
        QueueRepository(db).append_outbox(application.organization_id,
            "feishu.approval.onboarding.reconcile", "feishu_onboarding", onboarding_id,
            {"organization_id": str(application.organization_id), "onboarding_id": str(onboarding_id),
             "generation": str(generation)})
    stopped.version += 1
    db.add(AuditLog(organization_id=application.organization_id, actor_user_id=actor_user_id,
        category="recruiting", event_type="application.external_reconciliation_requested", outcome="success",
        resource_type="application", resource_id=application.id, trace_id=trace_id,
        metadata_json={"reason_text": reason_text, "request_count": len(requested)}))
    db.flush()


def _command(application_id, payload, request, if_match, key, *, external=False, reconcile=False):
    principal = _principal(request)
    if isinstance(principal, JSONResponse):
        return principal
    if if_match is None:
        return problem(request, 428, "precondition_required", "If-Match is required.")
    if len(if_match) < 3 or not if_match.startswith('"') or not if_match.endswith('"') or not if_match[1:-1].isascii() or not if_match[1:-1].isdigit() or int(if_match[1:-1]) < 1:
        return problem(request, 422, "validation_failed", "If-Match is invalid.")
    if not key or len(key) > 255 or not key.strip():
        return problem(request, 422, "idempotency_key_invalid", "Idempotency-Key is required.")
    expected = int(if_match[1:-1])
    body = {**payload.model_dump(), "expected_version": expected}
    operation = f"{'termination.reconcile' if reconcile else 'termination.resolve' if external else 'termination'}:{application_id}"
    with request.app.state.identity_store.sync_session() as db:
        try:
            application = _authorized(db, principal, application_id, lock=True)
            if application is None:
                return problem(request, 404, "resource_not_found", "The resource was not found.")
            def action():
                kwargs = dict(expected_version=expected, reason_text=payload.reason_text,
                              actor_user_id=principal.user_id, trace_id=request.state.trace_id)
                if reconcile:
                    _queue_reconciliation(db, application, **kwargs)
                elif external:
                    confirm_external_resolution(db, application, **kwargs)
                else:
                    terminate_application(db, application, expected_preview=payload.preview_version,
                        confirm_not_onboarded=payload.confirm_not_onboarded, request=request, **kwargs)
                return 200, termination_projection(db, application)
            status, data = persisted_idempotent(db, principal.organization_id, principal.user_id, operation, key, body, action)
            db.commit()
        except CandidateUnavailable:
            return problem(request, 404, "resource_not_found", "The resource was not found.")
        except IdempotencyConflict:
            return problem(request, 409, "idempotency_conflict", "The idempotency key was already used.")
        except TerminationError as error:
            return problem(request, error.status, error.code, "The termination command cannot be applied.")
        return _response(data, etag=data["termination"]["version"] if external or reconcile else data["version"])


@router.post("/applications/{application_id}/termination")
def post_termination(application_id: uuid.UUID, payload: TerminateCommand, request: Request,
                     if_match: str | None = Header(default=None),
                     idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")):
    return _command(application_id, payload, request, if_match, idempotency_key)


@router.post("/applications/{application_id}/termination/external-resolution")
def post_external_resolution(application_id: uuid.UUID, payload: ReasonCommand, request: Request,
                             if_match: str | None = Header(default=None),
                             idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")):
    return _command(application_id, payload, request, if_match, idempotency_key, external=True)


@router.post("/applications/{application_id}/termination/reconcile-external")
def post_external_reconciliation(application_id: uuid.UUID, payload: ReasonCommand, request: Request,
                                 if_match: str | None = Header(default=None),
                                 idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")):
    return _command(application_id, payload, request, if_match, idempotency_key, reconcile=True)


@router.get("/application-terminations/pending")
def pending_terminations(request: Request):
    principal = _principal(request)
    if isinstance(principal, JSONResponse):
        return principal
    with request.app.state.identity_store.sync_session() as db:
        scoped = _scoped_query(principal).with_only_columns(Application.id)
        rows = db.execute(select(ApplicationTermination, Candidate.display_name, Job.title)
            .join(Candidate, (Candidate.organization_id == ApplicationTermination.organization_id) & (Candidate.id == ApplicationTermination.candidate_id))
            .join(Job, (Job.organization_id == ApplicationTermination.organization_id) & (Job.id == ApplicationTermination.job_id))
            .where(ApplicationTermination.organization_id == principal.organization_id,
                ApplicationTermination.application_id.in_(scoped), ApplicationTermination.external_status == "pending")
            .order_by(ApplicationTermination.terminated_at, ApplicationTermination.id)).all()
        items = [{"application_id": str(row.application_id), "candidate_id": str(row.candidate_id),
            "candidate_name": name, "job_id": str(row.job_id), "job_title": title,
            "version": row.version, "terminated_at": row.terminated_at.isoformat(),
            "external_items": row.external_items} for row, name, title in rows]
        return _response({"items": items, "total": len(items)})
