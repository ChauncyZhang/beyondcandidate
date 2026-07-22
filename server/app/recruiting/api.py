import hashlib
import json
import re
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Header, Query, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy import and_, exists, func, literal, or_, select
from sqlalchemy.orm import aliased

from server.app.governance.retention import recalculate_candidate_retention
from server.app.governance.audit import AuditValidationError, _validate_metadata
from server.app.identity.api import problem, session_token
from server.app.identity.models import AuditLog, Department, Job, JobCollaborator, User, UserRole, UserStatus, WorkflowTemplate
from server.app.identity.policy import Principal
from server.app.identity.service import InvalidSession
from server.app.recruiting.cursor import CursorCodec, InvalidCursor
from server.app.recruiting.authorization import RecruitingAction, RecruitingAuthorizationService
from server.app.recruiting.models import (
    Application, ApplicationReviewTask, ApplicationStageEvent, Candidate, CandidateContact, CandidateEvent, CandidateNote,
    DownloadTicket, JobJdVersion, Resume, ResumeProfile, ScreeningRuleVersion,
)
from server.app.screening.models import ScreeningItem, ScreeningResult
from server.app.llm.models import LlmScreeningEvaluation
from server.app.interviews.models import Interview
from server.app.notifications.service import read_versions, workbench_notification_version
from server.app.recruiting.tasks import LLM_TERMINAL_SAFE_ERROR_CODES, normalize_llm_terminal_safe_error_code
from server.app.screening.rules import RuleSnapshotError,normalize_rule_content
from server.app.recruiting.security import ContactCipher
from server.app.recruiting.http import content_disposition
from server.app.recruiting.workflow import next_interview_round
from server.app.recruiting.storage import MAX_DOWNLOAD_BYTES, MAX_PREVIEW_BYTES, StorageObjectTooLarge, StorageReadFailed
from server.app.recruiting.resume_profile import extract_resume_profile
from server.app.recruiting.schemas import (
    ApplicationCollection, ApplicationResource, CandidateCollection, CandidateResource,
    FunnelResource, JobCollection, JobDefinitionCommand, JobDefinitionResource, JobOwnerOptionCollection, JobResource, NoteCollection, NoteResource,
    PreviewResource, ResumeCollection, TicketResource, TimelineCollection,
    VersionCollection, VersionResource, WorkbenchResource, Problem,
)
from server.app.recruiting.service import (
    ActiveApplicationExists, CandidateUnavailable, IdempotencyConflict, InvalidAggregateRelationship,
    InvalidStateTransition, ResourceVersionConflict, SystemClock, SystemTokens,
    TicketInvalid, consume_download_ticket_record, create_application_record,
    create_job_definition_record,
    issue_download_ticket_record, persisted_idempotent,
    lock_active_candidate, lock_job_for_version_write,
    transition_job_record, patch_job_record, patch_candidate_record, patch_application_record,
    replace_job_definition_record, apply_application_workflow_action_record,
)


PROBLEM_RESPONSES = {
    status: {"model": Problem, "content": {"application/problem+json": {"schema": {"$ref": "#/components/schemas/Problem"}}}}
    for status in (401, 403, 404, 409, 422, 428, 503)
}
router = APIRouter(prefix="/api/v1", responses=PROBLEM_RESPONSES)
ETAG = re.compile(r'^"([1-9][0-9]*)"$')
AUTH = RecruitingAuthorizationService()
JOB_STATUSES = ("draft", "open", "paused", "closed", "archived")
WORKBENCH_STAGES = ("new", "review", "contact", "interview_pending", "interviewing", "decision", "passed")
WORKBENCH_TASK_STAGES = ("review", "interview_pending", "decision", "passed")
WORKBENCH_TASK_ACTIONS = {
    "review": RecruitingAction.RECOMMEND,
    "interview_pending": RecruitingAction.TRANSITION,
    "decision": RecruitingAction.RECOMMEND,
    "passed": RecruitingAction.TRANSITION,
}


def _job_permission_projection(principal: Principal, action: RecruitingAction, label: str):
    predicate = AUTH.job_predicate(principal, action, Job)
    return literal(predicate).label(label) if isinstance(predicate, bool) else predicate.label(label)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class JobCreate(StrictModel):
    title: str = Field(min_length=1, max_length=200)
    department_id: UUID | None = None
    headcount: int = Field(default=1, gt=0)
    priority: str = Field(default="normal", max_length=16)
    hiring_owner_id: UUID | None = None


class JobPatch(StrictModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    headcount: int | None = Field(default=None, gt=0)
    priority: str | None = Field(default=None, max_length=16)
    hiring_owner_id: UUID | None = None


class Transition(StrictModel):
    target: str = Field(min_length=1, max_length=32)
    reason_code: str | None = Field(default=None, max_length=64)
    reason_text: str | None = Field(default=None, max_length=1000)


class WorkflowAction(StrictModel):
    action: Literal[
        "review_approved",
        "review_rejected",
        "hiring_approved",
        "hiring_rejected",
        "offer_accepted",
        "offer_declined",
    ]
    reason_text: str | None = Field(default=None, max_length=1000)


class VersionCreate(StrictModel):
    content: dict[str, Any]


class ContactInput(StrictModel):
    kind: str
    value: str = Field(min_length=1, max_length=320)


class CandidateCreate(StrictModel):
    display_name: str = Field(min_length=1, max_length=200)
    current_title: str | None = Field(default=None, max_length=200)
    location: str | None = Field(default=None, max_length=200)
    owner_id: UUID | None = None
    contacts: list[ContactInput] = Field(default_factory=list, max_length=10)


class CandidatePatch(StrictModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=200)
    current_title: str | None = Field(default=None, max_length=200)
    location: str | None = Field(default=None, max_length=200)
    owner_id: UUID | None = None


class NoteCreate(StrictModel):
    application_id: UUID
    body: str = Field(min_length=1, max_length=4000)

    @field_validator("body")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("note must not be blank")
        return value.strip()


class ApplicationCreate(StrictModel):
    candidate_id: UUID
    resume_id: UUID
    owner_id: UUID | None = None
    source: str = Field(default="manual", min_length=1, max_length=64)


class ApplicationPatch(StrictModel):
    owner_id: UUID | None = None
    human_conclusion: str | None = Field(default=None, max_length=4000)


class TicketConsume(StrictModel):
    token: str = Field(min_length=20, max_length=512)


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


def _problem_for(request: Request, error: Exception) -> JSONResponse:
    mapping = {
        ResourceVersionConflict: (409, "resource_version_conflict"),
        InvalidStateTransition: (409, "invalid_state_transition"),
        IdempotencyConflict: (409, "idempotency_conflict"),
        ActiveApplicationExists: (409, "active_application_exists"),
        InvalidAggregateRelationship: (422, "validation_failed"),
        CandidateUnavailable: (404, "resource_not_found"),
        TicketInvalid: (404, "resource_not_found"),
        InvalidCursor: (422, "validation_failed"),
    }
    status, code = mapping.get(type(error), (422, "validation_failed"))
    return problem(request, status, code, "The request could not be completed.")


def _expected_version(request: Request, value: str | None) -> int | JSONResponse:
    if value is None:
        return problem(request, 428, "precondition_required", "A quoted If-Match version is required.")
    match = ETAG.fullmatch(value)
    if not match:
        return problem(request, 422, "validation_failed", "If-Match must be a quoted integer.")
    return int(match.group(1))


def _idempotency(request: Request, value: str | None) -> str | JSONResponse:
    if not value or len(value) > 255:
        return problem(request, 428, "idempotency_key_required", "Idempotency-Key is required.")
    return value


def _job_scope(principal: Principal, action: RecruitingAction = RecruitingAction.READ):
    return AUTH.job_predicate(principal, action)


def _candidate_scope(principal: Principal, action: RecruitingAction = RecruitingAction.READ):
    return AUTH.candidate_predicate(principal, action)


def _resume_application_scope(principal: Principal, action: RecruitingAction):
    active_candidate = exists().where(
        Candidate.organization_id == Resume.organization_id,
        Candidate.id == Resume.candidate_id,
        Candidate.deleted_at.is_(None),
    )
    visible_application = exists().where(
        Application.organization_id == Resume.organization_id,
        Application.resume_id == Resume.id,
        exists().where(
            Job.organization_id == Application.organization_id,
            Job.id == Application.job_id,
            AUTH.job_predicate(principal, action, Job),
        ),
    )
    return and_(active_candidate, visible_application)


def _load_candidate_application(db, principal: Principal, candidate_id: UUID, application_id: UUID, action: RecruitingAction):
    return db.scalar(select(Application).join(
        Job, and_(Job.organization_id == Application.organization_id, Job.id == Application.job_id)
    ).join(
        Candidate, and_(Candidate.organization_id == Application.organization_id, Candidate.id == Application.candidate_id)
    ).where(
        Application.organization_id == principal.organization_id,
        Application.id == application_id,
        Application.candidate_id == candidate_id,
        Candidate.deleted_at.is_(None),
        AUTH.job_predicate(principal, action, Job),
    ))


def _job_data(job: Job) -> dict[str, Any]:
    return {"id": str(job.id), "title": job.title, "department_id": str(job.department_id) if job.department_id else None, "headcount": job.headcount, "priority": job.priority, "hiring_owner_id": str(job.hiring_owner_id) if job.hiring_owner_id else None, "workflow_template_id": str(job.workflow_template_id) if job.workflow_template_id else None, "owner_id": str(job.owner_id), "status": job.status, "version": job.version, "updated_at": job.updated_at.isoformat()}


def _job_jd_definition(jd: JobJdVersion) -> dict[str, Any]:
    content = jd.content if isinstance(jd.content, dict) else {}
    if "text" in content:
        if set(content) == {"text"} and isinstance(content["text"], str):
            content = {
                "description": content["text"],
                "location": "",
                "process_template": "默认招聘流程",
                "workflow_template_id": None,
                "llm_enabled": False,
            }
        else:
            content = {}
    return {"id": str(jd.id), "version_number": jd.version_number, **{key: content.get(key) for key in ("description", "location", "process_template", "workflow_template_id", "llm_enabled")}}


def _screening_rules_definition(rules: ScreeningRuleVersion) -> dict[str, Any]:
    try:
        content = normalize_rule_content(rules.content)
    except RuleSnapshotError:
        content = {}
    return {"id": str(rules.id), "version_number": rules.version_number, **{key: content.get(key) for key in ("must_have", "nice_to_have")}}


def _workbench_candidate_data(row, next_interview_round: str | None = None) -> dict[str, Any]:
    return {
        "application_id": str(row.application_id),
        "candidate_id": str(row.candidate_id),
        "job_id": str(row.job_id),
        "display_name": row.display_name,
        "current_title": row.current_title,
        "location": row.location,
        "source": row.source,
        "stage": row.stage,
        "updated_at": row.updated_at,
        "next_interview_round": next_interview_round,
    }


def _job_definition_data(job: Job, jd: JobJdVersion | None, rules: ScreeningRuleVersion | None) -> dict[str, Any]:
    return {
        "job": _job_data(job),
        "jd": None if jd is None else _job_jd_definition(jd),
        "rules": None if rules is None else _screening_rules_definition(rules),
    }


def _job_definition_response(request: Request, body: dict[str, Any], status: int) -> JSONResponse:
    try:
        serialized = JobDefinitionResource.model_validate(body).model_dump(mode="json")
    except ValidationError:
        return problem(request, 409, "job_definition_incompatible", "The stored job definition is incompatible with this API contract.")
    response = JSONResponse(serialized, status_code=status)
    response.headers["ETag"] = f'"{serialized["data"]["job"]["version"]}"'
    return response


def _candidate_data(db, candidate: Candidate, principal: Principal) -> dict[str, Any]:
    contacts = db.scalars(select(CandidateContact).where(CandidateContact.organization_id == candidate.organization_id, CandidateContact.candidate_id == candidate.id)).all()
    return {"id": str(candidate.id), "display_name": candidate.display_name, "current_title": candidate.current_title, "location": candidate.location, "owner_id": str(candidate.owner_id) if candidate.owner_id else None, "version": candidate.version, "updated_at": candidate.updated_at.isoformat(), "contacts": [{"kind": item.kind, "value": item.masked_value} for item in contacts]}


def _latest_screening_results(organization_id: UUID):
    return select(
        ScreeningResult.organization_id,
        ScreeningResult.id.label("screening_result_id"),
        ScreeningResult.item_id,
        ScreeningResult.application_id,
        ScreeningResult.rule_score,
        ScreeningResult.recommendation,
        func.row_number().over(
            partition_by=(ScreeningResult.organization_id, ScreeningResult.application_id),
            order_by=(ScreeningResult.created_at.desc(), ScreeningResult.id.desc()),
        ).label("result_rank"),
    ).where(
        ScreeningResult.organization_id == organization_id,
        ScreeningResult.application_id.is_not(None),
    ).subquery()


def _latest_llm_evaluations(organization_id: UUID):
    return select(
        LlmScreeningEvaluation.organization_id,
        LlmScreeningEvaluation.id.label("evaluation_id"),
        LlmScreeningEvaluation.screening_result_id,
        LlmScreeningEvaluation.score,
        LlmScreeningEvaluation.recommendation,
        LlmScreeningEvaluation.dimensions,
        LlmScreeningEvaluation.summary,
        LlmScreeningEvaluation.strengths,
        LlmScreeningEvaluation.gaps,
        LlmScreeningEvaluation.risks,
        LlmScreeningEvaluation.interview_questions,
        func.row_number().over(
            partition_by=(LlmScreeningEvaluation.organization_id,LlmScreeningEvaluation.screening_result_id),
            order_by=(LlmScreeningEvaluation.created_at.desc(),LlmScreeningEvaluation.id.desc()),
        ).label("evaluation_rank"),
    ).where(LlmScreeningEvaluation.organization_id==organization_id).subquery()


def _validated_terminal_routes(db,organization_id:UUID,application_ids:list[UUID]):
    if not application_ids: return {}
    item_rows=db.execute(select(ScreeningItem.id,ScreeningItem.application_id).where(
        ScreeningItem.organization_id==organization_id,
        ScreeningItem.application_id.in_(application_ids),
    )).all()
    item_applications={item_id:application_id for item_id,application_id in item_rows}
    audits=db.scalars(select(AuditLog).where(
        AuditLog.organization_id==organization_id,
        AuditLog.category=="recruiting",
        AuditLog.event_type=="screening.terminal_routed",
        AuditLog.outcome=="success",
        AuditLog.resource_type=="application",
        AuditLog.resource_id.in_(application_ids),
    ).order_by(AuditLog.created_at.desc(),AuditLog.id.desc())).all()
    routes={}; seen=set()
    for audit in audits:
        application_id=audit.resource_id
        if application_id in seen: continue
        seen.add(application_id)
        try:
            metadata=_validate_metadata("screening.terminal_routed",audit.metadata_json)
            item_id=UUID(metadata["item_id"])
        except (AuditValidationError,KeyError,TypeError,ValueError):
            continue
        route=metadata.get("to_stage"); ai_status=metadata.get("ai_status")
        if (
            metadata.get("application_id")!=str(application_id)
            or metadata.get("from_stage")!="new"
            or item_applications.get(item_id)!=application_id
            or route not in {"review","deferred"}
            or ai_status not in {"succeeded","failed"}
        ):
            continue
        score=metadata.get("score")
        safe_error_code=metadata.get("safe_error_code")
        if ai_status=="succeeded":
            if not isinstance(score,int) or isinstance(score,bool) or not 0<=score<=100 or safe_error_code is not None:
                continue
        else:
            if route!="review" or score is not None or safe_error_code not in LLM_TERMINAL_SAFE_ERROR_CODES:
                continue
        routes[application_id]={"route_result":route,"ai_status":ai_status,"safe_error_code":safe_error_code}
    return routes


def _screening_projections_for_applications(db,organization_id:UUID,application_ids:list[UUID]):
    if not application_ids: return {}
    latest_result=_latest_screening_results(organization_id); latest_evaluation=_latest_llm_evaluations(organization_id)
    rows=db.execute(select(
        Application.id,ScreeningItem.llm_status,
        latest_evaluation.c.evaluation_id,latest_evaluation.c.score,latest_evaluation.c.recommendation,
        latest_evaluation.c.dimensions,latest_evaluation.c.summary,latest_evaluation.c.strengths,
        latest_evaluation.c.gaps,latest_evaluation.c.risks,latest_evaluation.c.interview_questions,
    ).join(latest_result,and_(latest_result.c.organization_id==Application.organization_id,latest_result.c.application_id==Application.id,latest_result.c.result_rank==1)).join(ScreeningItem,and_(ScreeningItem.organization_id==Application.organization_id,ScreeningItem.id==latest_result.c.item_id)).outerjoin(latest_evaluation,and_(latest_evaluation.c.organization_id==Application.organization_id,latest_evaluation.c.screening_result_id==latest_result.c.screening_result_id,latest_evaluation.c.evaluation_rank==1)).where(Application.organization_id==organization_id,Application.id.in_(application_ids))).all()
    routes=_validated_terminal_routes(db,organization_id,application_ids); projections={}
    for row in rows:
        route=routes.get(row.id); route_result=route["route_result"] if route else None
        evaluation=None if row.evaluation_id is None or route is not None and route["ai_status"]=="failed" else {"score":row.score,"recommendation":row.recommendation,"dimensions":list(row.dimensions or []),"summary":row.summary,"strengths":list(row.strengths or []),"gaps":list(row.gaps or []),"risks":list(row.risks or []),"questions":list(row.interview_questions or [])}
        unavailable=evaluation is None and route is not None and route["ai_status"]=="failed"
        projections[row.id]={"route_result":route_result,"ai_score":row.score if evaluation is not None else None,"ai_recommendation":row.recommendation if evaluation is not None else "AI评分不可用" if unavailable else None,"llm_status":route["ai_status"] if route else row.llm_status,"llm_error_code":normalize_llm_terminal_safe_error_code(route["safe_error_code"]) if unavailable else None,"llm_evaluation":evaluation}
    return projections


def _next_interview_rounds(
    db, organization_id: UUID, application_ids: list[UUID]
) -> dict[UUID, str | None]:
    if not application_ids:
        return {}
    template_rows = db.execute(select(
        Application.id,
        WorkflowTemplate.rounds,
    ).join(
        Job,
        and_(Job.organization_id == Application.organization_id, Job.id == Application.job_id),
    ).outerjoin(
        WorkflowTemplate,
        and_(
            WorkflowTemplate.organization_id == Job.organization_id,
            WorkflowTemplate.id == Job.workflow_template_id,
        ),
    ).where(
        Application.organization_id == organization_id,
        Application.id.in_(application_ids),
    )).all()
    completed_rows = db.execute(select(
        Interview.application_id,
        Interview.round_name,
    ).where(
        Interview.organization_id == organization_id,
        Interview.application_id.in_(application_ids),
        Interview.status == "feedback_completed",
    )).all()
    completed_by_application: dict[UUID, set[str]] = {}
    for application_id, round_name in completed_rows:
        completed_by_application.setdefault(application_id, set()).add(round_name)
    result: dict[UUID, str | None] = {}
    for application_id, rounds in template_rows:
        result[application_id] = next_interview_round(
            rounds, completed_by_application.get(application_id, set())
        )
    return result


def _candidate_application_summaries(db, organization_id: UUID, application_ids: list[UUID]) -> dict[UUID, dict[str, Any]]:
    if not application_ids:
        return {}
    latest_result = _latest_screening_results(organization_id)
    rows = db.execute(select(
        Application,
        Job.title,
        User.display_name,
        latest_result.c.rule_score,
        latest_result.c.recommendation,
    ).join(
        Job, and_(Job.organization_id == Application.organization_id, Job.id == Application.job_id),
    ).join(
        User, and_(User.organization_id == Application.organization_id, User.id == Application.owner_id),
    ).outerjoin(
        latest_result,
        and_(
            latest_result.c.organization_id == Application.organization_id,
            latest_result.c.application_id == Application.id,
            latest_result.c.result_rank == 1,
        ),
    ).where(
        Application.organization_id == organization_id,
        Application.id.in_(application_ids),
    )).all()
    projections=_screening_projections_for_applications(db,organization_id,application_ids)
    next_rounds = _next_interview_rounds(db, organization_id, application_ids)
    return {
        application.id: {
            "id": str(application.id),
            "job_id": str(application.job_id),
            "job_title": job_title,
            "resume_id": str(application.resume_id),
            "owner_id": str(application.owner_id),
            "owner_name": owner_name,
            "stage": application.stage,
            "source": application.source,
            "human_conclusion": application.human_conclusion,
            "version": application.version,
            "updated_at": application.updated_at.isoformat(),
            "next_interview_round": next_rounds.get(application.id),
            "rule_score": rule_score,
            "recommendation": recommendation,
            **projections.get(application.id,{"route_result":None,"ai_score":None,"ai_recommendation":None,"llm_status":None,"llm_error_code":None,"llm_evaluation":None}),
        }
        for application, job_title, owner_name, rule_score, recommendation in rows
    }


def _candidate_search_condition(request: Request, query: str):
    contact_conditions = [CandidateContact.masked_value.ilike(f"%{query}%")]
    for kind in ("email", "phone"):
        try:
            contact_conditions.append(CandidateContact.lookup_hash == request.app.state.contact_cipher.protect(kind, query).lookup_hash)
        except ValueError:
            pass
    return or_(
        Candidate.display_name.ilike(f"%{query}%"),
        Candidate.current_title.ilike(f"%{query}%"),
        exists().where(
            CandidateContact.organization_id == Candidate.organization_id,
            CandidateContact.candidate_id == Candidate.id,
            or_(*contact_conditions),
        ),
    )


def _eligible_recruiter(db, organization_id: UUID, user_id: UUID) -> bool:
    return db.scalar(select(exists().where(
        User.organization_id == organization_id,
        User.id == user_id,
        User.status == UserStatus.ACTIVE,
        exists().where(UserRole.user_id == User.id, UserRole.role == "recruiter"),
    )))


def _eligible_hiring_manager(db, organization_id: UUID, user_id: UUID | None) -> bool:
    return user_id is None or bool(db.scalar(select(exists().where(
        User.organization_id == organization_id,
        User.id == user_id,
        User.status == UserStatus.ACTIVE,
        exists().where(
            UserRole.user_id == User.id,
            UserRole.role.in_(("hiring_manager", "recruiting_admin")),
        ),
    ))))


def _department_is_valid(
    db,
    organization_id: UUID,
    department_id: UUID | None,
    *,
    allow_inactive_id: UUID | None = None,
) -> bool:
    return department_id is None or db.scalar(
        select(
            exists().where(
                Department.organization_id == organization_id,
                Department.id == department_id,
                or_(Department.status == "active", Department.id == allow_inactive_id),
            )
        )
    )


def _workflow_template_is_valid(db, organization_id: UUID, template_id: UUID | None) -> bool:
    return template_id is None or bool(db.scalar(select(exists().where(
        WorkflowTemplate.organization_id == organization_id,
        WorkflowTemplate.id == template_id,
        WorkflowTemplate.status == "active",
    ))))


def _application_data(item: Application) -> dict[str, Any]:
    return {"id": str(item.id), "candidate_id": str(item.candidate_id), "job_id": str(item.job_id), "resume_id": str(item.resume_id), "owner_id": str(item.owner_id), "stage": item.stage, "source": item.source, "source_application_id": str(item.source_application_id) if item.source_application_id else None, "human_conclusion": item.human_conclusion, "version": item.version, "updated_at": item.updated_at.isoformat()}


def _resource(data: dict[str, Any], status: int = 200) -> JSONResponse:
    response = JSONResponse({"data": data}, status_code=status)
    if "version" in data:
        response.headers["ETag"] = f'"{data["version"]}"'
    return response


@router.post("/job-definitions", status_code=201, response_model=JobDefinitionResource)
def create_job_definition(payload: JobDefinitionCommand, request: Request, idempotency_key: str | None = Header(None)):
    principal = _principal(request)
    key = _idempotency(request, idempotency_key)
    for value in (principal, key):
        if isinstance(value, JSONResponse):
            return value
    if not AUTH.role_allows(principal, RecruitingAction.MANAGE_JOB):
        return _denied(request)
    command = payload.model_dump()
    with request.app.state.identity_store.sync_session() as db:
        if not _department_is_valid(
            db, principal.organization_id, command["department_id"]
        ):
            return problem(
                request, 422, "department_invalid", "The department is invalid."
            )
        if not _workflow_template_is_valid(
            db, principal.organization_id, command["workflow_template_id"]
        ):
            return problem(request, 422, "workflow_template_invalid", "The workflow template is invalid.")
        if not _eligible_hiring_manager(db, principal.organization_id, command["hiring_owner_id"]):
            return problem(request, 422, "hiring_owner_invalid", "The hiring owner is invalid.")
        try:
            status, body = persisted_idempotent(
                db,
                principal.organization_id,
                principal.user_id,
                "job_definition.create",
                key,
                command,
                lambda: (201, {"data": _job_definition_data(*create_job_definition_record(db, principal.organization_id, principal.user_id, command, trace_id=request.state.trace_id))}),
            )
            response = _job_definition_response(request, body, status)
            if response.status_code >= 400:
                db.rollback()
                return response
            db.commit()
            return response
        except IdempotencyConflict as error:
            db.rollback()
            return _problem_for(request, error)
        except Exception:
            db.rollback()
            raise


@router.get("/job-definitions/{job_id}", response_model=JobDefinitionResource)
def get_job_definition(job_id: UUID, request: Request):
    principal = _principal(request)
    if isinstance(principal, JSONResponse):
        return principal
    with request.app.state.identity_store.sync_session() as db:
        job = _load_job(db, principal, job_id)
        if job is None:
            return _denied(request)
        jd = db.scalar(select(JobJdVersion).where(JobJdVersion.organization_id == principal.organization_id, JobJdVersion.job_id == job_id).order_by(JobJdVersion.version_number.desc()))
        rules = db.scalar(select(ScreeningRuleVersion).where(ScreeningRuleVersion.organization_id == principal.organization_id, ScreeningRuleVersion.job_id == job_id).order_by(ScreeningRuleVersion.version_number.desc()))
        return _job_definition_response(request, {"data": _job_definition_data(job, jd, rules)}, 200)


@router.put("/job-definitions/{job_id}", response_model=JobDefinitionResource)
def replace_job_definition(job_id: UUID, payload: JobDefinitionCommand, request: Request, if_match: str | None = Header(None), idempotency_key: str | None = Header(None)):
    principal = _principal(request)
    expected = _expected_version(request, if_match)
    key = _idempotency(request, idempotency_key)
    for value in (principal, expected, key):
        if isinstance(value, JSONResponse):
            return value
    command = payload.model_dump()
    with request.app.state.identity_store.sync_session() as db:
        job = _load_job(db, principal, job_id, RecruitingAction.MANAGE_JOB)
        if job is None:
            return _denied(request)
        if not _department_is_valid(
            db,
            principal.organization_id,
            command["department_id"],
            allow_inactive_id=job.department_id,
        ):
            return problem(request, 422, "department_invalid", "The department is invalid.")
        if not _workflow_template_is_valid(
            db, principal.organization_id, command["workflow_template_id"]
        ):
            return problem(request, 422, "workflow_template_invalid", "The workflow template is invalid.")
        if not _eligible_hiring_manager(db, principal.organization_id, command["hiring_owner_id"]):
            return problem(request, 422, "hiring_owner_invalid", "The hiring owner is invalid.")
        try:
            status, body = persisted_idempotent(
                db,
                principal.organization_id,
                principal.user_id,
                f"job_definition.replace:{job_id}",
                key,
                command,
                lambda: (200, {"data": _job_definition_data(*replace_job_definition_record(db, principal.organization_id, job_id, principal.user_id, command, expected_version=expected, trace_id=request.state.trace_id))}),
            )
            response = _job_definition_response(request, body, status)
            if response.status_code >= 400:
                db.rollback()
                return response
            db.commit()
            return response
        except (IdempotencyConflict, InvalidStateTransition, ResourceVersionConflict) as error:
            db.rollback()
            return _problem_for(request, error)
        except Exception:
            db.rollback()
            raise


@router.get("/workbench", response_model=WorkbenchResource)
def get_workbench(request: Request, response: Response):
    principal = _principal(request)
    if isinstance(principal, JSONResponse):
        return principal
    if not AUTH.role_allows(principal, RecruitingAction.READ):
        return _denied(request)
    response.headers["Cache-Control"] = "no-store"

    with request.app.state.identity_store.sync_session() as db:
        job_rows = db.execute(select(
            Job,
            Department.name.label("department_name"),
        ).outerjoin(
            Department,
            and_(Department.organization_id == Job.organization_id, Department.id == Job.department_id),
        ).where(
            Job.organization_id == principal.organization_id,
            Job.status == "open",
            AUTH.job_predicate(principal, RecruitingAction.READ, Job),
        ).order_by(Job.updated_at.desc(), Job.id.desc()).limit(20)).all()

        jobs = []
        jobs_by_id = {}
        for job, department_name in job_rows:
            stages = {stage: {"count": 0, "items": []} for stage in WORKBENCH_STAGES}
            data = {
                "id": str(job.id),
                "title": job.title,
                "department_name": department_name,
                "status": "open",
                "updated_at": job.updated_at,
                "active_count": 0,
                "stages": stages,
            }
            jobs.append(data)
            jobs_by_id[job.id] = data

        tasks = {stage: {"count": 0, "items": []} for stage in WORKBENCH_TASK_STAGES}
        notification_candidates = {stage: [] for stage in WORKBENCH_TASK_STAGES}
        if jobs_by_id:
            application_rows = db.execute(select(
                Application.id.label("application_id"),
                Application.candidate_id,
                Application.job_id,
                Application.source,
                Application.stage,
                Application.version.label("application_version"),
                Application.updated_at,
                Candidate.display_name,
                Candidate.current_title,
                Candidate.location,
                _job_permission_projection(principal, RecruitingAction.RECOMMEND, "can_recommend"),
                _job_permission_projection(principal, RecruitingAction.TRANSITION, "can_transition"),
            ).join(
                Candidate,
                and_(
                    Candidate.organization_id == Application.organization_id,
                    Candidate.id == Application.candidate_id,
                ),
            ).join(
                Job,
                and_(Job.organization_id == Application.organization_id, Job.id == Application.job_id),
            ).where(
                Application.organization_id == principal.organization_id,
                Application.job_id.in_(jobs_by_id),
                Application.stage.in_(WORKBENCH_STAGES),
                Candidate.deleted_at.is_(None),
                AUTH.job_predicate(principal, RecruitingAction.READ, Job),
            ).order_by(Application.updated_at.desc(), Application.id.desc())).all()

            next_rounds = _next_interview_rounds(
                db,
                principal.organization_id,
                [row.application_id for row in application_rows],
            )
            for row in application_rows:
                job = jobs_by_id[row.job_id]
                stage = job["stages"][row.stage]
                stage["count"] += 1
                job["active_count"] += 1
                item = _workbench_candidate_data(row, next_rounds.get(row.application_id))
                if len(stage["items"]) < 5:
                    stage["items"].append(item)
                task_action = WORKBENCH_TASK_ACTIONS.get(row.stage)
                can_handle_task = (
                    row.can_recommend if task_action == RecruitingAction.RECOMMEND
                    else row.can_transition if task_action == RecruitingAction.TRANSITION
                    else False
                )
                if row.stage in tasks and can_handle_task:
                    if row.stage == "review":
                        continue
                    task = tasks[row.stage]
                    task["count"] += 1
                    if len(task["items"]) < 5:
                        task["items"].append(item)
                    notification_item = dict(item)
                    notification_item["notification_version"] = workbench_notification_version(row)
                    notification_candidates[row.stage].append(notification_item)

        review_rows=db.execute(select(
            ApplicationReviewTask.id.label("task_id"),
            ApplicationReviewTask.ai_status,
            Application.id.label("application_id"),Application.candidate_id,Application.job_id,
            Application.source,literal("review").label("stage"),Application.updated_at,Candidate.display_name,Candidate.current_title,Candidate.location,
            Application.version.label("application_version"),
            Job.hiring_owner_id,
        ).join(Application,and_(Application.organization_id==ApplicationReviewTask.organization_id,Application.id==ApplicationReviewTask.application_id)).join(Candidate,and_(Candidate.organization_id==Application.organization_id,Candidate.id==Application.candidate_id)).join(Job,and_(Job.organization_id==Application.organization_id,Job.id==Application.job_id)).where(
            ApplicationReviewTask.organization_id==principal.organization_id,
            ApplicationReviewTask.assignee_id==principal.user_id,
            ApplicationReviewTask.status=="open",
            Candidate.deleted_at.is_(None),
            AUTH.job_predicate(principal,RecruitingAction.READ,Job),
        ).order_by(Application.updated_at.desc(),ApplicationReviewTask.id.desc())).all()
        review_task=tasks["review"]
        review_task["count"]=len(review_rows)
        for row in review_rows[:5]:
            item=_workbench_candidate_data(row)
            item.update({
                "stage":"review",
                "task_id":str(row.task_id),
                "ai_status":row.ai_status,
                "config_warning":row.hiring_owner_id is None,
                "candidate_link":f"/candidates/{row.candidate_id}?tab=evidence&application={row.application_id}&job={row.job_id}",
            })
            review_task["items"].append(item)
            notification_item=dict(item)
            notification_item["notification_version"]=workbench_notification_version(
                row,
                stage="review",
                task_id=row.task_id,
                ai_status=row.ai_status,
                config_warning=row.hiring_owner_id is None,
            )
            notification_candidates["review"].append(notification_item)

        notification_application_ids = [
            UUID(item["application_id"])
            for candidates in notification_candidates.values()
            for item in candidates
        ]
        receipt_versions = read_versions(
            db,
            principal.organization_id,
            principal.user_id,
            notification_application_ids,
        )
        notifications = {}
        for stage, candidates in notification_candidates.items():
            unread = [
                item for item in candidates
                if receipt_versions.get(UUID(item["application_id"])) != item["notification_version"]
            ]
            notifications[stage] = {"count": len(unread), "items": unread[:5]}

        return {
            "data": {
                "generated_at": datetime.now(timezone.utc),
                "jobs": jobs,
                "tasks": tasks,
                "notifications": notifications,
                "interviews": {"available": False, "upcoming": [], "pending_feedback": []},
            },
        }


@router.get("/job-owner-options", response_model=JobOwnerOptionCollection)
def list_job_owner_options(request: Request):
    principal = _principal(request)
    if isinstance(principal, JSONResponse):
        return principal
    if not AUTH.role_allows(principal, RecruitingAction.MANAGE_JOB):
        return _denied(request)
    with request.app.state.identity_store.sync_session() as db:
        rows = db.execute(select(User.id, User.display_name).where(
            User.organization_id == principal.organization_id,
            User.status == UserStatus.ACTIVE,
            exists().where(
                UserRole.user_id == User.id,
                UserRole.role.in_(("hiring_manager", "recruiting_admin")),
            ),
        ).order_by(User.display_name.asc(), User.id.asc())).all()
        return {
            "data": [{"id": str(user_id), "name": display_name} for user_id, display_name in rows],
            "meta": {"count": len(rows)},
        }


@router.get("/jobs", response_model=JobCollection)
def list_jobs(
    request: Request,
    q: str | None = None,
    status: Literal["draft", "open", "paused", "closed", "archived"] | None = None,
    department_id: UUID | None = None,
    owner_id: UUID | None = None,
    cursor: str | None = None,
    limit: int = Query(50, ge=1, le=100),
):
    trimmed_q = q.strip() if q else ""
    if len(trimmed_q) > 200:
        return problem(request, 422, "validation_failed", "The request could not be completed.")
    principal = _principal(request)
    if isinstance(principal, JSONResponse): return principal
    normalized_q = trimmed_q.casefold()
    cursor_scope = hashlib.sha256(json.dumps({
        "department_id": str(department_id) if department_id else None,
        "owner_id": str(owner_id) if owner_id else None,
        "q": normalized_q,
        "status": status,
    }, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    cursor_sort = "jobs:-updated_at" if not any((normalized_q, status, department_id, owner_id)) else f"jobs:-updated_at:{cursor_scope}"
    with request.app.state.identity_store.sync_session() as db:
        department = aliased(Department)
        owner = aliased(User)
        hiring_owner = aliased(User)
        effective_owner_id = func.coalesce(Job.hiring_owner_id, Job.owner_id)
        base_conditions = [Job.organization_id == principal.organization_id, _job_scope(principal)]
        page_conditions = list(base_conditions)
        if normalized_q:
            page_conditions.append(Job.title.icontains(normalized_q, autoescape=True))
        if status:
            page_conditions.append(Job.status == status)
        if department_id:
            page_conditions.append(Job.department_id == department_id)
        if owner_id:
            page_conditions.append(effective_owner_id == owner_id)
        query = select(
            Job,
            department.name.label("department_name"),
            owner.display_name.label("owner_name"),
            hiring_owner.display_name.label("hiring_owner_name"),
        ).outerjoin(
            department,
            and_(department.organization_id == Job.organization_id, department.id == Job.department_id),
        ).join(
            owner,
            and_(owner.organization_id == Job.organization_id, owner.id == Job.owner_id),
        ).outerjoin(
            hiring_owner,
            and_(hiring_owner.organization_id == Job.organization_id, hiring_owner.id == Job.hiring_owner_id),
        ).where(*page_conditions)
        if cursor:
            try:
                decoded = request.app.state.recruiting_cursor.decode(cursor, str(principal.organization_id), cursor_sort)
                updated_at = datetime.fromisoformat(decoded["value"])
                query = query.where(or_(Job.updated_at < updated_at, and_(Job.updated_at == updated_at, Job.id < UUID(decoded["id"]))))
            except Exception:
                return _problem_for(request, InvalidCursor())
        rows = db.execute(query.order_by(Job.updated_at.desc(), Job.id.desc()).limit(limit + 1)).all()
        next_cursor = None
        if len(rows) > limit:
            last = rows[limit - 1][0]
            next_cursor = request.app.state.recruiting_cursor.encode(str(principal.organization_id), cursor_sort, last.updated_at.isoformat(), str(last.id))
            rows = rows[:limit]
        page_job_ids = [row[0].id for row in rows]
        funnel_counts: dict[UUID, dict[str, int]] = {}
        if page_job_ids:
            for job_id, stage, count in db.execute(select(
                Application.job_id,
                Application.stage,
                func.count(Application.id),
            ).where(
                Application.organization_id == principal.organization_id,
                Application.job_id.in_(page_job_ids),
            ).group_by(Application.job_id, Application.stage)):
                funnel_counts.setdefault(job_id, {})[stage] = count

        facet_rows = db.execute(select(
            Job.status,
            department.id.label("department_id"),
            department.name.label("department_name"),
            func.coalesce(hiring_owner.id, owner.id).label("owner_id"),
            func.coalesce(hiring_owner.display_name, owner.display_name).label("owner_name"),
        ).outerjoin(
            department,
            and_(department.organization_id == Job.organization_id, department.id == Job.department_id),
        ).join(
            owner,
            and_(owner.organization_id == Job.organization_id, owner.id == Job.owner_id),
        ).outerjoin(
            hiring_owner,
            and_(hiring_owner.organization_id == Job.organization_id, hiring_owner.id == Job.hiring_owner_id),
        ).where(*base_conditions)).all()
        departments = {
            department_id: department_name
            for _, department_id, department_name, _, _ in facet_rows
            if department_id is not None
        }
        owners = {effective_id: effective_name for _, _, _, effective_id, effective_name in facet_rows}
        status_counts = Counter(status_value for status_value, _, _, _, _ in facet_rows)

        data = []
        for job, department_name, owner_name, hiring_owner_name in rows:
            stages = funnel_counts.get(job.id, {})
            data.append({
                **_job_data(job),
                "department_name": department_name,
                "owner_name": owner_name,
                "hiring_owner_name": hiring_owner_name,
                "funnel": {"stages": stages, "total": sum(stages.values())},
            })
        return {
            "data": data,
            "meta": {
                "limit": limit,
                "next_cursor": next_cursor,
                "departments": [
                    {"id": str(facet_id), "name": name}
                    for facet_id, name in sorted(departments.items(), key=lambda item: (item[1].casefold(), str(item[0])))
                ],
                "owners": [
                    {"id": str(facet_id), "name": name}
                    for facet_id, name in sorted(owners.items(), key=lambda item: (item[1].casefold(), str(item[0])))
                ],
                "status_counts": {status_value: status_counts.get(status_value, 0) for status_value in JOB_STATUSES},
            },
        }


@router.post("/jobs", status_code=201, response_model=JobResource)
def create_job(payload: JobCreate, request: Request):
    principal = _principal(request)
    if isinstance(principal, JSONResponse): return principal
    if not AUTH.role_allows(principal, RecruitingAction.MANAGE_JOB): return _denied(request)
    with request.app.state.identity_store.sync_session() as db:
        if not _department_is_valid(
            db, principal.organization_id, payload.department_id
        ):
            return problem(
                request, 422, "department_invalid", "The department is invalid."
            )
        job = Job(organization_id=principal.organization_id, owner_id=principal.user_id, **payload.model_dump())
        db.add(job); db.flush()
        db.add(JobCollaborator(organization_id=principal.organization_id, job_id=job.id, user_id=principal.user_id, access_role="job_owner"))
        db.add(AuditLog(organization_id=principal.organization_id, actor_user_id=principal.user_id, event_type="job.created", outcome="success", trace_id=request.state.trace_id, metadata_json={"job_id": str(job.id)}))
        db.commit()
        return _resource(_job_data(job), 201)


def _load_job(db, principal: Principal, job_id: UUID, action: RecruitingAction = RecruitingAction.READ):
    return db.scalar(select(Job).where(Job.organization_id == principal.organization_id, Job.id == job_id, _job_scope(principal, action)))


@router.get("/jobs/{job_id}", response_model=JobResource)
def get_job(job_id: UUID, request: Request):
    principal = _principal(request)
    if isinstance(principal, JSONResponse): return principal
    with request.app.state.identity_store.sync_session() as db:
        job = _load_job(db, principal, job_id)
        return _denied(request) if job is None else _resource(_job_data(job))


@router.patch("/jobs/{job_id}", response_model=JobResource)
def patch_job(job_id: UUID, payload: JobPatch, request: Request, if_match: str | None = Header(None)):
    principal = _principal(request); expected = _expected_version(request, if_match)
    if isinstance(principal, JSONResponse): return principal
    if isinstance(expected, JSONResponse): return expected
    with request.app.state.identity_store.sync_session() as db:
        job = _load_job(db, principal, job_id, RecruitingAction.MANAGE_JOB)
        if job is None: return _denied(request)
        try:
            job = patch_job_record(db, principal.organization_id, job_id, payload.model_dump(exclude_unset=True), expected_version=expected, actor_user_id=principal.user_id, trace_id=request.state.trace_id)
            db.commit(); return _resource(_job_data(job))
        except ResourceVersionConflict as error:
            db.rollback(); return _problem_for(request, error)


@router.post("/jobs/{job_id}/transitions", response_model=JobResource)
def transition_job(job_id: UUID, payload: Transition, request: Request, if_match: str | None = Header(None), idempotency_key: str | None = Header(None)):
    principal = _principal(request); expected = _expected_version(request, if_match); key = _idempotency(request, idempotency_key)
    for value in (principal, expected, key):
        if isinstance(value, JSONResponse): return value
    with request.app.state.identity_store.sync_session() as db:
        if _load_job(db, principal, job_id, RecruitingAction.TRANSITION) is None: return _denied(request)
        try:
            status, body = persisted_idempotent(db, principal.organization_id, principal.user_id, "job.transition", key, {"job_id": job_id, **payload.model_dump()}, lambda: (200, {"data": _job_data(transition_job_record(db, job_id, payload.target, expected_version=expected, actor_user_id=principal.user_id, trace_id=request.state.trace_id))}))
            db.commit(); response = JSONResponse(body, status_code=status); response.headers["ETag"] = f'"{body["data"]["version"]}"'; return response
        except Exception as error:
            db.rollback(); return _problem_for(request, error)


def _versions(job_id: UUID, request: Request, model, payload: VersionCreate | None = None):
    principal = _principal(request)
    if isinstance(principal, JSONResponse): return principal
    with request.app.state.identity_store.sync_session() as db:
        action = RecruitingAction.CREATE_VERSION if payload is not None else RecruitingAction.READ
        if _load_job(db, principal, job_id, action) is None: return _denied(request)
        if payload is not None:
            if lock_job_for_version_write(db, principal.organization_id, job_id) is None: return _denied(request)
            number = (db.scalar(select(func.max(model.version_number)).where(model.organization_id == principal.organization_id, model.job_id == job_id)) or 0) + 1
            row = model(organization_id=principal.organization_id, job_id=job_id, version_number=number, content=payload.content, created_by=principal.user_id); db.add(row); db.flush()
            db.add(AuditLog(organization_id=principal.organization_id, actor_user_id=principal.user_id, event_type="job.version_created", outcome="success", trace_id=request.state.trace_id, metadata_json={"job_id": str(job_id), "version_number": number, "version_type": model.__tablename__}))
            db.commit()
            return _resource({"id": str(row.id), "version_number": row.version_number, "content": row.content}, 201)
        rows = db.scalars(select(model).where(model.organization_id == principal.organization_id, model.job_id == job_id).order_by(model.version_number)).all()
        return {"data": [{"id": str(row.id), "version_number": row.version_number, "content": row.content} for row in rows], "meta": {"count": len(rows)}}


@router.get("/jobs/{job_id}/jd-versions", response_model=VersionCollection)
def list_jd(job_id: UUID, request: Request): return _versions(job_id, request, JobJdVersion)
@router.post("/jobs/{job_id}/jd-versions", status_code=201, response_model=VersionResource)
def create_jd(job_id: UUID, payload: VersionCreate, request: Request): return _versions(job_id, request, JobJdVersion, payload)
@router.get("/jobs/{job_id}/rule-versions", response_model=VersionCollection)
def list_rules(job_id: UUID, request: Request): return _versions(job_id, request, ScreeningRuleVersion)
@router.post("/jobs/{job_id}/rule-versions", status_code=201, response_model=VersionResource)
def create_rules(job_id: UUID, payload: VersionCreate, request: Request): return _versions(job_id, request, ScreeningRuleVersion, payload)


@router.get("/jobs/{job_id}/funnel", response_model=FunnelResource)
def funnel(job_id: UUID, request: Request):
    principal = _principal(request)
    if isinstance(principal, JSONResponse): return principal
    with request.app.state.identity_store.sync_session() as db:
        if _load_job(db, principal, job_id) is None: return _denied(request)
        counts = Counter(db.scalars(select(Application.stage).where(Application.organization_id == principal.organization_id, Application.job_id == job_id)).all())
        return {"data": {"job_id": str(job_id), "stages": dict(counts), "total": sum(counts.values())}}


@router.get("/candidates", response_model=CandidateCollection)
def list_candidates(request: Request, job_id: UUID | None = None, stage: str | None = None, owner_id: UUID | None = None, source: str | None = None, min_score: int | None = Query(None, ge=0, le=100), q: str | None = Query(None, max_length=200), cursor: str | None = None, limit: int = Query(50, ge=1, le=100), sort: str = "-updated_at"):
    principal = _principal(request)
    if isinstance(principal, JSONResponse): return principal
    if sort != "-updated_at": return problem(request, 422, "validation_failed", "Unsupported sort order.")
    with request.app.state.identity_store.sync_session() as db:
        selected_application = aliased(Application)
        selected_job = aliased(Job)
        latest_result = _latest_screening_results(principal.organization_id)
        latest_evaluation = _latest_llm_evaluations(principal.organization_id)

        def application_conditions(application, job, include_owner: bool):
            conditions = [
                application.organization_id == principal.organization_id,
                AUTH.job_predicate(principal, RecruitingAction.READ, job),
            ]
            if job_id: conditions.append(application.job_id == job_id)
            if stage: conditions.append(application.stage == stage)
            if include_owner and owner_id: conditions.append(application.owner_id == owner_id)
            if source: conditions.append(application.source == source)
            if min_score is not None: conditions.append(latest_evaluation.c.score >= min_score)
            return conditions

        ranked_applications = select(
            selected_application.organization_id,
            selected_application.candidate_id,
            selected_application.id.label("application_id"),
            selected_application.updated_at.label("application_updated_at"),
            func.row_number().over(
                partition_by=(selected_application.organization_id, selected_application.candidate_id),
                order_by=(selected_application.updated_at.desc(), selected_application.id.desc()),
            ).label("application_rank"),
        ).join(
            selected_job,
            and_(selected_job.organization_id == selected_application.organization_id, selected_job.id == selected_application.job_id),
        ).outerjoin(
            latest_result,
            and_(
                latest_result.c.organization_id == selected_application.organization_id,
                latest_result.c.application_id == selected_application.id,
                latest_result.c.result_rank == 1,
            ),
        ).outerjoin(
            latest_evaluation,
            and_(
                latest_evaluation.c.organization_id == selected_application.organization_id,
                latest_evaluation.c.screening_result_id == latest_result.c.screening_result_id,
                latest_evaluation.c.evaluation_rank == 1,
            ),
        ).where(*application_conditions(selected_application, selected_job, True)).subquery()
        sort_updated_at = func.coalesce(ranked_applications.c.application_updated_at, Candidate.updated_at)
        query = select(Candidate, ranked_applications.c.application_id, sort_updated_at.label("sort_updated_at")).outerjoin(
            ranked_applications,
            and_(
                ranked_applications.c.organization_id == Candidate.organization_id,
                ranked_applications.c.candidate_id == Candidate.id,
                ranked_applications.c.application_rank == 1,
            ),
        ).where(Candidate.organization_id == principal.organization_id, _candidate_scope(principal))
        if q: query = query.where(_candidate_search_condition(request, q))
        if any(value is not None for value in (job_id, stage, owner_id, source, min_score)):
            query = query.where(ranked_applications.c.application_id.is_not(None))
        cursor_sort = "candidates:-application_updated_at"
        if cursor:
            try:
                decoded = request.app.state.recruiting_cursor.decode(cursor, str(principal.organization_id), cursor_sort)
                updated_at = datetime.fromisoformat(decoded["value"])
                query = query.where(or_(sort_updated_at < updated_at, and_(sort_updated_at == updated_at, Candidate.id < UUID(decoded["id"]))))
            except Exception as error: return _problem_for(request, InvalidCursor() if not isinstance(error, InvalidCursor) else error)
        if min_score is None:
            rows = db.execute(query.order_by(sort_updated_at.desc(), Candidate.id.desc()).limit(limit + 1)).all()
            application_ids = [application_id for _, application_id, _ in rows if application_id is not None]
            summaries = _candidate_application_summaries(db, principal.organization_id, application_ids)
        else:
            rows = []
            summaries = {}
            scan_query = query
            while len(rows) < limit + 1:
                batch = db.execute(scan_query.order_by(sort_updated_at.desc(), Candidate.id.desc()).limit(limit + 1)).all()
                if not batch:
                    break
                batch_application_ids = [application_id for _, application_id, _ in batch if application_id is not None]
                batch_summaries = _candidate_application_summaries(db, principal.organization_id, batch_application_ids)
                for row in batch:
                    application_id = row.application_id
                    summary = batch_summaries.get(application_id)
                    if summary is None or summary["ai_score"] is None or summary["ai_score"] < min_score:
                        continue
                    rows.append(row)
                    summaries[application_id] = summary
                    if len(rows) == limit + 1:
                        break
                if len(rows) == limit + 1 or len(batch) < limit + 1:
                    break
                last_scanned = batch[-1]
                scan_query = query.where(or_(
                    sort_updated_at < last_scanned.sort_updated_at,
                    and_(sort_updated_at == last_scanned.sort_updated_at, Candidate.id < last_scanned[0].id),
                ))
        next_cursor = None
        if len(rows) > limit:
            last = rows[limit - 1]
            next_cursor = request.app.state.recruiting_cursor.encode(str(principal.organization_id), cursor_sort, last.sort_updated_at.isoformat(), str(last[0].id))
            rows = rows[:limit]
        data = []
        for candidate, application_id, _ in rows:
            item = _candidate_data(db, candidate, principal)
            item["application"] = summaries.get(application_id)
            data.append(item)

        facet_application = aliased(Application)
        facet_job = aliased(Job)
        facet_owner = aliased(User)
        owner_columns=(facet_application.id,facet_owner.id,facet_owner.display_name) if min_score is not None else (facet_owner.id,facet_owner.display_name)
        owner_query = select(*owner_columns).select_from(facet_application).join(
            facet_job,
            and_(facet_job.organization_id == facet_application.organization_id, facet_job.id == facet_application.job_id),
        ).join(
            Candidate,
            and_(Candidate.organization_id == facet_application.organization_id, Candidate.id == facet_application.candidate_id),
        ).join(
            facet_owner,
            and_(facet_owner.organization_id == facet_application.organization_id, facet_owner.id == facet_application.owner_id),
        ).outerjoin(
            latest_result,
            and_(
                latest_result.c.organization_id == facet_application.organization_id,
                latest_result.c.application_id == facet_application.id,
                latest_result.c.result_rank == 1,
            ),
        ).outerjoin(
            latest_evaluation,
            and_(
                latest_evaluation.c.organization_id == facet_application.organization_id,
                latest_evaluation.c.screening_result_id == latest_result.c.screening_result_id,
                latest_evaluation.c.evaluation_rank == 1,
            ),
        ).where(
            *application_conditions(facet_application, facet_job, False),
            Candidate.organization_id == principal.organization_id,
            _candidate_scope(principal),
        )
        if q: owner_query = owner_query.where(_candidate_search_condition(request, q))
        owner_query=owner_query.order_by(facet_owner.display_name,facet_owner.id)
        if min_score is not None:
            owner_rows=db.execute(owner_query.order_by(facet_application.id)).all()
            owner_application_ids=[application_id for application_id,_,_ in owner_rows]
            owner_projections={}
            for offset in range(0,len(owner_application_ids),500):
                owner_projections.update(_screening_projections_for_applications(db,principal.organization_id,owner_application_ids[offset:offset+500]))
            owner_rows=[row for row in owner_rows if (owner_projections.get(row[0]) or {}).get("ai_score") is not None and owner_projections[row[0]]["ai_score"]>=min_score]
            owners=[]; seen_owner_ids=set()
            for _,facet_owner_id,facet_owner_name in owner_rows:
                if facet_owner_id in seen_owner_ids: continue
                seen_owner_ids.add(facet_owner_id); owners.append({"id":str(facet_owner_id),"name":facet_owner_name})
        else:
            owner_rows=db.execute(owner_query.distinct()).all()
            owners=[{"id":str(facet_owner_id),"name":facet_owner_name} for facet_owner_id,facet_owner_name in owner_rows]
        return {"data": data, "meta": {"limit": limit, "next_cursor": next_cursor, "owners": owners}}


@router.post("/candidates", status_code=201, response_model=CandidateResource)
def create_candidate(payload: CandidateCreate, request: Request):
    principal = _principal(request)
    if isinstance(principal, JSONResponse): return principal
    if not AUTH.role_allows(principal, RecruitingAction.MANAGE_CANDIDATE): return _denied(request)
    with request.app.state.identity_store.sync_session() as db:
        owner_id = payload.owner_id
        if owner_id is None and "recruiter" in principal.roles:
            owner_id = principal.user_id
        if owner_id is not None and not _eligible_recruiter(db, principal.organization_id, owner_id):
            return _denied(request)
        candidate = Candidate(organization_id=principal.organization_id, display_name=payload.display_name, current_title=payload.current_title, location=payload.location, owner_id=owner_id); db.add(candidate); db.flush()
        try:
            for contact in payload.contacts:
                protected = request.app.state.contact_cipher.protect(contact.kind, contact.value)
                canonical_kind = contact.kind.strip().casefold()
                db.add(CandidateContact(organization_id=principal.organization_id, candidate_id=candidate.id, kind=canonical_kind, ciphertext=protected.ciphertext, lookup_hash=protected.lookup_hash, masked_value=protected.masked_value))
            db.add(CandidateEvent(organization_id=principal.organization_id, candidate_id=candidate.id, actor_user_id=principal.user_id, event_type="candidate.created", payload={}))
            db.add(AuditLog(organization_id=principal.organization_id, actor_user_id=principal.user_id, event_type="candidate.created", outcome="success", trace_id=request.state.trace_id, metadata_json={"candidate_id": str(candidate.id)}))
            db.flush(); recalculate_candidate_retention(db, principal.organization_id, candidate.id)
            db.commit(); return _resource(_candidate_data(db, candidate, principal), 201)
        except Exception as error:
            db.rollback(); return _problem_for(request, error)


def _load_candidate(db, principal: Principal, candidate_id: UUID, action: RecruitingAction = RecruitingAction.READ):
    return db.scalar(select(Candidate).where(Candidate.organization_id == principal.organization_id, Candidate.id == candidate_id, _candidate_scope(principal, action)))


@router.get("/candidates/{candidate_id}", response_model=CandidateResource)
def get_candidate(candidate_id: UUID, request: Request):
    principal = _principal(request)
    if isinstance(principal, JSONResponse): return principal
    with request.app.state.identity_store.sync_session() as db:
        candidate = _load_candidate(db, principal, candidate_id)
        return _denied(request) if candidate is None else _resource(_candidate_data(db, candidate, principal))


@router.patch("/candidates/{candidate_id}", response_model=CandidateResource)
def patch_candidate(candidate_id: UUID, payload: CandidatePatch, request: Request, if_match: str | None = Header(None)):
    principal = _principal(request); expected = _expected_version(request, if_match)
    if isinstance(principal, JSONResponse): return principal
    if isinstance(expected, JSONResponse): return expected
    with request.app.state.identity_store.sync_session() as db:
        candidate = _load_candidate(db, principal, candidate_id, RecruitingAction.MANAGE_CANDIDATE)
        if candidate is None: return _denied(request)
        changes = payload.model_dump(exclude_unset=True)
        if changes.get("owner_id") is not None and not _eligible_recruiter(db, principal.organization_id, changes["owner_id"]):
            return _denied(request)
        try:
            candidate = patch_candidate_record(db, principal.organization_id, candidate_id, changes, expected_version=expected, actor_user_id=principal.user_id, trace_id=request.state.trace_id)
            db.commit(); return _resource(_candidate_data(db, candidate, principal))
        except (ResourceVersionConflict, CandidateUnavailable) as error:
            db.rollback(); return _problem_for(request, error)


@router.get("/candidates/{candidate_id}/timeline", response_model=TimelineCollection)
def timeline(candidate_id: UUID, request: Request):
    principal = _principal(request)
    if isinstance(principal, JSONResponse): return principal
    with request.app.state.identity_store.sync_session() as db:
        if _load_candidate(db, principal, candidate_id, RecruitingAction.READ) is None: return _denied(request)
        candidate_events = db.scalars(select(CandidateEvent).where(
            CandidateEvent.organization_id == principal.organization_id,
            CandidateEvent.candidate_id == candidate_id,
            CandidateEvent.event_type.in_(("candidate.created", "candidate.corrected", "candidate.note_added", "application.created", "application.updated")),
        )).all()
        authorized_application_ids = {str(value) for value in db.scalars(select(Application.id).join(
            Job, and_(Job.organization_id == Application.organization_id, Job.id == Application.job_id)
        ).where(
            Application.organization_id == principal.organization_id,
            Application.candidate_id == candidate_id,
            AUTH.job_predicate(principal, RecruitingAction.READ, Job),
        )).all()}
        global_events = [
            row for row in candidate_events
            if row.event_type in ("candidate.created", "candidate.corrected")
            or row.payload.get("application_id") in authorized_application_ids
        ]
        stage_events = db.scalars(select(ApplicationStageEvent).join(
            Application, and_(Application.organization_id == ApplicationStageEvent.organization_id, Application.id == ApplicationStageEvent.application_id)
        ).join(Job, and_(Job.organization_id == Application.organization_id, Job.id == Application.job_id)).where(
            ApplicationStageEvent.organization_id == principal.organization_id,
            Application.candidate_id == candidate_id,
            AUTH.job_predicate(principal, RecruitingAction.READ, Job),
        )).all()
        summaries = {
            "candidate.created": "Candidate created",
            "candidate.corrected": "Candidate profile corrected",
            "candidate.note_added": "Candidate note added",
            "application.created": "Application created",
            "application.updated": "Application updated",
            "application.reactivated": "Application reactivated from talent pool",
        }
        rows = sorted([*global_events, *stage_events], key=lambda row: (row.created_at, row.id), reverse=True)
        def summary(row):
            if row.event_type != "application.stage_changed":
                return summaries.get(row.event_type, "Candidate activity recorded")
            source = row.payload.get("from_stage")
            target = row.payload.get("to_stage")
            text = f"Application stage changed from {source} to {target}" if source and target else "Application stage changed"
            reason = row.payload.get("reason_text")
            return f"{text}: {reason.strip()}" if isinstance(reason, str) and reason.strip() else text
        return {"data": [{"id": str(row.id), "event_type": row.event_type, "summary": summary(row), "actor_id": str(row.actor_user_id), "created_at": row.created_at.isoformat()} for row in rows], "meta": {"count": len(rows)}}


@router.get("/candidates/{candidate_id}/notes", response_model=NoteCollection)
def notes(candidate_id: UUID, request: Request, application_id: UUID = Query(...)):
    principal = _principal(request)
    if isinstance(principal, JSONResponse): return principal
    with request.app.state.identity_store.sync_session() as db:
        if _load_candidate_application(db, principal, candidate_id, application_id, RecruitingAction.READ) is None: return _denied(request)
        rows = db.scalars(select(CandidateNote).where(
            CandidateNote.organization_id == principal.organization_id,
            CandidateNote.candidate_id == candidate_id,
            CandidateNote.payload["application_id"].as_string() == str(application_id),
        ).order_by(CandidateNote.created_at)).all()
        return {"data": [{"id": str(row.id), "application_id": row.payload["application_id"], "body": row.payload["body"], "author_id": str(row.actor_user_id), "created_at": row.created_at.isoformat()} for row in rows], "meta": {"count": len(rows)}}


@router.post("/candidates/{candidate_id}/notes", status_code=201, response_model=NoteResource)
def add_note(candidate_id: UUID, payload: NoteCreate, request: Request):
    principal = _principal(request)
    if isinstance(principal, JSONResponse): return principal
    with request.app.state.identity_store.sync_session() as db:
        if _load_candidate_application(db, principal, candidate_id, payload.application_id, RecruitingAction.COMMENT) is None: return _denied(request)
        try:
            lock_active_candidate(db, principal.organization_id, candidate_id)
        except CandidateUnavailable:
            db.rollback()
            return _denied(request)
        application_id = str(payload.application_id)
        note = CandidateNote(organization_id=principal.organization_id, candidate_id=candidate_id, actor_user_id=principal.user_id, event_type="candidate.note", payload={"application_id": application_id, "body": payload.body}); db.add(note); db.add(CandidateEvent(organization_id=principal.organization_id, candidate_id=candidate_id, actor_user_id=principal.user_id, event_type="candidate.note_added", payload={"application_id": application_id})); db.flush()
        recalculate_candidate_retention(db, principal.organization_id, candidate_id)
        db.add(AuditLog(organization_id=principal.organization_id, actor_user_id=principal.user_id, event_type="candidate.note_added", outcome="success", trace_id=request.state.trace_id, metadata_json={"candidate_id": str(candidate_id), "application_id": application_id, "note_id": str(note.id)})); db.commit()
        return _resource({"id": str(note.id), "application_id": application_id, "body": payload.body, "author_id": str(principal.user_id), "created_at": note.created_at.isoformat()}, 201)


@router.get("/candidates/{candidate_id}/resumes", response_model=ResumeCollection)
def resumes(candidate_id: UUID, request: Request):
    principal = _principal(request)
    if isinstance(principal, JSONResponse): return principal
    with request.app.state.identity_store.sync_session() as db:
        if _load_candidate(db, principal, candidate_id) is None: return _denied(request)
        rows = db.scalars(select(Resume).where(
            Resume.organization_id == principal.organization_id,
            Resume.candidate_id == candidate_id,
            _resume_application_scope(principal, RecruitingAction.READ),
        ).order_by(Resume.version_number)).all()
        persisted = {
            profile.resume_id: profile
            for profile in db.scalars(select(ResumeProfile).where(
                ResumeProfile.organization_id == principal.organization_id,
                ResumeProfile.resume_id.in_([row.id for row in rows]),
            )).all()
        } if rows else {}
        data = []
        for row in rows:
            stored = persisted.get(row.id)
            profile = dict(stored.data) if stored is not None else extract_resume_profile(row.parsed_text or "", include_metadata=True)
            profile = {
                "summary": profile.get("summary"),
                "summary_origin": profile.get("summary_origin"),
                "skills": list(profile.get("skills") or []),
                "experience": profile.get("experience"),
                "education": profile.get("education"),
                "status": stored.status if stored is not None else profile.get("status", "unavailable"),
                "source": stored.source if stored is not None else "rules",
            }
            data.append({"id": str(row.id), "candidate_id": str(row.candidate_id), "version_number": row.version_number, "created_at": row.created_at.isoformat(), "profile": profile})
        return {"data": data, "meta": {"count": len(rows)}}


def _load_resume(db, principal: Principal, resume_id: UUID, action: RecruitingAction):
    return db.scalar(select(Resume).where(
        Resume.organization_id == principal.organization_id,
        Resume.id == resume_id,
        _resume_application_scope(principal, action),
    ))


@router.get("/resumes/{resume_id}/preview", response_model=PreviewResource)
def preview(resume_id: UUID, request: Request):
    principal = _principal(request)
    if isinstance(principal, JSONResponse): return principal
    with request.app.state.identity_store.sync_session() as db:
        resume = _load_resume(db, principal, resume_id, RecruitingAction.PREVIEW)
        if resume is None: return _denied(request)
        content = resume.parsed_text or ""
        if len(content.encode("utf-8")) > MAX_PREVIEW_BYTES:
            return problem(request, 422, "preview_too_large", "The preview is unavailable.")
        db.add(AuditLog(organization_id=principal.organization_id, actor_user_id=principal.user_id, event_type="resume.previewed", outcome="success", trace_id=request.state.trace_id, metadata_json={"resume_id": str(resume.id)})); db.commit()
        response = JSONResponse({"data": {"resume_id": str(resume.id), "text": content}}); response.headers["Cache-Control"] = "no-store"; return response


@router.get(
    "/resumes/{resume_id}/file",
    responses={200: {"content": {"application/octet-stream": {"schema": {"type": "string", "format": "binary"}}}}},
)
def preview_file(resume_id: UUID, request: Request):
    principal = _principal(request)
    if isinstance(principal, JSONResponse): return principal
    with request.app.state.identity_store.sync_session() as db:
        resume = _load_resume(db, principal, resume_id, RecruitingAction.PREVIEW)
        if resume is None: return _denied(request)
        file = resume.__table__.metadata.tables["file_objects"]
        row = db.execute(select(file.c.storage_key, file.c.mime_type, file.c.original_filename).where(
            file.c.organization_id == principal.organization_id,
            file.c.id == resume.file_object_id,
        )).one_or_none()
        if row is None: return _denied(request)
        spool = None
        try:
            spool = request.app.state.resume_storage.open_download(row.storage_key, MAX_DOWNLOAD_BYTES)
            disposition = content_disposition(row.original_filename).replace("attachment;", "inline;", 1)
        except (StorageReadFailed, StorageObjectTooLarge, ValueError):
            if spool is not None: spool.close()
            db.rollback()
            return problem(request, 503, "attachment_unavailable", "The attachment is temporarily unavailable.")
        db.add(AuditLog(
            organization_id=principal.organization_id,
            actor_user_id=principal.user_id,
            event_type="resume.previewed",
            outcome="success",
            trace_id=request.state.trace_id,
            metadata_json={"resume_id": str(resume.id), "disposition": "inline"},
        ))
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

        return StreamingResponse(stream_spool(), media_type=row.mime_type, headers={
            "Cache-Control": "no-store",
            "Content-Disposition": disposition,
            "X-Content-Type-Options": "nosniff",
        })


@router.post("/resumes/{resume_id}/download-tickets", status_code=201, response_model=TicketResource)
def issue_ticket(resume_id: UUID, request: Request):
    principal = _principal(request)
    if isinstance(principal, JSONResponse): return principal
    if not AUTH.role_allows(principal, RecruitingAction.ISSUE_TICKET): return _denied(request)
    with request.app.state.identity_store.sync_session() as db:
        resume = _load_resume(db, principal, resume_id, RecruitingAction.ISSUE_TICKET)
        if resume is None: return _denied(request)
        try:
            raw = issue_download_ticket_record(db, principal.organization_id, principal.user_id, resume.id, request.app.state.recruiting_clock, request.app.state.recruiting_tokens)
        except CandidateUnavailable:
            db.rollback()
            return _denied(request)
        db.add(AuditLog(organization_id=principal.organization_id, actor_user_id=principal.user_id, event_type="resume.download_ticket_issued", outcome="success", trace_id=request.state.trace_id, metadata_json={"resume_id": str(resume.id)})); db.commit()
        response = _resource({"token": raw, "expires_in": 60}, 201); response.headers["Cache-Control"] = "no-store"; return response


@router.post("/download-tickets/consume", responses={200: {"content": {"application/octet-stream": {"schema": {"type": "string", "format": "binary"}}}}})
def consume_ticket(payload: TicketConsume, request: Request):
    principal = _principal(request)
    if isinstance(principal, JSONResponse): return principal
    with request.app.state.identity_store.sync_session() as db:
        ticket = db.scalar(select(DownloadTicket).where(DownloadTicket.token_hash == hashlib.sha256(payload.token.encode()).hexdigest()))
        if ticket is None: return _denied(request)
        resume = _load_resume(db, principal, ticket.resume_id, RecruitingAction.DOWNLOAD)
        if resume is None or not AUTH.role_allows(principal, RecruitingAction.DOWNLOAD): return _denied(request)
        file = resume.__table__.metadata.tables["file_objects"]
        row = db.execute(select(file.c.storage_key, file.c.mime_type, file.c.original_filename).where(file.c.organization_id == principal.organization_id, file.c.id == resume.file_object_id)).one()
        spool = None
        try:
            spool = request.app.state.resume_storage.open_download(row.storage_key, MAX_DOWNLOAD_BYTES)
            disposition = content_disposition(row.original_filename)
        except (StorageReadFailed, StorageObjectTooLarge, ValueError):
            if spool is not None: spool.close()
            db.rollback()
            return problem(request, 503, "attachment_unavailable", "The attachment is temporarily unavailable.")
        try: consume_download_ticket_record(db, payload.token, principal.organization_id, principal.user_id, resume.id, request.app.state.recruiting_clock)
        except TicketInvalid as error:
            spool.close(); db.rollback(); return _problem_for(request, error)
        db.add(AuditLog(organization_id=principal.organization_id, actor_user_id=principal.user_id, event_type="resume.downloaded", outcome="success", trace_id=request.state.trace_id, metadata_json={"resume_id": str(resume.id)})); db.commit()
        def stream_spool():
            try:
                while chunk := spool.read(64 * 1024):
                    yield chunk
            finally:
                spool.close()
        response = StreamingResponse(stream_spool(), media_type=row.mime_type); response.headers["Cache-Control"] = "no-store"; response.headers["Content-Disposition"] = disposition; response.headers["X-Content-Type-Options"] = "nosniff"; return response


@router.get("/candidates/{candidate_id}/applications", response_model=ApplicationCollection)
def applications(candidate_id: UUID, request: Request):
    principal = _principal(request)
    if isinstance(principal, JSONResponse): return principal
    with request.app.state.identity_store.sync_session() as db:
        if _load_candidate(db, principal, candidate_id) is None: return _denied(request)
        rows = db.execute(select(Application, Job.title).join(Job, and_(Job.organization_id == Application.organization_id, Job.id == Application.job_id)).where(Application.organization_id == principal.organization_id, Application.candidate_id == candidate_id, _job_scope(principal)).order_by(Application.created_at.desc())).all()
        application_ids = [application.id for application, _ in rows]
        projections=_screening_projections_for_applications(db,principal.organization_id,application_ids)
        next_rounds = _next_interview_rounds(db, principal.organization_id, application_ids)
        empty={"route_result":None,"ai_score":None,"ai_recommendation":None,"llm_status":None,"llm_error_code":None,"llm_evaluation":None}
        return {"data": [{**_application_data(application), "job_title": job_title, "next_interview_round": next_rounds.get(application.id), **projections.get(application.id,empty)} for application, job_title in rows], "meta": {"count": len(rows)}}


@router.post("/jobs/{job_id}/applications", status_code=201, response_model=ApplicationResource)
def create_application(job_id: UUID, payload: ApplicationCreate, request: Request, idempotency_key: str | None = Header(None)):
    principal = _principal(request); key = _idempotency(request, idempotency_key)
    if isinstance(principal, JSONResponse): return principal
    if isinstance(key, JSONResponse): return key
    with request.app.state.identity_store.sync_session() as db:
        if _load_job(db, principal, job_id, RecruitingAction.TRANSITION) is None: return _denied(request)
        owner_id = payload.owner_id or principal.user_id
        if not _eligible_recruiter(db, principal.organization_id, owner_id): return _denied(request)
        candidate = _load_candidate(db, principal, payload.candidate_id, RecruitingAction.MANAGE_CANDIDATE)
        resume = db.scalar(select(Resume).where(
            Resume.organization_id == principal.organization_id,
            Resume.id == payload.resume_id,
            Resume.candidate_id == payload.candidate_id,
        ))
        if candidate is None or resume is None:
            return _denied(request)
        try:
            def action():
                item = create_application_record(db, organization_id=principal.organization_id, candidate_id=payload.candidate_id, job_id=job_id, resume_id=payload.resume_id, owner_id=owner_id, source=payload.source)
                db.add(CandidateEvent(organization_id=principal.organization_id, candidate_id=item.candidate_id, actor_user_id=principal.user_id, event_type="application.created", payload={"application_id": str(item.id), "job_id": str(job_id)})); db.add(AuditLog(organization_id=principal.organization_id, actor_user_id=principal.user_id, event_type="application.created", outcome="success", trace_id=request.state.trace_id, metadata_json={"application_id": str(item.id), "job_id": str(job_id)})); db.flush(); recalculate_candidate_retention(db, principal.organization_id, item.candidate_id); return 201, {"data": _application_data(item)}
            status, body = persisted_idempotent(db, principal.organization_id, principal.user_id, "application.create", key, {"job_id": job_id, **payload.model_dump()}, action); db.commit(); response = JSONResponse(body, status_code=status); response.headers["ETag"] = f'"{body["data"]["version"]}"'; return response
        except Exception as error: db.rollback(); return _problem_for(request, error)


def _load_application(db, principal: Principal, application_id: UUID, action: RecruitingAction = RecruitingAction.READ):
    return db.scalar(select(Application).join(Job, and_(Job.organization_id == Application.organization_id, Job.id == Application.job_id)).join(Candidate, and_(Candidate.organization_id == Application.organization_id, Candidate.id == Application.candidate_id)).where(Application.organization_id == principal.organization_id, Application.id == application_id, Candidate.deleted_at.is_(None), _job_scope(principal, action)))


@router.patch("/applications/{application_id}", response_model=ApplicationResource)
def patch_application(application_id: UUID, payload: ApplicationPatch, request: Request, if_match: str | None = Header(None)):
    principal = _principal(request); expected = _expected_version(request, if_match)
    if isinstance(principal, JSONResponse): return principal
    if isinstance(expected, JSONResponse): return expected
    with request.app.state.identity_store.sync_session() as db:
        changes = payload.model_dump(exclude_unset=True)
        field_actions = {"owner_id": RecruitingAction.MANAGE_CANDIDATE, "human_conclusion": RecruitingAction.RECOMMEND}
        required_actions = {field_actions[field] for field in changes} or {RecruitingAction.MANAGE_CANDIDATE}
        item = None
        for action in required_actions:
            item = _load_application(db, principal, application_id, action)
            if item is None: return _denied(request)
        if "owner_id" in changes and (changes["owner_id"] is None or not _eligible_recruiter(db, principal.organization_id, changes["owner_id"])):
            return _denied(request)
        try:
            item = patch_application_record(db, principal.organization_id, application_id, changes, expected_version=expected, actor_user_id=principal.user_id, trace_id=request.state.trace_id)
            db.commit(); return _resource(_application_data(item))
        except (ResourceVersionConflict, CandidateUnavailable) as error:
            db.rollback(); return _problem_for(request, error)


@router.post("/applications/{application_id}/workflow-actions", response_model=ApplicationResource)
def apply_application_workflow_action(application_id: UUID, payload: WorkflowAction, request: Request, if_match: str | None = Header(None), idempotency_key: str | None = Header(None)):
    principal = _principal(request); expected = _expected_version(request, if_match); key = _idempotency(request, idempotency_key)
    for value in (principal, expected, key):
        if isinstance(value, JSONResponse): return value
    required_action = RecruitingAction.TRANSITION if payload.action.startswith("offer_") else RecruitingAction.RECOMMEND
    with request.app.state.identity_store.sync_session() as db:
        if _load_application(db, principal, application_id, required_action) is None: return _denied(request)
        try:
            def action():
                item = apply_application_workflow_action_record(
                    db,
                    principal.organization_id,
                    application_id,
                    payload.action,
                    expected_version=expected,
                    actor_user_id=principal.user_id,
                    trace_id=request.state.trace_id,
                    reason_text=payload.reason_text,
                )
                return 200, {"data": _application_data(item)}
            status, body = persisted_idempotent(
                db,
                principal.organization_id,
                principal.user_id,
                "application.workflow_action",
                key,
                {"application_id": application_id, **payload.model_dump()},
                action,
            )
            db.commit(); response = JSONResponse(body, status_code=status); response.headers["ETag"] = f'"{body["data"]["version"]}"'; return response
        except Exception as error: db.rollback(); return _problem_for(request, error)
