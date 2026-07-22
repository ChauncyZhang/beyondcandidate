from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import and_, literal, select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from server.app.identity.api import problem, session_token
from server.app.identity.models import Job
from server.app.identity.policy import Principal
from server.app.identity.service import InvalidSession
from server.app.notifications.models import NotificationRead
from server.app.notifications.service import workbench_notification_version
from server.app.recruiting.authorization import RecruitingAction, RecruitingAuthorizationService
from server.app.recruiting.models import Application, ApplicationReviewTask, Candidate


router = APIRouter(prefix="/api/v1/notifications")
AUTH = RecruitingAuthorizationService()
TASK_ACTIONS = {
    "interview_pending": RecruitingAction.TRANSITION,
    "decision": RecruitingAction.RECOMMEND,
    "passed": RecruitingAction.TRANSITION,
}


class NotificationReadCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: str = Field(pattern=r"^[0-9a-f]{64}$")


class NotificationReadOut(BaseModel):
    application_id: UUID
    version: str
    read_at: datetime


class NotificationReadResource(BaseModel):
    data: NotificationReadOut


def _principal(request: Request) -> Principal | JSONResponse:
    token = session_token(request)
    if not token:
        return problem(request, 401, "authentication_required", "Authentication is required.")
    try:
        return request.app.state.identity_service.principal(token)
    except InvalidSession:
        return problem(request, 401, "authentication_required", "Authentication is required.")


def _base_projection():
    return (
        Application.id.label("application_id"),
        Application.version.label("application_version"),
        Application.stage,
        Application.updated_at,
    )


def _current_notification(db, principal: Principal, application_id: UUID) -> tuple[Any, str] | None:
    review = db.execute(
        select(
            *_base_projection(),
            ApplicationReviewTask.id.label("task_id"),
            ApplicationReviewTask.ai_status,
            Job.hiring_owner_id,
        )
        .join(
            Application,
            and_(
                Application.organization_id == ApplicationReviewTask.organization_id,
                Application.id == ApplicationReviewTask.application_id,
            ),
        )
        .join(
            Candidate,
            and_(
                Candidate.organization_id == Application.organization_id,
                Candidate.id == Application.candidate_id,
            ),
        )
        .join(
            Job,
            and_(Job.organization_id == Application.organization_id, Job.id == Application.job_id),
        )
        .where(
            ApplicationReviewTask.organization_id == principal.organization_id,
            ApplicationReviewTask.application_id == application_id,
            ApplicationReviewTask.assignee_id == principal.user_id,
            ApplicationReviewTask.status == "open",
            Candidate.deleted_at.is_(None),
            AUTH.job_predicate(principal, RecruitingAction.READ, Job),
        )
    ).first()
    if review is not None:
        return review, workbench_notification_version(
            review,
            stage="review",
            task_id=review.task_id,
            ai_status=review.ai_status,
            config_warning=review.hiring_owner_id is None,
        )

    visible_job_ids = select(Job.id).where(
        Job.organization_id == principal.organization_id,
        Job.status == "open",
        AUTH.job_predicate(principal, RecruitingAction.READ, Job),
    ).order_by(Job.updated_at.desc(), Job.id.desc()).limit(20)
    action_predicates = {
        stage: AUTH.job_predicate(principal, action, Job)
        for stage, action in TASK_ACTIONS.items()
    }
    action_allowed = literal(False)
    for stage, predicate in action_predicates.items():
        stage_predicate = literal(predicate) if isinstance(predicate, bool) else predicate
        action_allowed = action_allowed | and_(Application.stage == stage, stage_predicate)
    row = db.execute(
        select(
            *_base_projection(),
            literal(None).label("task_id"),
            literal(None).label("ai_status"),
        )
        .join(
            Candidate,
            and_(
                Candidate.organization_id == Application.organization_id,
                Candidate.id == Application.candidate_id,
            ),
        )
        .join(
            Job,
            and_(Job.organization_id == Application.organization_id, Job.id == Application.job_id),
        )
        .where(
            Application.organization_id == principal.organization_id,
            Application.id == application_id,
            Application.stage.in_(TASK_ACTIONS),
            Application.job_id.in_(visible_job_ids),
            Job.status == "open",
            Candidate.deleted_at.is_(None),
            AUTH.job_predicate(principal, RecruitingAction.READ, Job),
            action_allowed,
        )
    ).first()
    if row is None:
        return None
    return row, workbench_notification_version(row)


def _insert_receipt_if_missing(db, principal: Principal, application_id: UUID, version: str) -> None:
    values = {
        "id": uuid4(),
        "organization_id": principal.organization_id,
        "user_id": principal.user_id,
        "application_id": application_id,
        "notification_version": version,
        "read_at": datetime.now(timezone.utc),
    }
    dialect = db.get_bind().dialect.name
    statement = (
        postgresql_insert(NotificationRead) if dialect == "postgresql"
        else sqlite_insert(NotificationRead) if dialect == "sqlite"
        else None
    )
    if statement is None:
        db.add(NotificationRead(**values))
        db.flush()
        return
    db.execute(statement.values(**values).on_conflict_do_nothing(
        index_elements=["organization_id", "user_id", "application_id"],
    ))


@router.put(
    "/workbench/{application_id}/read",
    response_model=NotificationReadResource,
    responses={status: {"content": {"application/problem+json": {}}} for status in (401, 404, 409, 422)},
)
def mark_workbench_notification_read(
    application_id: UUID,
    command: NotificationReadCommand,
    request: Request,
    response: Response,
):
    principal = _principal(request)
    if isinstance(principal, JSONResponse):
        return principal
    response.headers["Cache-Control"] = "no-store"
    with request.app.state.identity_store.sync_session() as db:
        current = _current_notification(db, principal, application_id)
        if current is None:
            return problem(request, 404, "resource_not_found", "The requested resource is unavailable.")
        _, current_version = current
        if command.version != current_version:
            return problem(
                request,
                409,
                "notification_version_conflict",
                "The notification changed before it could be marked read.",
            )
        receipt = db.scalar(
            select(NotificationRead).where(
                NotificationRead.organization_id == principal.organization_id,
                NotificationRead.user_id == principal.user_id,
                NotificationRead.application_id == application_id,
            )
        )
        if receipt is None:
            _insert_receipt_if_missing(db, principal, application_id, current_version)
            receipt = db.scalar(
                select(NotificationRead).where(
                    NotificationRead.organization_id == principal.organization_id,
                    NotificationRead.user_id == principal.user_id,
                    NotificationRead.application_id == application_id,
                )
            )
        elif receipt.notification_version != current_version:
            receipt.notification_version = current_version
            receipt.read_at = datetime.now(timezone.utc)
            db.flush()
        body = {
            "data": {
                "application_id": application_id,
                "version": receipt.notification_version,
                "read_at": receipt.read_at if receipt.read_at.tzinfo is not None else receipt.read_at.replace(tzinfo=timezone.utc),
            }
        }
        db.commit()
        return body
