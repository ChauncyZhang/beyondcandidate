import hashlib
from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Header, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy import select

from server.app.identity.api import problem, session_token
from server.app.identity.models import AuditLog
from server.app.identity.policy import Principal
from server.app.identity.service import InvalidSession
from server.app.recruiting.http import content_disposition
from server.app.recruiting.authorization import RecruitingAction
from server.app.recruiting.service import IdempotencyConflict, persisted_idempotent
from server.app.recruiting.storage import StorageObjectTooLarge, StorageReadFailed
from server.app.reports.models import ExportDownloadTicket, ExportRecord
from server.app.reports.schemas import (
    ExportCreate,
    ExportResource,
    FunnelResource,
    ScreeningQualityResource,
    TicketConsume,
    TicketResource,
)
from server.app.reports.service import (
    authorized_job_ids,
    consume_export_ticket,
    create_export_record,
    issue_export_ticket,
    recruiting_funnel,
    screening_quality,
)
from server.app.reports.storage import MAX_EXPORT_BYTES


router = APIRouter(prefix="/api/v1", tags=["reports"])
REPORT_ROLES = {"recruiting_admin", "recruiter", "hiring_manager"}
EXPORT_ROLES = {"recruiting_admin", "recruiter"}


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


def _role_allowed(principal: Principal, allowed: set[str]) -> bool:
    return principal.active and bool(principal.roles & allowed)


def _valid_range(request: Request, from_: datetime | None, to: datetime | None):
    if from_ is not None and to is not None and from_ > to:
        return problem(request, 422, "validation_failed", "The report date range is invalid.")
    return None


def _scoped_jobs(
    db,
    principal: Principal,
    job_id: UUID | None,
    request: Request,
    action: RecruitingAction = RecruitingAction.READ,
):
    job_ids = authorized_job_ids(db, principal, job_id, action)
    if job_id is not None and not job_ids:
        return _denied(request)
    return job_ids


def _export_scoped_jobs(db, principal: Principal, job_id: UUID | None, request: Request):
    readable = authorized_job_ids(db, principal, job_id, RecruitingAction.READ)
    exportable = authorized_job_ids(db, principal, job_id, RecruitingAction.EXPORT)
    if not exportable or set(readable) != set(exportable):
        return _denied(request)
    return exportable


def _export_data(export: ExportRecord) -> dict:
    return {
        "id": str(export.id),
        "status": export.status,
        "format": export.format,
        "row_count": export.row_count,
        "created_at": export.created_at.isoformat(),
        "completed_at": export.completed_at.isoformat() if export.completed_at else None,
    }


def _load_export(db, principal: Principal, export_id: UUID) -> ExportRecord | None:
    export = db.scalar(
        select(ExportRecord).where(
            ExportRecord.organization_id == principal.organization_id,
            ExportRecord.id == export_id,
            ExportRecord.requested_by == principal.user_id,
        )
    )
    if export is None:
        return None
    requested = {UUID(item) for item in export.filters.get("job_ids", [])}
    return export if requested <= set(authorized_job_ids(db, principal, action=RecruitingAction.EXPORT)) else None


@router.get("/reports/recruiting-funnel", response_model=FunnelResource)
def get_recruiting_funnel(
    request: Request,
    job_id: UUID | None = None,
    from_: datetime | None = Query(default=None, alias="from"),
    to: datetime | None = None,
):
    principal = _principal(request)
    if isinstance(principal, JSONResponse):
        return principal
    if not _role_allowed(principal, REPORT_ROLES):
        return _denied(request)
    if invalid := _valid_range(request, from_, to):
        return invalid
    with request.app.state.identity_store.sync_session() as db:
        job_ids = _scoped_jobs(db, principal, job_id, request)
        if isinstance(job_ids, JSONResponse):
            return job_ids
        data = recruiting_funnel(
            db, principal, job_ids, from_, to, request.app.state.recruiting_clock.current_time()
        )
        exportable = authorized_job_ids(db, principal, job_id, RecruitingAction.EXPORT)
        data["can_export"] = bool(job_ids) and set(job_ids) == set(exportable)
        response = JSONResponse({"data": data})
        response.headers["Cache-Control"] = "no-store"
        return response


@router.get("/reports/screening-quality", response_model=ScreeningQualityResource)
def get_screening_quality(
    request: Request,
    job_id: UUID | None = None,
    from_: datetime | None = Query(default=None, alias="from"),
    to: datetime | None = None,
):
    principal = _principal(request)
    if isinstance(principal, JSONResponse):
        return principal
    if not _role_allowed(principal, REPORT_ROLES):
        return _denied(request)
    if invalid := _valid_range(request, from_, to):
        return invalid
    with request.app.state.identity_store.sync_session() as db:
        job_ids = _scoped_jobs(db, principal, job_id, request)
        if isinstance(job_ids, JSONResponse):
            return job_ids
        response = JSONResponse({"data": screening_quality(db, principal, job_ids, from_, to)})
        response.headers["Cache-Control"] = "no-store"
        return response


@router.post("/exports", status_code=201, response_model=ExportResource)
def create_export(
    payload: ExportCreate,
    request: Request,
    idempotency_key: str | None = Header(None),
):
    principal = _principal(request)
    if isinstance(principal, JSONResponse):
        return principal
    if not _role_allowed(principal, EXPORT_ROLES):
        return _denied(request)
    if not idempotency_key or len(idempotency_key) > 255:
        return problem(request, 428, "idempotency_key_required", "Idempotency-Key is required.")
    if invalid := _valid_range(request, payload.from_, payload.to):
        return invalid
    with request.app.state.identity_store.sync_session() as db:
        job_ids = _export_scoped_jobs(db, principal, payload.job_id, request)
        if isinstance(job_ids, JSONResponse):
            return job_ids
        body = payload.model_dump(by_alias=True)

        def action():
            export = create_export_record(
                db,
                principal,
                job_ids,
                payload.from_,
                payload.to,
                request.state.trace_id,
                idempotency_key,
            )
            return 201, {"data": _export_data(export)}

        try:
            status, response_body = persisted_idempotent(
                db,
                principal.organization_id,
                principal.user_id,
                "report_export.create",
                idempotency_key,
                body,
                action,
            )
            db.commit()
        except IdempotencyConflict:
            db.rollback()
            return problem(request, 409, "idempotency_conflict", "The request conflicts with an earlier request.")
        response = JSONResponse(response_body, status_code=status)
        response.headers["Location"] = f"/api/v1/exports/{response_body['data']['id']}"
        response.headers["Cache-Control"] = "no-store"
        return response


@router.get("/exports/{export_id}", response_model=ExportResource)
def get_export(export_id: UUID, request: Request):
    principal = _principal(request)
    if isinstance(principal, JSONResponse):
        return principal
    if not _role_allowed(principal, EXPORT_ROLES):
        return _denied(request)
    with request.app.state.identity_store.sync_session() as db:
        export = _load_export(db, principal, export_id)
        if export is None:
            return _denied(request)
        response = JSONResponse({"data": _export_data(export)})
        response.headers["Cache-Control"] = "no-store"
        return response


@router.post("/exports/{export_id}/download-tickets", status_code=201, response_model=TicketResource)
def create_download_ticket(export_id: UUID, request: Request):
    principal = _principal(request)
    if isinstance(principal, JSONResponse):
        return principal
    if not _role_allowed(principal, EXPORT_ROLES):
        return _denied(request)
    with request.app.state.identity_store.sync_session() as db:
        export = _load_export(db, principal, export_id)
        if export is None or export.status != "succeeded":
            return _denied(request)
        raw = issue_export_ticket(
            db, export, principal, request.app.state.recruiting_clock, request.app.state.recruiting_tokens
        )
        db.commit()
        response = JSONResponse({"data": {"token": raw, "expires_in": 60}}, status_code=201)
        response.headers["Cache-Control"] = "no-store"
        return response


@router.post(
    "/export-download-tickets/consume",
    responses={200: {"content": {"text/csv": {"schema": {"type": "string", "format": "binary"}}}}},
)
def download_export(payload: TicketConsume, request: Request):
    principal = _principal(request)
    if isinstance(principal, JSONResponse):
        return principal
    if not _role_allowed(principal, EXPORT_ROLES):
        return _denied(request)
    with request.app.state.identity_store.sync_session() as db:
        ticket = db.scalar(
            select(ExportDownloadTicket).where(
                ExportDownloadTicket.organization_id == principal.organization_id,
                ExportDownloadTicket.user_id == principal.user_id,
                ExportDownloadTicket.token_hash == hashlib.sha256(payload.token.encode()).hexdigest(),
            )
        )
        if ticket is None:
            return _denied(request)
        export = _load_export(db, principal, ticket.export_id)
        if export is None or export.status != "succeeded" or export.object_key is None:
            return _denied(request)
        spool = None
        try:
            consume_export_ticket(db, payload.token, principal, export, request.app.state.recruiting_clock)
            spool = request.app.state.export_storage.open_download(export.object_key, MAX_EXPORT_BYTES)
        except LookupError:
            if spool is not None:
                spool.close()
            db.rollback()
            return _denied(request)
        except (StorageReadFailed, StorageObjectTooLarge, KeyError):
            if spool is not None:
                spool.close()
            db.rollback()
            return problem(request, 503, "export_unavailable", "The export is temporarily unavailable.")
        db.add(
            AuditLog(
                organization_id=principal.organization_id,
                actor_user_id=principal.user_id,
                event_type="report_export.downloaded",
                outcome="success",
                trace_id=request.state.trace_id,
                metadata_json={"export_id": str(export.id)},
            )
        )
        db.commit()

        def stream():
            try:
                while chunk := spool.read(64 * 1024):
                    yield chunk
            finally:
                spool.close()

        response = StreamingResponse(stream(), media_type="text/csv; charset=utf-8")
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Disposition"] = content_disposition(f"recruiting-export-{export.id}.csv")
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response
