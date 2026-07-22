import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Header, Query, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from sqlalchemy import and_, delete, exists, func, or_, select, text

from server.app.governance.retention import recalculate_candidate_retention
from server.app.identity.api import problem, session_token
from server.app.identity.models import AuditLog, Job, JobCollaborator, User, UserRole, UserStatus, WorkflowTemplate
from server.app.identity.policy import Principal
from server.app.identity.service import InvalidSession
from server.app.integrations.feishu.sync import schedule_interview_sync
from server.app.interviews.domain import CalendarContact, build_calendar_invitation
from server.app.interviews.availability import INTERNAL_AVAILABILITY_PROVIDER, privacy_safe_availability
from server.app.llm.redaction import redact_screening_text
from server.app.interviews.models import (
    Interview,
    InterviewEvent,
    InterviewFeedback,
    InterviewFeedbackRevision,
    InterviewParticipant,
)
from server.app.interviews.schemas import (
    DataCollection,
    DataResource,
    FeedbackAmendment,
    FeedbackDraft,
    InterviewCreate,
    NewInterviewConflictInput,
    InterviewPatch,
    InterviewTransition,
    ScheduleInput,
)
from server.app.recruiting.authorization import RecruitingAction, RecruitingAuthorizationService
from server.app.recruiting.http import content_disposition
from server.app.recruiting.workflow import next_interview_round, normalized_workflow_rounds
from server.app.recruiting.models import Application, ApplicationStageEvent, Candidate, FileObject, JobJdVersion, Resume
from server.app.recruiting.storage import (
    MAX_DOWNLOAD_BYTES,
    MAX_PREVIEW_BYTES,
    StorageObjectTooLarge,
    StorageReadFailed,
)
from server.app.recruiting.service import (
    CandidateUnavailable,
    IdempotencyConflict,
    InvalidStateTransition,
    RecruitingService,
    ResourceVersionConflict,
    persisted_idempotent,
    lock_active_candidate,
    transition_application_record,
)
from server.app.screening.models import ScreeningResult


router = APIRouter(prefix="/api/v1")
AUTH = RecruitingAuthorizationService()
ETAG = re.compile(r'^"(0|[1-9][0-9]*)"$')
REQUIRED_RATINGS = {"professional_ability", "problem_solving", "communication", "role_fit"}
INTERVIEW_ACCESS_ROLES = frozenset({"recruiting_admin", "recruiter", "hiring_manager", "interviewer"})
INTERVIEW_PARTICIPANT_ROLES = ("recruiting_admin", "recruiter", "hiring_manager", "interviewer")
INTERVIEW_SCHEDULABLE_STAGES = frozenset({"interview_pending", "interviewing", "decision"})


class ScheduleConflict(Exception):
    def __init__(self, kind: str, interview_ids: list[UUID]):
        self.kind = kind
        self.interview_ids = interview_ids
        super().__init__(kind)


class InterviewTimeInPast(Exception):
    pass


class InterviewRoundMismatch(Exception):
    pass


def _expected_interview_round(db, application: Application) -> tuple[bool, str | None]:
    template_row = db.execute(
        select(WorkflowTemplate.id, WorkflowTemplate.rounds)
        .join(
            Job,
            and_(
                Job.organization_id == WorkflowTemplate.organization_id,
                Job.workflow_template_id == WorkflowTemplate.id,
            ),
        )
        .where(
            Job.organization_id == application.organization_id,
            Job.id == application.job_id,
        )
    ).first()
    if template_row is None:
        return False, None
    completed_rounds = db.scalars(select(Interview.round_name).where(
        Interview.organization_id == application.organization_id,
        Interview.application_id == application.id,
        Interview.status == "feedback_completed",
    )).all()
    return True, next_interview_round(template_row.rounds, completed_rounds)


def _advance_application_to_interviewing(db, application, *, principal, trace_id):
    if application.stage not in INTERVIEW_SCHEDULABLE_STAGES:
        raise InvalidStateTransition
    if application.stage == "decision":
        application.stage = "interviewing"
        application.version += 1
        application.updated_at = datetime.now(timezone.utc)
        stage_payload = {
            "from_stage": "decision",
            "to_stage": "interviewing",
            "reason": "additional_interview_scheduled",
        }
        db.add(ApplicationStageEvent(
            organization_id=principal.organization_id,
            application_id=application.id,
            actor_user_id=principal.user_id,
            event_type="application.stage_changed",
            payload=stage_payload,
        ))
        db.add(AuditLog(
            organization_id=principal.organization_id,
            actor_user_id=principal.user_id,
            event_type="application.stage_changed",
            outcome="success",
            resource_type="application",
            resource_id=application.id,
            trace_id=trace_id,
            metadata_json=stage_payload,
        ))
        db.flush()
        recalculate_candidate_retention(
            db, principal.organization_id, application.candidate_id
        )
        return application
    target_index = RecruitingService.APPLICATION_PATH.index("interviewing")
    while application.stage != "interviewing":
        source_index = RecruitingService.APPLICATION_PATH.index(application.stage)
        if source_index >= target_index:
            raise InvalidStateTransition
        application = transition_application_record(
            db,
            principal.organization_id,
            application.id,
            RecruitingService.APPLICATION_PATH[source_index + 1],
            expected_version=application.version,
            actor_user_id=principal.user_id,
            trace_id=trace_id,
        )
    return application


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


def _idempotency(request: Request, value: str | None) -> str | JSONResponse:
    if not value or len(value) > 255:
        return problem(request, 428, "idempotency_key_required", "Idempotency-Key is required.")
    return value


def _expected_version(request: Request, value: str | None) -> int | JSONResponse:
    if value is None:
        return problem(request, 428, "precondition_required", "A quoted If-Match version is required.")
    matched = ETAG.fullmatch(value)
    if not matched:
        return problem(request, 422, "validation_failed", "If-Match must be a quoted integer.")
    return int(matched.group(1))


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _interview_time_in_past(value: datetime) -> bool:
    return _aware(value).astimezone(timezone.utc) <= datetime.now(timezone.utc)


def _has_assignment_access(principal: Principal) -> bool:
    return principal.active and bool(principal.roles & INTERVIEW_ACCESS_ROLES)


def _load_application_for_management(db, principal: Principal, application_id: UUID):
    return db.scalar(
        select(Application)
        .join(Job, and_(Job.organization_id == Application.organization_id, Job.id == Application.job_id))
        .join(Candidate, and_(Candidate.organization_id == Application.organization_id, Candidate.id == Application.candidate_id))
        .where(
            Application.organization_id == principal.organization_id,
            Application.id == application_id,
            Candidate.deleted_at.is_(None),
            AUTH.job_predicate(principal, RecruitingAction.TRANSITION, Job),
        )
    )


def _load_interview(db, principal: Principal, interview_id: UUID, *, manage: bool = False):
    action = RecruitingAction.TRANSITION if manage else RecruitingAction.READ
    assigned = (
        exists().where(
            InterviewParticipant.organization_id == Interview.organization_id,
            InterviewParticipant.interview_id == Interview.id,
            InterviewParticipant.user_id == principal.user_id,
        )
        if _has_assignment_access(principal)
        else False
    )
    scope = AUTH.job_predicate(principal, action, Job)
    if manage:
        allowed = scope
    else:
        allowed = or_(scope, assigned)
    return db.scalar(
        select(Interview)
        .join(Application, and_(Application.organization_id == Interview.organization_id, Application.id == Interview.application_id))
        .join(Job, and_(Job.organization_id == Application.organization_id, Job.id == Application.job_id))
        .join(Candidate, and_(Candidate.organization_id == Application.organization_id, Candidate.id == Application.candidate_id))
        .where(
            Interview.organization_id == principal.organization_id,
            Interview.id == interview_id,
            Candidate.deleted_at.is_(None),
            allowed,
        )
    )


def _load_interview_for_feedback_summary(db, principal: Principal, interview_id: UUID):
    if not principal.active:
        return None
    allowed = []
    if "recruiting_admin" in principal.roles:
        allowed.append(True)
    if "recruiter" in principal.roles:
        allowed.append(Application.owner_id == principal.user_id)
    if "hiring_manager" in principal.roles:
        allowed.append(
            exists().where(
                JobCollaborator.organization_id == Job.organization_id,
                JobCollaborator.job_id == Job.id,
                JobCollaborator.user_id == principal.user_id,
                JobCollaborator.access_role == "job_manager",
            )
        )
    if _has_assignment_access(principal):
        allowed.append(
            exists().where(
                InterviewParticipant.organization_id == Interview.organization_id,
                InterviewParticipant.interview_id == Interview.id,
                InterviewParticipant.user_id == principal.user_id,
            )
        )
    if not allowed:
        return None
    return db.scalar(
        select(Interview)
        .join(
            Application,
            and_(
                Application.organization_id == Interview.organization_id,
                Application.id == Interview.application_id,
            ),
        )
        .join(
            Job,
            and_(
                Job.organization_id == Application.organization_id,
                Job.id == Application.job_id,
            ),
        )
        .join(Candidate, and_(Candidate.organization_id == Application.organization_id, Candidate.id == Application.candidate_id))
        .where(
            Interview.organization_id == principal.organization_id,
            Interview.id == interview_id,
            Candidate.deleted_at.is_(None),
            or_(*allowed),
        )
    )


def _load_interview_material_context(db, principal: Principal, interview_id: UUID):
    assigned = (
        exists().where(
            InterviewParticipant.organization_id == Interview.organization_id,
            InterviewParticipant.interview_id == Interview.id,
            InterviewParticipant.user_id == principal.user_id,
            InterviewParticipant.role == "interviewer",
            InterviewParticipant.task_status != "cancelled",
        )
        if _has_assignment_access(principal)
        else False
    )
    return db.execute(
        select(Interview, Application, Job)
        .join(
            Application,
            and_(
                Application.organization_id == Interview.organization_id,
                Application.id == Interview.application_id,
            ),
        )
        .join(
            Job,
            and_(
                Job.organization_id == Application.organization_id,
                Job.id == Application.job_id,
            ),
        )
        .join(Candidate, and_(Candidate.organization_id == Application.organization_id, Candidate.id == Application.candidate_id))
        .where(
            Interview.organization_id == principal.organization_id,
            Interview.id == interview_id,
            Candidate.deleted_at.is_(None),
            or_(AUTH.job_predicate(principal, RecruitingAction.READ, Job), assigned),
        )
    ).one_or_none()


def _schedule_conflicts(
    db,
    organization_id: UUID,
    participant_ids: list[UUID],
    candidate_id: UUID,
    starts_at: datetime,
    ends_at: datetime,
    *,
    exclude_interview_id: UUID | None = None,
    buffer_minutes: int = 15,
) -> tuple[list[UUID], list[UUID]]:
    lower_bound = starts_at - timedelta(minutes=buffer_minutes)
    upper_bound = ends_at + timedelta(minutes=buffer_minutes)
    statement = (
        select(Interview)
        .join(
            Application,
            and_(
                Application.organization_id == Interview.organization_id,
                Application.id == Interview.application_id,
            ),
        )
        .join(
            InterviewParticipant,
            and_(
                InterviewParticipant.organization_id == Interview.organization_id,
                InterviewParticipant.interview_id == Interview.id,
            ),
        )
        .where(
            Interview.organization_id == organization_id,
            Interview.status != "cancelled",
            or_(
                InterviewParticipant.user_id.in_(participant_ids),
                Application.candidate_id == candidate_id,
            ),
            Interview.starts_at < upper_bound,
            Interview.ends_at > lower_bound,
        )
        .distinct()
    )
    if exclude_interview_id is not None:
        statement = statement.where(Interview.id != exclude_interview_id)
    hard: list[UUID] = []
    soft: list[UUID] = []
    for interview in db.scalars(statement).all():
        existing_start = _aware(interview.starts_at)
        existing_end = _aware(interview.ends_at)
        if starts_at < existing_end and existing_start < ends_at:
            hard.append(interview.id)
        elif min(abs(starts_at - existing_end), abs(existing_start - ends_at)) < timedelta(minutes=buffer_minutes):
            soft.append(interview.id)
    return hard, soft


def _realtime_availability_conflicts(
    request: Request,
    db,
    organization_id: UUID,
    participant_ids: list[UUID],
    starts_at: datetime,
    ends_at: datetime,
    *,
    exclude_interview_id: UUID | None = None,
    buffer_minutes: int = 15,
) -> tuple[list[UUID], list[UUID], list[UUID]]:
    provider = getattr(
        request.app.state,
        "interview_availability_provider",
        INTERNAL_AVAILABILITY_PROVIDER,
    )
    try:
        rows = privacy_safe_availability(
            provider.availability(
                db=db,
                organization_id=organization_id,
                participant_ids=participant_ids,
                starts_at=starts_at,
                ends_at=ends_at,
                buffer_minutes=buffer_minutes,
                exclude_interview_id=exclude_interview_id,
            ),
            participant_ids,
        )
    except Exception:
        return [], [], list(participant_ids)

    hard: list[UUID] = []
    soft: list[UUID] = []
    unconfirmed: list[UUID] = []
    for row in rows:
        participant_id = UUID(row["participant_id"])
        if row["status"] != "confirmed":
            unconfirmed.append(participant_id)
        for block in row["busy"]:
            busy_start = datetime.fromisoformat(block["starts_at"])
            busy_end = datetime.fromisoformat(block["ends_at"])
            if starts_at < busy_end and busy_start < ends_at:
                hard.append(participant_id)
                break
            if min(abs(starts_at - busy_end), abs(busy_start - ends_at)) < timedelta(minutes=buffer_minutes):
                soft.append(participant_id)
                break
    return list(dict.fromkeys(hard)), list(dict.fromkeys(soft)), list(dict.fromkeys(unconfirmed))


def _interview_data(db, interview: Interview) -> dict:
    application = db.get(Application, interview.application_id)
    candidate = db.get(Candidate, application.candidate_id)
    job = db.get(Job, application.job_id)
    rows = db.execute(
        select(InterviewParticipant, User)
        .join(
            User,
            and_(
                User.organization_id == InterviewParticipant.organization_id,
                User.id == InterviewParticipant.user_id,
            ),
        )
        .where(
            InterviewParticipant.organization_id == interview.organization_id,
            InterviewParticipant.interview_id == interview.id,
        )
        .order_by(User.display_name, User.id)
    ).all()
    return {
        "id": str(interview.id),
        "application_id": str(interview.application_id),
        "candidate": {
            "id": str(candidate.id),
            "display_name": candidate.display_name,
            "current_title": candidate.current_title,
        },
        "job": {"id": str(job.id), "title": job.title},
        "round_name": interview.round_name,
        "method": interview.method,
        "timezone": interview.timezone,
        "starts_at": _aware(interview.starts_at).isoformat(),
        "ends_at": _aware(interview.ends_at).isoformat(),
        "location": interview.location,
        "meeting_url": interview.meeting_url,
        "status": interview.status,
        "notification_status": interview.notification_status,
        "invitation_status": interview.invitation_status,
        "participants": [
            {
                "user_id": str(participant.user_id),
                "display_name": user.display_name,
                "role": participant.role,
                "required_feedback": participant.required_feedback,
                "attendance_status": participant.attendance_status,
                "task_status": participant.task_status,
            }
            for participant, user in rows
        ],
        "version": interview.version,
        "calendar_sequence": interview.calendar_sequence,
        "updated_at": _aware(interview.updated_at).isoformat(),
    }


def _schedule_snapshot(interview: Interview) -> dict:
    return {
        "round_name": interview.round_name,
        "method": interview.method,
        "timezone": interview.timezone,
        "starts_at": _aware(interview.starts_at).isoformat(),
        "ends_at": _aware(interview.ends_at).isoformat(),
        "location": interview.location,
        "meeting_url": interview.meeting_url,
    }


def _advance_application_if_interviews_complete(
    db,
    organization_id: UUID,
    application_id: UUID,
    current_round_name: str,
    *,
    actor_user_id: UUID,
    trace_id: str,
) -> tuple[bool, str | None]:
    candidate_id = _lock_application_candidate_retention(
        db, organization_id, application_id
    )
    application = db.scalar(
        select(Application)
        .where(
            Application.organization_id == organization_id,
            Application.id == application_id,
        )
        .with_for_update()
    )
    if application is None or application.candidate_id != candidate_id:
        raise InvalidStateTransition
    template_rounds = db.scalar(
        select(WorkflowTemplate.rounds)
        .join(
            Job,
            and_(
                Job.organization_id == WorkflowTemplate.organization_id,
                Job.workflow_template_id == WorkflowTemplate.id,
            ),
        )
        .where(
            Job.organization_id == organization_id,
            Job.id == application.job_id,
        )
    )
    normalized_rounds = normalized_workflow_rounds(template_rounds)
    managed_round = normalized_rounds is not None and current_round_name in normalized_rounds
    status_query = select(Interview.status).where(
            Interview.organization_id == organization_id,
            Interview.application_id == application_id,
            Interview.status.not_in(("cancelled", "no_show")),
        )
    if managed_round:
        status_query = status_query.where(Interview.round_name == current_round_name)
    active_statuses = db.scalars(status_query).all()
    if not active_statuses or any(status != "feedback_completed" for status in active_statuses):
        return False, None
    next_round_name = None
    if managed_round:
        round_index = normalized_rounds.index(current_round_name)
        if round_index + 1 < len(normalized_rounds):
            next_round_name = normalized_rounds[round_index + 1]
    if application.stage == "interviewing":
        if next_round_name is None:
            transition_application_record(
                db,
                organization_id,
                application.id,
                "decision",
                expected_version=application.version,
                actor_user_id=actor_user_id,
                trace_id=trace_id,
            )
        else:
            application.stage = "interview_pending"
            application.version += 1
            application.updated_at = datetime.now(timezone.utc)
            payload = {
                "from_stage": "interviewing",
                "to_stage": "interview_pending",
                "next_round_name": next_round_name,
                "application_advanced": True,
            }
            db.add(ApplicationStageEvent(
                organization_id=organization_id,
                application_id=application.id,
                actor_user_id=actor_user_id,
                event_type="application.stage_changed",
                payload=payload,
            ))
            db.add(AuditLog(
                organization_id=organization_id,
                actor_user_id=actor_user_id,
                event_type="application.stage_changed",
                outcome="success",
                resource_type="application",
                resource_id=application.id,
                trace_id=trace_id,
                metadata_json={
                    "from_stage": "interviewing",
                    "to_stage": "interview_pending",
                    "next_round_name": next_round_name,
                },
            ))
            db.flush()
            recalculate_candidate_retention(db, organization_id, application.candidate_id)
    elif application.stage not in {"decision", "interview_pending"}:
        raise InvalidStateTransition
    return True, next_round_name


def _lock_application_candidate_retention(db, organization_id, application_id):
    candidate_id = db.scalar(
        select(Application.candidate_id).where(
            Application.organization_id == organization_id,
            Application.id == application_id,
        )
    )
    if candidate_id is None:
        raise InvalidStateTransition
    lock_active_candidate(db, organization_id, candidate_id)
    return candidate_id


def _participant_role_predicate():
    return exists().where(
        UserRole.user_id == User.id,
        UserRole.role.in_(INTERVIEW_PARTICIPANT_ROLES),
    )


def _validate_participants(db, organization_id: UUID, participant_ids: list[UUID]) -> bool:
    return len(
        db.scalars(
            select(User.id).where(
                User.organization_id == organization_id,
                User.id.in_(participant_ids),
                User.status == UserStatus.ACTIVE,
                _participant_role_predicate(),
            )
        ).all()
    ) == len(participant_ids)


def _lock_participants(db, organization_id: UUID, participant_ids: list[UUID]) -> bool:
    ordered_ids = sorted(participant_ids, key=str)
    locked_ids = db.scalars(
        select(User.id)
        .where(
            User.organization_id == organization_id,
            User.id.in_(ordered_ids),
            User.status == UserStatus.ACTIVE,
            _participant_role_predicate(),
        )
        .order_by(User.id)
        .with_for_update()
    ).all()
    return len(locked_ids) == len(ordered_ids)


def _calendar_contact_snapshot(db, organization_id: UUID, organizer_id: UUID, participant_ids: list[UUID]):
    organizer = db.scalar(
        select(User).where(
            User.organization_id == organization_id,
            User.id == organizer_id,
        )
    )
    attendees = db.scalars(
        select(User)
        .where(
            User.organization_id == organization_id,
            User.id.in_(participant_ids),
        )
        .order_by(User.id)
    ).all()
    if organizer is None or len(attendees) != len(participant_ids):
        raise InvalidStateTransition
    return (
        {"name": organizer.display_name, "email": organizer.email},
        [{"name": user.display_name, "email": user.email} for user in attendees],
    )


def _assigned_participant(db, principal: Principal, interview_id: UUID, *, for_update: bool = False):
    if not _has_assignment_access(principal):
        return None
    statement = select(InterviewParticipant).where(
        InterviewParticipant.organization_id == principal.organization_id,
        InterviewParticipant.interview_id == interview_id,
        InterviewParticipant.user_id == principal.user_id,
        InterviewParticipant.role == "interviewer",
    )
    if for_update:
        statement = statement.with_for_update()
    return db.scalar(statement)


def _feedback_data(feedback: InterviewFeedback) -> dict:
    return {
        "id": str(feedback.id),
        "interview_id": str(feedback.interview_id),
        "author_id": str(feedback.author_id),
        "status": feedback.status,
        "ratings": feedback.ratings,
        "strengths": feedback.strengths,
        "risks": feedback.risks,
        "conclusion": feedback.conclusion,
        "notes": feedback.notes,
        "version": feedback.version,
        "submitted_at": _aware(feedback.submitted_at).isoformat() if feedback.submitted_at else None,
        "updated_at": _aware(feedback.updated_at).isoformat(),
    }


def _submitted_feedback_data(feedback: InterviewFeedback, author: User) -> dict:
    return {
        "id": str(feedback.id),
        "interview_id": str(feedback.interview_id),
        "author": {"id": str(author.id), "display_name": author.display_name},
        "status": feedback.status,
        "ratings": feedback.ratings,
        "strengths": feedback.strengths,
        "risks": feedback.risks,
        "conclusion": feedback.conclusion,
        "notes": feedback.notes,
        "submitted_at": _aware(feedback.submitted_at).isoformat(),
        "version": feedback.version,
    }


def _feedback_document(feedback: InterviewFeedback) -> dict:
    return {
        "ratings": feedback.ratings,
        "strengths": feedback.strengths,
        "risks": feedback.risks,
        "conclusion": feedback.conclusion,
        "notes": feedback.notes,
        "status": feedback.status,
        "submitted_at": _aware(feedback.submitted_at).isoformat() if feedback.submitted_at else None,
    }


@router.get("/applications/{application_id}/interview-participant-options", response_model=DataCollection)
def list_interview_participant_options(application_id: UUID, request: Request):
    principal = _principal(request)
    if isinstance(principal, JSONResponse):
        return principal
    with request.app.state.identity_store.sync_session() as db:
        if _load_application_for_management(db, principal, application_id) is None:
            return _denied(request)
        rows = db.execute(
            select(User.id, User.display_name, UserRole.role)
            .join(UserRole, UserRole.user_id == User.id)
            .where(
                User.organization_id == principal.organization_id,
                User.status == UserStatus.ACTIVE,
                UserRole.role.in_(INTERVIEW_PARTICIPANT_ROLES),
            )
            .order_by(User.display_name, User.id, UserRole.role)
        ).all()
        options = []
        for user_id, display_name, role in rows:
            if not options or options[-1]["id"] != str(user_id):
                options.append({"id": str(user_id), "display_name": display_name, "roles": []})
            options[-1]["roles"].append(role)
        response = JSONResponse({"data": options, "meta": {"count": len(options)}})
        response.headers["Cache-Control"] = "no-store"
        return response


@router.get("/interview-availability", response_model=DataResource)
def get_interview_availability(
    request: Request,
    date_from: datetime = Query(alias="from"),
    date_to: datetime = Query(alias="to"),
    participant_ids: list[str] = Query(),
    timezone_name: str = Query(alias="timezone"),
    buffer: int = Query(15, ge=0, le=240),
    exclude: UUID | None = None,
):
    principal = _principal(request)
    if isinstance(principal, JSONResponse):
        return principal
    if not principal.roles.intersection({"recruiting_admin", "recruiter", "hiring_manager"}):
        return _denied(request)
    try:
        ZoneInfo(timezone_name)
        parsed_ids = [UUID(value) for item in participant_ids for value in item.split(",") if value]
    except (ValueError, ZoneInfoNotFoundError):
        return problem(request, 422, "validation_failed", "The request could not be completed.")
    if (
        not parsed_ids
        or len(parsed_ids) > 20
        or len(set(parsed_ids)) != len(parsed_ids)
        or date_to <= date_from
        or date_to - date_from > timedelta(days=93)
    ):
        return problem(request, 422, "validation_failed", "The request could not be completed.")
    with request.app.state.identity_store.sync_session() as db:
        if not _validate_participants(db, principal.organization_id, parsed_ids):
            return problem(request, 422, "validation_failed", "The request could not be completed.")
        provider = getattr(request.app.state, "interview_availability_provider", INTERNAL_AVAILABILITY_PROVIDER)
        try:
            provider_rows = provider.availability(
                db=db,
                organization_id=principal.organization_id,
                participant_ids=parsed_ids,
                starts_at=date_from,
                ends_at=date_to,
                buffer_minutes=buffer,
                exclude_interview_id=exclude,
            )
            participants = privacy_safe_availability(provider_rows, parsed_ids)
        except Exception:
            return problem(request, 503, "availability_unavailable", "Availability could not be confirmed.")
    response = JSONResponse({
        "data": {
            "from": _aware(date_from).isoformat(),
            "to": _aware(date_to).isoformat(),
            "timezone": timezone_name,
            "buffer_minutes": buffer,
            "participants": participants,
        }
    })
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get("/interviews", response_model=DataCollection)
def list_interviews(
    request: Request,
    date_from: datetime | None = Query(default=None, alias="from"),
    date_to: datetime | None = Query(default=None, alias="to"),
    interviewer_id: UUID | None = None,
    status: str | None = None,
    cursor: str | None = None,
    limit: int = Query(50, ge=1, le=100),
):
    principal = _principal(request)
    if isinstance(principal, JSONResponse):
        return principal
    assigned = (
        exists().where(
            InterviewParticipant.organization_id == Interview.organization_id,
            InterviewParticipant.interview_id == Interview.id,
            InterviewParticipant.user_id == principal.user_id,
        )
        if _has_assignment_access(principal)
        else False
    )
    cursor_scope = hashlib.sha256(
        json.dumps(
            {
                "from": _aware(date_from).astimezone(timezone.utc).isoformat() if date_from else None,
                "interviewer_id": str(interviewer_id) if interviewer_id else None,
                "status": status,
                "to": _aware(date_to).astimezone(timezone.utc).isoformat() if date_to else None,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    cursor_sort = f"interviews:starts_at:{cursor_scope}"
    with request.app.state.identity_store.sync_session() as db:
        statement = (
            select(Interview)
            .join(Application, and_(Application.organization_id == Interview.organization_id, Application.id == Interview.application_id))
            .join(Job, and_(Job.organization_id == Application.organization_id, Job.id == Application.job_id))
            .join(Candidate, and_(Candidate.organization_id == Application.organization_id, Candidate.id == Application.candidate_id))
            .where(
                Interview.organization_id == principal.organization_id,
                Candidate.deleted_at.is_(None),
                or_(AUTH.job_predicate(principal, RecruitingAction.READ, Job), assigned),
            )
        )
        if date_from is not None:
            statement = statement.where(Interview.ends_at >= date_from)
        if date_to is not None:
            statement = statement.where(Interview.starts_at <= date_to)
        if status is not None:
            statement = statement.where(Interview.status == status)
        if interviewer_id is not None:
            statement = statement.where(
                exists().where(
                    InterviewParticipant.organization_id == Interview.organization_id,
                    InterviewParticipant.interview_id == Interview.id,
                    InterviewParticipant.user_id == interviewer_id,
                )
            )
        if cursor:
            try:
                decoded = request.app.state.recruiting_cursor.decode(
                    cursor,
                    str(principal.organization_id),
                    cursor_sort,
                )
                starts_at = datetime.fromisoformat(decoded["value"])
                statement = statement.where(
                    or_(
                        Interview.starts_at > starts_at,
                        and_(Interview.starts_at == starts_at, Interview.id > UUID(decoded["id"])),
                    )
                )
            except Exception:
                return problem(request, 422, "validation_failed", "The request could not be completed.")
        interviews = db.scalars(
            statement.order_by(Interview.starts_at, Interview.id).distinct().limit(limit + 1)
        ).all()
        next_cursor = None
        if len(interviews) > limit:
            last = interviews[limit - 1]
            next_cursor = request.app.state.recruiting_cursor.encode(
                str(principal.organization_id),
                cursor_sort,
                _aware(last.starts_at).isoformat(),
                str(last.id),
            )
            interviews = interviews[:limit]
        response = JSONResponse(
            {
                "data": [_interview_data(db, interview) for interview in interviews],
                "meta": {"limit": limit, "next_cursor": next_cursor},
            }
        )
        response.headers["Cache-Control"] = "no-store"
        return response


@router.post("/interviews", status_code=201, response_model=DataResource)
def create_interview(payload: InterviewCreate, request: Request, idempotency_key: str | None = Header(None)):
    principal = _principal(request)
    key = _idempotency(request, idempotency_key)
    for value in (principal, key):
        if isinstance(value, JSONResponse):
            return value
    command = payload.model_dump()
    with request.app.state.identity_store.sync_session() as db:
        application = _load_application_for_management(db, principal, payload.application_id)
        if application is None:
            return _denied(request)
        participant_ids = [item.user_id for item in payload.participants]
        if not _validate_participants(db, principal.organization_id, participant_ids):
            return _denied(request)
        try:
            def action():
                candidate_id = _lock_application_candidate_retention(
                    db, principal.organization_id, application.id
                )
                locked_application = db.scalar(
                    select(Application)
                    .where(
                        Application.organization_id == principal.organization_id,
                        Application.id == application.id,
                    )
                    .with_for_update()
                )
                if (
                    locked_application is None
                    or locked_application.candidate_id != candidate_id
                ):
                    raise InvalidStateTransition
                if locked_application.stage == "interview_pending":
                    managed, expected_round = _expected_interview_round(
                        db, locked_application
                    )
                    if managed and expected_round is None:
                        raise InvalidStateTransition
                    if managed and payload.round_name != expected_round:
                        raise InterviewRoundMismatch
                if not _lock_participants(db, principal.organization_id, participant_ids):
                    raise InvalidStateTransition
                if _interview_time_in_past(payload.starts_at):
                    raise InterviewTimeInPast
                calendar_organizer, calendar_attendees = _calendar_contact_snapshot(
                    db,
                    principal.organization_id,
                    principal.user_id,
                    participant_ids,
                )
                hard, soft = _schedule_conflicts(
                    db,
                    principal.organization_id,
                    participant_ids,
                    candidate_id,
                    payload.starts_at,
                    payload.ends_at,
                )
                calendar_hard, calendar_soft, _ = _realtime_availability_conflicts(
                    request,
                    db,
                    principal.organization_id,
                    participant_ids,
                    payload.starts_at,
                    payload.ends_at,
                )
                if hard or calendar_hard:
                    raise ScheduleConflict("hard", [*hard, *calendar_hard])
                if (soft or calendar_soft) and not payload.allow_soft_conflict:
                    raise ScheduleConflict("soft", [*soft, *calendar_soft])
                interview = Interview(
                    organization_id=principal.organization_id,
                    application_id=locked_application.id,
                    round_name=payload.round_name,
                    method=payload.method,
                    timezone=payload.timezone,
                    starts_at=payload.starts_at,
                    ends_at=payload.ends_at,
                    location=payload.location,
                    meeting_url=payload.meeting_url,
                    status="scheduled",
                    notification_status="not_sent",
                    invitation_status="artifact_ready",
                    owner_id=locked_application.owner_id,
                    created_by=principal.user_id,
                    calendar_organizer=calendar_organizer,
                    calendar_attendees=calendar_attendees,
                )
                db.add(interview)
                db.flush()
                db.add_all(
                    [
                        InterviewParticipant(
                            organization_id=principal.organization_id,
                            interview_id=interview.id,
                            user_id=item.user_id,
                            role=item.role,
                            required_feedback=item.required_feedback,
                        )
                        for item in payload.participants
                    ]
                )
                locked_application = _advance_application_to_interviewing(
                    db,
                    locked_application,
                    principal=principal,
                    trace_id=request.state.trace_id,
                )
                db.add(
                    InterviewEvent(
                        organization_id=principal.organization_id,
                        interview_id=interview.id,
                        actor_user_id=principal.user_id,
                        event_type="interview.created",
                        payload={
                            "participant_ids": [str(value) for value in participant_ids],
                            "soft_conflict_override": bool(soft and payload.allow_soft_conflict),
                        },
                    )
                )
                db.add(
                    AuditLog(
                        organization_id=principal.organization_id,
                        actor_user_id=principal.user_id,
                        event_type="interview.created",
                        outcome="success",
                        trace_id=request.state.trace_id,
                        metadata_json={"interview_id": str(interview.id), "application_id": str(locked_application.id)},
                    )
                )
                db.flush()
                recalculate_candidate_retention(
                    db, principal.organization_id, candidate_id
                )
                schedule_interview_sync(db, interview, "create")
                return 201, {"data": _interview_data(db, interview)}

            status_code, body = persisted_idempotent(
                db,
                principal.organization_id,
                principal.user_id,
                "interview.create",
                key,
                command,
                action,
            )
            db.commit()
            response = JSONResponse(body, status_code=status_code)
            response.headers["ETag"] = f'"{body["data"]["version"]}"'
            return response
        except IdempotencyConflict:
            db.rollback()
            return problem(request, 409, "idempotency_conflict", "The idempotency key was reused with another request.")
        except ScheduleConflict as error:
            db.rollback()
            return problem(
                request,
                409,
                f"schedule_{error.kind}_conflict",
                "One or more interviewers are unavailable.",
            )
        except InterviewTimeInPast:
            db.rollback()
            return problem(request, 422, "interview_time_in_past", "The interview must start in the future.")
        except InterviewRoundMismatch:
            db.rollback()
            return problem(request, 422, "interview_round_invalid", "The interview round is invalid.")
        except CandidateUnavailable:
            db.rollback()
            return _denied(request)
        except (InvalidStateTransition, ValueError):
            db.rollback()
            return problem(request, 409, "invalid_state_transition", "The application cannot be scheduled for an interview.")


@router.get("/interviews/{interview_id}", response_model=DataResource)
def get_interview(interview_id: UUID, request: Request):
    principal = _principal(request)
    if isinstance(principal, JSONResponse):
        return principal
    with request.app.state.identity_store.sync_session() as db:
        interview = _load_interview(db, principal, interview_id)
        if interview is None:
            return _denied(request)
        response = JSONResponse({"data": _interview_data(db, interview)})
        response.headers["ETag"] = f'"{interview.version}"'
        response.headers["Cache-Control"] = "no-store"
        return response


@router.patch("/interviews/{interview_id}", response_model=DataResource)
def patch_interview(interview_id: UUID, payload: InterviewPatch, request: Request, if_match: str | None = Header(None)):
    principal = _principal(request)
    expected = _expected_version(request, if_match)
    for value in (principal, expected):
        if isinstance(value, JSONResponse):
            return value
    with request.app.state.identity_store.sync_session() as db:
        interview = _load_interview(db, principal, interview_id, manage=True)
        if interview is None:
            return _denied(request)
        application_id = interview.application_id
        try:
            candidate_id = _lock_application_candidate_retention(
                db, principal.organization_id, application_id
            )
        except InvalidStateTransition:
            return _denied(request)
        interview = db.scalar(
            select(Interview)
            .where(
                Interview.organization_id == principal.organization_id,
                Interview.id == interview_id,
            )
            .with_for_update()
        )
        if interview is None or interview.application_id != application_id:
            return _denied(request)
        if interview.version != expected:
            return problem(request, 409, "resource_version_conflict", "The interview changed. Refresh and retry.")
        if interview.status not in {"draft", "scheduled", "rescheduled", "confirmed"}:
            return problem(request, 409, "invalid_state_transition", "The interview can no longer be rescheduled.")
        changes = payload.model_dump(exclude_unset=True, exclude={"allow_soft_conflict"})
        if not changes:
            return problem(request, 422, "validation_failed", "At least one interview field must change.")
        starts_at = payload.starts_at or _aware(interview.starts_at)
        ends_at = payload.ends_at or _aware(interview.ends_at)
        if ends_at <= starts_at:
            return problem(request, 422, "validation_failed", "ends_at must be after starts_at.")
        if _interview_time_in_past(starts_at):
            return problem(request, 422, "interview_time_in_past", "The interview must start in the future.")
        final_method = changes.get("method", interview.method)
        final_location = changes.get("location", interview.location)
        final_meeting_url = changes.get("meeting_url", interview.meeting_url)
        if final_method == "video" and not final_meeting_url:
            return problem(request, 422, "validation_failed", "meeting_url is required for video interviews.")
        if final_method == "onsite" and not final_location:
            return problem(request, 422, "validation_failed", "location is required for onsite interviews.")
        participants = payload.participants
        if participants is None:
            participant_ids = list(
                db.scalars(
                    select(InterviewParticipant.user_id).where(
                        InterviewParticipant.organization_id == principal.organization_id,
                        InterviewParticipant.interview_id == interview.id,
                    )
                ).all()
            )
        else:
            participant_ids = [item.user_id for item in participants]
            if not _validate_participants(db, principal.organization_id, participant_ids):
                return _denied(request)
        if not _lock_participants(db, principal.organization_id, participant_ids):
            return _denied(request)
        calendar_attendees = None
        if participants is not None:
            _, calendar_attendees = _calendar_contact_snapshot(
                db,
                principal.organization_id,
                interview.created_by,
                participant_ids,
            )
        hard, soft = _schedule_conflicts(
            db,
            principal.organization_id,
            participant_ids,
            candidate_id,
            starts_at,
            ends_at,
            exclude_interview_id=interview.id,
        )
        calendar_hard, calendar_soft, _ = _realtime_availability_conflicts(
            request,
            db,
            principal.organization_id,
            participant_ids,
            starts_at,
            ends_at,
            exclude_interview_id=interview.id,
        )
        if hard or calendar_hard:
            return problem(request, 409, "schedule_hard_conflict", "One or more interviewers are unavailable.")
        if (soft or calendar_soft) and not payload.allow_soft_conflict:
            return problem(request, 409, "schedule_soft_conflict", "One or more interviewers have an adjacent interview.")
        previous = _schedule_snapshot(interview)
        try:
            for field, value in changes.items():
                if field != "participants":
                    setattr(interview, field, value)
            if participants is not None:
                db.execute(
                    delete(InterviewParticipant).where(
                        InterviewParticipant.organization_id == principal.organization_id,
                        InterviewParticipant.interview_id == interview.id,
                    )
                )
                db.add_all(
                    [
                        InterviewParticipant(
                            organization_id=principal.organization_id,
                            interview_id=interview.id,
                            user_id=item.user_id,
                            role=item.role,
                            required_feedback=item.required_feedback,
                        )
                        for item in participants
                    ]
                )
                interview.calendar_attendees = calendar_attendees
            interview.status = "rescheduled"
            interview.version += 1
            interview.calendar_sequence += 1
            interview.updated_at = datetime.now(timezone.utc)
            current = _schedule_snapshot(interview)
            db.add(
                InterviewEvent(
                    organization_id=principal.organization_id,
                    interview_id=interview.id,
                    actor_user_id=principal.user_id,
                    event_type="interview.rescheduled",
                    payload={
                        "previous": previous,
                        "current": current,
                        "changed_fields": sorted(changes),
                        "soft_conflict_override": bool(soft and payload.allow_soft_conflict),
                    },
                )
            )
            db.add(
                AuditLog(
                    organization_id=principal.organization_id,
                    actor_user_id=principal.user_id,
                    event_type="interview.rescheduled",
                    outcome="success",
                    trace_id=request.state.trace_id,
                    metadata_json={"interview_id": str(interview.id), "changed_fields": sorted(changes)},
                )
            )
            db.flush()
            recalculate_candidate_retention(
                db, principal.organization_id, candidate_id
            )
            schedule_interview_sync(db, interview, "update")
            body = {"data": _interview_data(db, interview)}
            db.commit()
            response = JSONResponse(body)
            response.headers["ETag"] = f'"{body["data"]["version"]}"'
            return response
        except Exception:
            db.rollback()
            raise


@router.post("/interview-conflicts", response_model=DataResource)
def check_new_interview_conflicts(payload: NewInterviewConflictInput, request: Request):
    principal = _principal(request)
    if isinstance(principal, JSONResponse):
        return principal
    with request.app.state.identity_store.sync_session() as db:
        application = _load_application_for_management(db, principal, payload.application_id)
        if application is None:
            return _denied(request)
        if not _validate_participants(db, principal.organization_id, payload.participant_ids):
            return _denied(request)
        if _interview_time_in_past(payload.starts_at):
            return problem(request, 422, "interview_time_in_past", "The interview must start in the future.")
        hard, soft = _schedule_conflicts(
            db,
            principal.organization_id,
            payload.participant_ids,
            application.candidate_id,
            payload.starts_at,
            payload.ends_at,
            buffer_minutes=payload.buffer_minutes,
        )
        calendar_hard, calendar_soft, unconfirmed = _realtime_availability_conflicts(
            request,
            db,
            principal.organization_id,
            payload.participant_ids,
            payload.starts_at,
            payload.ends_at,
            buffer_minutes=payload.buffer_minutes,
        )
        return {"data": {
            "hard": [str(value) for value in hard],
            "soft": [str(value) for value in soft],
            "calendar_hard": [str(value) for value in calendar_hard],
            "calendar_soft": [str(value) for value in calendar_soft],
            "unconfirmed": [str(value) for value in unconfirmed],
        }}


@router.post("/interviews/{interview_id}/conflicts", response_model=DataResource)
def check_conflicts(interview_id: UUID, payload: ScheduleInput, request: Request):
    principal = _principal(request)
    if isinstance(principal, JSONResponse):
        return principal
    with request.app.state.identity_store.sync_session() as db:
        interview = _load_interview(db, principal, interview_id, manage=True)
        if interview is None:
            return _denied(request)
        if not _validate_participants(db, principal.organization_id, payload.participant_ids):
            return _denied(request)
        if _interview_time_in_past(payload.starts_at):
            return problem(request, 422, "interview_time_in_past", "The interview must start in the future.")
        application = db.get(Application, interview.application_id)
        hard, soft = _schedule_conflicts(
            db,
            principal.organization_id,
            payload.participant_ids,
            application.candidate_id,
            payload.starts_at,
            payload.ends_at,
            exclude_interview_id=interview.id,
            buffer_minutes=payload.buffer_minutes,
        )
        calendar_hard, calendar_soft, unconfirmed = _realtime_availability_conflicts(
            request,
            db,
            principal.organization_id,
            payload.participant_ids,
            payload.starts_at,
            payload.ends_at,
            exclude_interview_id=interview.id,
            buffer_minutes=payload.buffer_minutes,
        )
        return {"data": {
            "hard": [str(value) for value in hard],
            "soft": [str(value) for value in soft],
            "calendar_hard": [str(value) for value in calendar_hard],
            "calendar_soft": [str(value) for value in calendar_soft],
            "unconfirmed": [str(value) for value in unconfirmed],
        }}


@router.post("/interviews/{interview_id}/transitions", response_model=DataResource)
def transition_interview(
    interview_id: UUID,
    payload: InterviewTransition,
    request: Request,
    if_match: str | None = Header(None),
    idempotency_key: str | None = Header(None),
):
    principal = _principal(request)
    expected = _expected_version(request, if_match)
    key = _idempotency(request, idempotency_key)
    for value in (principal, expected, key):
        if isinstance(value, JSONResponse):
            return value
    with request.app.state.identity_store.sync_session() as db:
        interview = _load_interview(db, principal, interview_id, manage=True)
        if interview is None:
            return _denied(request)
        try:
            def action():
                application_id = interview.application_id
                candidate_id = _lock_application_candidate_retention(
                    db, principal.organization_id, application_id
                )
                locked = db.scalar(
                    select(Interview)
                    .where(
                        Interview.organization_id == principal.organization_id,
                        Interview.id == interview.id,
                    )
                    .with_for_update()
                )
                if (
                    locked is None
                    or locked.application_id != application_id
                    or locked.version != expected
                ):
                    raise ResourceVersionConflict
                source = locked.status
                target = payload.target
                allowed = {
                    "draft": {"confirmed", "cancelled"},
                    "scheduled": {"confirmed", "cancelled", "no_show"},
                    "rescheduled": {"confirmed", "cancelled", "no_show"},
                    "confirmed": {"completed", "cancelled", "no_show"},
                }
                if target not in allowed.get(source, set()):
                    raise InvalidStateTransition
                if target == "completed":
                    required = db.scalar(
                        select(InterviewParticipant.id).where(
                            InterviewParticipant.organization_id == principal.organization_id,
                            InterviewParticipant.interview_id == locked.id,
                            InterviewParticipant.required_feedback.is_(True),
                        ).limit(1)
                    )
                    locked.status = "pending_feedback" if required is not None else "feedback_completed"
                else:
                    locked.status = target
                if target in {"cancelled", "no_show"}:
                    locked.calendar_sequence += 1
                    participants = db.scalars(
                        select(InterviewParticipant).where(
                            InterviewParticipant.organization_id == principal.organization_id,
                            InterviewParticipant.interview_id == locked.id,
                        )
                    ).all()
                    for participant in participants:
                        participant.task_status = "cancelled"
                locked.version += 1
                locked.updated_at = datetime.now(timezone.utc)
                db.flush()
                application_advanced = False
                next_round_name = None
                if locked.status == "feedback_completed" or target in {"cancelled", "no_show"}:
                    application_advanced, next_round_name = _advance_application_if_interviews_complete(
                        db,
                        principal.organization_id,
                        locked.application_id,
                        locked.round_name,
                        actor_user_id=principal.user_id,
                        trace_id=request.state.trace_id,
                    )
                db.add(
                    InterviewEvent(
                        organization_id=principal.organization_id,
                        interview_id=locked.id,
                        actor_user_id=principal.user_id,
                        event_type=f"interview.{target}",
                        payload={
                            "from_status": source,
                            "to_status": locked.status,
                            "reason": payload.reason,
                            "application_advanced": application_advanced,
                            "next_round_name": next_round_name,
                        },
                    )
                )
                db.add(
                    AuditLog(
                        organization_id=principal.organization_id,
                        actor_user_id=principal.user_id,
                        event_type=f"interview.{target}",
                        outcome="success",
                        trace_id=request.state.trace_id,
                        metadata_json={"interview_id": str(locked.id), "from_status": source, "to_status": locked.status},
                    )
                )
                db.flush()
                recalculate_candidate_retention(
                    db, principal.organization_id, candidate_id
                )
                if target == "cancelled":
                    schedule_interview_sync(db, locked, "cancel")
                return 200, {"data": _interview_data(db, locked)}

            status_code, body = persisted_idempotent(
                db,
                principal.organization_id,
                principal.user_id,
                f"interview.transition:{interview.id}",
                key,
                {"interview_id": interview.id, **payload.model_dump(), "expected_version": expected},
                action,
            )
            db.commit()
            response = JSONResponse(body, status_code=status_code)
            response.headers["ETag"] = f'"{body["data"]["version"]}"'
            return response
        except CandidateUnavailable:
            db.rollback()
            return _denied(request)
        except ResourceVersionConflict:
            db.rollback()
            return problem(request, 409, "resource_version_conflict", "The interview changed. Refresh and retry.")
        except InvalidStateTransition:
            db.rollback()
            return problem(request, 409, "invalid_state_transition", "The requested interview transition is invalid.")
        except IdempotencyConflict:
            db.rollback()
            return problem(request, 409, "idempotency_conflict", "The idempotency key was reused with another request.")


@router.get("/interviews/{interview_id}/calendar-file")
def download_calendar(interview_id: UUID, request: Request) -> Response:
    principal = _principal(request)
    if isinstance(principal, JSONResponse):
        return principal
    with request.app.state.identity_store.sync_session() as db:
        interview = _load_interview(db, principal, interview_id)
        if interview is None:
            return _denied(request)
        application = db.get(Application, interview.application_id)
        try:
            candidate = lock_active_candidate(db, principal.organization_id, application.candidate_id)
        except CandidateUnavailable:
            db.rollback()
            return _denied(request)
        job = db.get(Job, application.job_id)
        payload = build_calendar_invitation(
            interview_id=interview.id,
            starts_at=_aware(interview.starts_at),
            duration_minutes=int((_aware(interview.ends_at) - _aware(interview.starts_at)).total_seconds() // 60),
            summary=f"{job.title} - {candidate.display_name} - {interview.round_name}",
            location=interview.location or interview.meeting_url or "",
            description=f"Interview method: {interview.method}",
            sequence=interview.calendar_sequence,
            dtstamp=_aware(interview.updated_at),
            status=interview.status,
            organizer=CalendarContact(**interview.calendar_organizer),
            attendees=tuple(
                CalendarContact(**contact)
                for contact in interview.calendar_attendees
            ),
        )
        db.add(
            AuditLog(
                organization_id=principal.organization_id,
                actor_user_id=principal.user_id,
                event_type="interview.calendar_downloaded",
                outcome="success",
                trace_id=request.state.trace_id,
                metadata_json={"interview_id": str(interview.id)},
            )
        )
        db.commit()
        return Response(
            content=payload,
            media_type="text/calendar; charset=utf-8",
            headers={
                "Content-Disposition": f'attachment; filename="interview-{interview.id}.ics"',
                "Cache-Control": "no-store",
            },
        )


@router.get("/interviews/{interview_id}/materials", response_model=DataResource)
def get_interview_materials(interview_id: UUID, request: Request):
    principal = _principal(request)
    if isinstance(principal, JSONResponse):
        return principal
    with request.app.state.identity_store.sync_session() as db:
        context = _load_interview_material_context(db, principal, interview_id)
        if context is None:
            return _denied(request)
        interview, application, job = context
        candidate = db.scalar(
            select(Candidate).where(
                Candidate.organization_id == principal.organization_id,
                Candidate.id == application.candidate_id,
                Candidate.deleted_at.is_(None),
            )
        )
        resume = db.scalar(
            select(Resume).where(
                Resume.organization_id == principal.organization_id,
                Resume.id == application.resume_id,
                Resume.candidate_id == application.candidate_id,
            )
        )
        if candidate is None or resume is None:
            return _denied(request)
        try:
            candidate = lock_active_candidate(db, principal.organization_id, candidate.id)
        except CandidateUnavailable:
            db.rollback()
            return _denied(request)
        preview_source = resume.parsed_text or ""
        if len(preview_source.encode("utf-8")) > MAX_PREVIEW_BYTES:
            return problem(request, 422, "preview_too_large", "The preview is unavailable.")
        preview_text = redact_screening_text(preview_source, candidate_name=candidate.display_name)
        jd = db.scalar(
            select(JobJdVersion)
            .where(
                JobJdVersion.organization_id == principal.organization_id,
                JobJdVersion.job_id == job.id,
            )
            .order_by(JobJdVersion.version_number.desc(), JobJdVersion.id.desc())
        )
        screening = db.scalar(
            select(ScreeningResult)
            .where(
                ScreeningResult.organization_id == principal.organization_id,
                ScreeningResult.application_id == application.id,
            )
            .order_by(ScreeningResult.created_at.desc(), ScreeningResult.id.desc())
        )

        def redacted_items(values):
            return [
                redact_screening_text(value, candidate_name=candidate.display_name)
                for value in values or []
                if isinstance(value, str)
            ]

        jd_content = jd.content if jd is not None and isinstance(jd.content, dict) else {}
        description = jd_content.get("description", jd_content.get("text"))
        if not isinstance(description, str):
            description = None
        body = {
            "data": {
                "interview_id": str(interview.id),
                "candidate": {
                    "id": str(candidate.id),
                    "display_name": candidate.display_name,
                    "current_title": candidate.current_title,
                    "location": candidate.location,
                },
                "job": {"id": str(job.id), "title": job.title},
                "jd": None
                if jd is None
                else {"version_number": jd.version_number, "description": description},
                "resume": {"id": str(resume.id), "preview_text": preview_text},
                "screening": None
                if screening is None
                else {
                    "id": str(screening.id),
                    "required_missing": redacted_items(screening.required_missing),
                    "risks": redacted_items(screening.risks),
                    "questions": redacted_items(screening.questions),
                },
            }
        }
        db.add(
            AuditLog(
                organization_id=principal.organization_id,
                actor_user_id=principal.user_id,
                event_type="interview.materials_viewed",
                outcome="success",
                trace_id=request.state.trace_id,
                metadata_json={"interview_id": str(interview.id)},
            )
        )
        db.commit()
        response = JSONResponse(body)
        response.headers["Cache-Control"] = "no-store"
        return response


@router.get(
    "/interviews/{interview_id}/resume-file",
    responses={200: {"content": {"application/octet-stream": {"schema": {"type": "string", "format": "binary"}}}}},
)
def get_interview_resume_file(interview_id: UUID, request: Request, download: bool = Query(False)):
    principal = _principal(request)
    if isinstance(principal, JSONResponse):
        return principal
    with request.app.state.identity_store.sync_session() as db:
        context = _load_interview_material_context(db, principal, interview_id)
        if context is None:
            return _denied(request)
        interview, application, _ = context
        row = db.execute(
            select(Resume, FileObject)
            .join(
                FileObject,
                and_(
                    FileObject.organization_id == Resume.organization_id,
                    FileObject.id == Resume.file_object_id,
                ),
            )
            .where(
                Resume.organization_id == principal.organization_id,
                Resume.id == application.resume_id,
                Resume.candidate_id == application.candidate_id,
                FileObject.storage_state == "clean",
                FileObject.scan_status == "clean",
            )
        ).one_or_none()
        if row is None:
            return _denied(request)
        resume, file_object = row
        spool = None
        try:
            spool = request.app.state.resume_storage.open_download(
                file_object.storage_key,
                MAX_DOWNLOAD_BYTES,
            )
            disposition = content_disposition(file_object.original_filename)
        except (StorageReadFailed, StorageObjectTooLarge, ValueError):
            if spool is not None:
                spool.close()
            db.rollback()
            return problem(request, 503, "attachment_unavailable", "The attachment is temporarily unavailable.")

        disposition_kind = "attachment" if download else "inline"
        if not download:
            disposition = disposition.replace("attachment;", "inline;", 1)
        db.add(
            AuditLog(
                organization_id=principal.organization_id,
                actor_user_id=principal.user_id,
                event_type="interview.resume_file_accessed",
                outcome="success",
                trace_id=request.state.trace_id,
                metadata_json={
                    "interview_id": str(interview.id),
                    "resume_id": str(resume.id),
                    "disposition": disposition_kind,
                },
            )
        )
        try:
            db.commit()
        except Exception:
            spool.close()
            raise

        def stream_spool():
            try:
                while chunk := spool.read(64 * 1024):
                    yield chunk
            finally:
                spool.close()

        return StreamingResponse(
            stream_spool(),
            media_type=file_object.mime_type,
            headers={
                "Cache-Control": "no-store",
                "Content-Disposition": disposition,
                "X-Content-Type-Options": "nosniff",
            },
        )


@router.get("/interviews/{interview_id}/feedbacks", response_model=DataCollection)
def list_interview_feedbacks(interview_id: UUID, request: Request):
    principal = _principal(request)
    if isinstance(principal, JSONResponse):
        return principal
    with request.app.state.identity_store.sync_session() as db:
        if _load_interview_for_feedback_summary(db, principal, interview_id) is None:
            return _denied(request)
        rows = db.execute(
            select(InterviewFeedback, User)
            .join(
                User,
                and_(
                    User.organization_id == InterviewFeedback.organization_id,
                    User.id == InterviewFeedback.author_id,
                ),
            )
            .where(
                InterviewFeedback.organization_id == principal.organization_id,
                InterviewFeedback.interview_id == interview_id,
                InterviewFeedback.status.in_(("submitted", "amended")),
            )
            .order_by(InterviewFeedback.submitted_at, InterviewFeedback.id)
        ).all()
        response = JSONResponse(
            {
                "data": [_submitted_feedback_data(feedback, author) for feedback, author in rows],
                "meta": {"count": len(rows)},
            }
        )
        response.headers["Cache-Control"] = "no-store"
        return response


@router.get("/interviews/{interview_id}/my-feedback", response_model=DataResource)
def get_my_feedback(interview_id: UUID, request: Request):
    principal = _principal(request)
    if isinstance(principal, JSONResponse):
        return principal
    with request.app.state.identity_store.sync_session() as db:
        interview = _load_interview(db, principal, interview_id)
        participant = _assigned_participant(db, principal, interview_id)
        if interview is None or participant is None:
            return _denied(request)
        feedback = db.scalar(
            select(InterviewFeedback).where(
                InterviewFeedback.organization_id == principal.organization_id,
                InterviewFeedback.interview_id == interview_id,
                InterviewFeedback.author_id == principal.user_id,
            )
        )
        if feedback is None:
            response = JSONResponse({"data": {"status": "draft", "version": 0}})
            response.headers["ETag"] = '"0"'
        else:
            response = JSONResponse({"data": _feedback_data(feedback)})
            response.headers["ETag"] = f'"{feedback.version}"'
        response.headers["Cache-Control"] = "no-store"
        return response


@router.put("/interviews/{interview_id}/my-feedback", response_model=DataResource)
def put_my_feedback(interview_id: UUID, payload: FeedbackDraft, request: Request, if_match: str | None = Header(None)):
    principal = _principal(request)
    expected = _expected_version(request, if_match)
    for value in (principal, expected):
        if isinstance(value, JSONResponse):
            return value
    with request.app.state.identity_store.sync_session() as db:
        interview = _load_interview(db, principal, interview_id)
        if interview is None:
            return _denied(request)
        application_id = interview.application_id
        try:
            _lock_application_candidate_retention(db, principal.organization_id, application_id)
        except CandidateUnavailable:
            db.rollback()
            return _denied(request)
        locked_interview = db.scalar(
            select(Interview)
            .where(
                Interview.organization_id == principal.organization_id,
                Interview.id == interview_id,
            )
            .with_for_update()
        )
        if locked_interview is None or locked_interview.application_id != application_id:
            return _denied(request)
        participant = _assigned_participant(db, principal, interview_id, for_update=True)
        if participant is None:
            return _denied(request)
        now = datetime.now(timezone.utc)
        can_open_feedback = locked_interview.status in {"scheduled", "rescheduled", "confirmed"}
        if locked_interview.status != "pending_feedback" and not can_open_feedback:
            return problem(request, 409, "invalid_state_transition", "Feedback is not open for this interview.")
        feedback = db.scalar(
            select(InterviewFeedback)
            .where(
                InterviewFeedback.organization_id == principal.organization_id,
                InterviewFeedback.interview_id == interview_id,
                InterviewFeedback.author_id == principal.user_id,
            )
            .with_for_update()
        )
        if feedback is None:
            if expected != 0:
                return problem(request, 409, "resource_version_conflict", "The feedback draft changed. Refresh and retry.")
            if can_open_feedback:
                source_status = locked_interview.status
                db.add(
                    InterviewEvent(
                        organization_id=principal.organization_id,
                        interview_id=interview_id,
                        actor_user_id=principal.user_id,
                        event_type="interview.feedback_opened",
                        payload={"source_status": source_status},
                    )
                )
                db.add(
                    AuditLog(
                        organization_id=principal.organization_id,
                        actor_user_id=principal.user_id,
                        event_type="interview.feedback_opened",
                        outcome="success",
                        trace_id=request.state.trace_id,
                        metadata_json={"interview_id": str(interview_id)},
                    )
                )
            feedback = InterviewFeedback(
                organization_id=principal.organization_id,
                interview_id=interview_id,
                author_id=principal.user_id,
                status="draft",
                version=1,
                **payload.model_dump(),
            )
            db.add(feedback)
        else:
            if locked_interview.status != "pending_feedback" and not can_open_feedback:
                return problem(request, 409, "invalid_state_transition", "Feedback is not open for this interview.")
            if feedback.status != "draft":
                return problem(request, 409, "feedback_already_submitted", "Submitted feedback cannot be overwritten.")
            if feedback.version != expected:
                return problem(request, 409, "resource_version_conflict", "The feedback draft changed. Refresh and retry.")
            for field, value in payload.model_dump().items():
                setattr(feedback, field, value)
            feedback.version += 1
            feedback.updated_at = datetime.now(timezone.utc)
        db.flush()
        body = {"data": _feedback_data(feedback)}
        db.commit()
        response = JSONResponse(body)
        response.headers["ETag"] = f'"{body["data"]["version"]}"'
        response.headers["Cache-Control"] = "no-store"
        return response


@router.post("/interviews/{interview_id}/my-feedback/submit", response_model=DataResource)
def submit_my_feedback(interview_id: UUID, request: Request, idempotency_key: str | None = Header(None)):
    principal = _principal(request)
    key = _idempotency(request, idempotency_key)
    for value in (principal, key):
        if isinstance(value, JSONResponse):
            return value
    with request.app.state.identity_store.sync_session() as db:
        interview = _load_interview(db, principal, interview_id)
        participant = _assigned_participant(db, principal, interview_id)
        if interview is None or participant is None:
            return _denied(request)
        try:
            def action():
                application_id = interview.application_id
                candidate_id = _lock_application_candidate_retention(
                    db, principal.organization_id, application_id
                )
                locked_interview = db.scalar(
                    select(Interview)
                    .where(
                        Interview.organization_id == principal.organization_id,
                        Interview.id == interview_id,
                    )
                    .with_for_update()
                )
                if (
                    locked_interview is None
                    or locked_interview.application_id != application_id
                    or locked_interview.status
                    not in {"scheduled", "rescheduled", "confirmed", "pending_feedback"}
                ):
                    raise InvalidStateTransition
                locked_participant = _assigned_participant(
                    db, principal, interview_id, for_update=True
                )
                if locked_participant is None:
                    raise InvalidStateTransition
                feedback = db.scalar(
                    select(InterviewFeedback)
                    .where(
                        InterviewFeedback.organization_id == principal.organization_id,
                        InterviewFeedback.interview_id == interview_id,
                        InterviewFeedback.author_id == principal.user_id,
                    )
                    .with_for_update()
                )
                if feedback is None or feedback.status != "draft":
                    raise InvalidStateTransition
                if (
                    set(feedback.ratings) != REQUIRED_RATINGS
                    or feedback.conclusion is None
                    or not (feedback.strengths and feedback.strengths.strip())
                    or not (feedback.risks and feedback.risks.strip())
                ):
                    raise ValueError("feedback is incomplete")
                now = datetime.now(timezone.utc)
                feedback.status = "submitted"
                feedback.submitted_at = now
                feedback.version += 1
                feedback.updated_at = now
                locked_participant.task_status = "completed"
                db.flush()

                required_ids = set(
                    db.scalars(
                        select(InterviewParticipant.user_id).where(
                            InterviewParticipant.organization_id == principal.organization_id,
                            InterviewParticipant.interview_id == interview_id,
                            InterviewParticipant.required_feedback.is_(True),
                        )
                    ).all()
                )
                submitted_ids = set(
                    db.scalars(
                        select(InterviewFeedback.author_id).where(
                            InterviewFeedback.organization_id == principal.organization_id,
                            InterviewFeedback.interview_id == interview_id,
                            InterviewFeedback.status.in_(("submitted", "amended")),
                        )
                    ).all()
                )
                if required_ids <= submitted_ids:
                    locked_interview.status = "feedback_completed"
                    locked_interview.version += 1
                    locked_interview.updated_at = now
                    db.flush()
                    application_advanced, next_round_name = _advance_application_if_interviews_complete(
                        db,
                        principal.organization_id,
                        locked_interview.application_id,
                        locked_interview.round_name,
                        actor_user_id=principal.user_id,
                        trace_id=request.state.trace_id,
                    )
                    db.add(
                        InterviewEvent(
                            organization_id=principal.organization_id,
                            interview_id=interview_id,
                            actor_user_id=principal.user_id,
                            event_type="interview.feedback_completed",
                            payload={
                                "required_feedback_count": len(required_ids),
                                "application_advanced": application_advanced,
                                "next_round_name": next_round_name,
                            },
                        )
                    )
                db.add(
                    AuditLog(
                        organization_id=principal.organization_id,
                        actor_user_id=principal.user_id,
                        event_type="interview.feedback_submitted",
                        outcome="success",
                        trace_id=request.state.trace_id,
                        metadata_json={"interview_id": str(interview_id), "feedback_id": str(feedback.id)},
                    )
                )
                db.flush()
                recalculate_candidate_retention(
                    db, principal.organization_id, candidate_id
                )
                return 200, {"data": _feedback_data(feedback)}

            status_code, body = persisted_idempotent(
                db,
                principal.organization_id,
                principal.user_id,
                f"interview.feedback.submit:{interview_id}",
                key,
                {"interview_id": interview_id},
                action,
            )
            db.commit()
            response = JSONResponse(body, status_code=status_code)
            response.headers["ETag"] = f'"{body["data"]["version"]}"'
            response.headers["Cache-Control"] = "no-store"
            return response
        except CandidateUnavailable:
            db.rollback()
            return _denied(request)
        except InvalidStateTransition:
            db.rollback()
            return problem(request, 409, "invalid_state_transition", "The feedback cannot be submitted in its current state.")
        except IdempotencyConflict:
            db.rollback()
            return problem(request, 409, "idempotency_conflict", "The idempotency key was reused with another request.")
        except ValueError:
            db.rollback()
            return problem(request, 422, "validation_failed", "Complete all required feedback fields before submitting.")


@router.post("/interview-feedback/{feedback_id}/amendments", response_model=DataResource)
def amend_feedback(feedback_id: UUID, payload: FeedbackAmendment, request: Request, if_match: str | None = Header(None)):
    principal = _principal(request)
    expected = _expected_version(request, if_match)
    for value in (principal, expected):
        if isinstance(value, JSONResponse):
            return value
    with request.app.state.identity_store.sync_session() as db:
        feedback = db.scalar(
            select(InterviewFeedback)
            .where(
                InterviewFeedback.organization_id == principal.organization_id,
                InterviewFeedback.id == feedback_id,
                InterviewFeedback.author_id == principal.user_id,
            )
        )
        if feedback is None or _assigned_participant(db, principal, feedback.interview_id) is None:
            return _denied(request)
        interview_id = feedback.interview_id
        application_id = db.scalar(
            select(Interview.application_id).where(
                Interview.organization_id == principal.organization_id,
                Interview.id == interview_id,
            )
        )
        if application_id is None:
            return _denied(request)
        try:
            candidate_id = _lock_application_candidate_retention(
                db, principal.organization_id, application_id
            )
        except CandidateUnavailable:
            db.rollback()
            return _denied(request)
        feedback = db.scalar(
            select(InterviewFeedback)
            .where(
                InterviewFeedback.organization_id == principal.organization_id,
                InterviewFeedback.id == feedback_id,
                InterviewFeedback.author_id == principal.user_id,
            )
            .with_for_update()
        )
        if (
            feedback is None
            or feedback.interview_id != interview_id
            or _assigned_participant(db, principal, interview_id) is None
        ):
            return _denied(request)
        if feedback.status not in {"submitted", "amended"}:
            return problem(request, 409, "invalid_state_transition", "Only submitted feedback can be amended.")
        if feedback.version != expected:
            return problem(request, 409, "resource_version_conflict", "The feedback changed. Refresh and retry.")
        previous = _feedback_document(feedback)
        changes = payload.model_dump(exclude={"reason"})
        now = datetime.now(timezone.utc)
        try:
            if db.get_bind().dialect.name == "postgresql":
                db.execute(
                    text("SELECT set_config('app.actor_user_id', :actor_id, true)"),
                    {"actor_id": str(principal.user_id)},
                )
                db.execute(
                    text("SELECT set_config('app.feedback_revision_reason', :reason, true)"),
                    {"reason": payload.reason.strip()},
                )
                for field, value in changes.items():
                    setattr(feedback, field, value)
                feedback.updated_at = now
                db.flush()
                db.refresh(feedback)
            else:
                for field, value in changes.items():
                    setattr(feedback, field, value)
                feedback.status = "amended"
                feedback.version += 1
                feedback.updated_at = now
                db.flush()
                revision_number = (
                    db.scalar(
                        select(func.max(InterviewFeedbackRevision.revision_number)).where(
                            InterviewFeedbackRevision.organization_id == principal.organization_id,
                            InterviewFeedbackRevision.feedback_id == feedback.id,
                        )
                    )
                    or 0
                ) + 1
                db.add(
                    InterviewFeedbackRevision(
                        organization_id=principal.organization_id,
                        feedback_id=feedback.id,
                        revision_number=revision_number,
                        previous_payload=previous,
                        new_payload=_feedback_document(feedback),
                        reason=payload.reason.strip(),
                        actor_id=principal.user_id,
                    )
                )
            db.add(
                AuditLog(
                    organization_id=principal.organization_id,
                    actor_user_id=principal.user_id,
                    event_type="interview.feedback_amended",
                    outcome="success",
                    trace_id=request.state.trace_id,
                    metadata_json={"feedback_id": str(feedback.id), "interview_id": str(feedback.interview_id)},
                )
            )
            db.flush()
            recalculate_candidate_retention(
                db, principal.organization_id, candidate_id
            )
            body = {"data": _feedback_data(feedback)}
            db.commit()
            response = JSONResponse(body)
            response.headers["ETag"] = f'"{body["data"]["version"]}"'
            response.headers["Cache-Control"] = "no-store"
            return response
        except Exception:
            db.rollback()
            raise


@router.get("/me/tasks", response_model=DataCollection)
def my_tasks(request: Request):
    principal = _principal(request)
    if isinstance(principal, JSONResponse):
        return principal
    if not _has_assignment_access(principal):
        response = JSONResponse({"data": [], "meta": {"count": 0}})
        response.headers["Cache-Control"] = "no-store"
        return response
    with request.app.state.identity_store.sync_session() as db:
        rows = db.execute(
            select(InterviewParticipant, Interview, Application, Candidate, Job)
            .join(
                Interview,
                and_(
                    Interview.organization_id == InterviewParticipant.organization_id,
                    Interview.id == InterviewParticipant.interview_id,
                ),
            )
            .join(
                Application,
                and_(
                    Application.organization_id == Interview.organization_id,
                    Application.id == Interview.application_id,
                ),
            )
            .join(
                Candidate,
                and_(
                    Candidate.organization_id == Application.organization_id,
                    Candidate.id == Application.candidate_id,
                ),
            )
            .join(Job, and_(Job.organization_id == Application.organization_id, Job.id == Application.job_id))
            .where(
                InterviewParticipant.organization_id == principal.organization_id,
                InterviewParticipant.user_id == principal.user_id,
                InterviewParticipant.role == "interviewer",
                InterviewParticipant.task_status == "ready",
                Interview.status.in_(("scheduled", "rescheduled", "confirmed", "pending_feedback")),
                Candidate.deleted_at.is_(None),
            )
            .order_by(Interview.starts_at, Interview.id)
        ).all()
        data = [
            {
                "id": f"{('feedback' if interview.status == 'pending_feedback' else 'interview')}:{interview.id}",
                "type": "interview_feedback" if interview.status == "pending_feedback" else "interview",
                "interview_id": str(interview.id),
                "application_id": str(application.id),
                "candidate": {"id": str(candidate.id), "display_name": candidate.display_name},
                "job": {"id": str(job.id), "title": job.title},
                "round_name": interview.round_name,
                "starts_at": _aware(interview.starts_at).isoformat(),
                "status": interview.status,
            }
            for participant, interview, application, candidate, job in rows
        ]
        response = JSONResponse({"data": data, "meta": {"count": len(data)}})
        response.headers["Cache-Control"] = "no-store"
        return response
