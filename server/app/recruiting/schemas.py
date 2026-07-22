from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator
from server.app.screening.rules import MAX_JD_TEXT_CHARS,MAX_RULE_TERM_CHARS,MAX_RULE_TERMS
from server.app.screening.schemas import LlmEvaluationOut


class ApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Meta(ApiModel):
    limit: int | None = None
    count: int | None = None
    next_cursor: str | None = None


class OwnerFacetOut(ApiModel):
    id: str
    name: str = Field(min_length=1)


class DepartmentFacetOut(ApiModel):
    id: str
    name: str


class CandidateMeta(Meta):
    owners: list[OwnerFacetOut]


class ContactOut(ApiModel):
    kind: str
    value: str


class JobOut(ApiModel):
    id: str
    title: str
    department_id: str | None
    headcount: int
    priority: str
    hiring_owner_id: str | None
    workflow_template_id: str | None
    owner_id: str
    status: str
    version: int
    updated_at: str


class JobFunnelOut(ApiModel):
    stages: dict[str, int]
    total: int


class JobListOut(JobOut):
    department_name: str | None
    owner_name: str = Field(min_length=1)
    hiring_owner_name: str | None
    funnel: JobFunnelOut


class JobMeta(ApiModel):
    limit: int
    next_cursor: str | None
    departments: list[DepartmentFacetOut]
    owners: list[OwnerFacetOut]
    status_counts: dict[str, int]


class JobOwnerOptionMeta(ApiModel):
    count: int


class JobOwnerOptionCollection(ApiModel):
    data: list[OwnerFacetOut]
    meta: JobOwnerOptionMeta


RuleItem = Annotated[str, Field(min_length=1, max_length=MAX_RULE_TERM_CHARS)]


class JobDefinitionCommand(ApiModel):
    title: str = Field(min_length=1, max_length=200)
    department_id: UUID | None = None
    headcount: int = Field(ge=1, le=1000)
    priority: Literal["high", "normal", "low"]
    hiring_owner_id: UUID | None = None
    description: str = Field(min_length=1, max_length=MAX_JD_TEXT_CHARS)
    location: str = Field(max_length=200)
    process_template: str = Field(min_length=1, max_length=100)
    workflow_template_id: UUID | None = None
    llm_enabled: bool
    must_have: list[RuleItem] = Field(max_length=MAX_RULE_TERMS)
    nice_to_have: list[RuleItem] = Field(max_length=MAX_RULE_TERMS)
    publish: bool

    @field_validator("title", "description", "process_template")
    @classmethod
    def nonblank_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("value must not be blank")
        return value.strip()

    @field_validator("must_have", "nice_to_have")
    @classmethod
    def nonblank_rules(cls, values: list[str]) -> list[str]:
        if any(not value.strip() for value in values):
            raise ValueError("rule items must not be blank")
        return [value.strip() for value in values]


class JobJdDefinitionOut(ApiModel):
    id: str
    version_number: int
    description: str
    location: str
    process_template: str
    workflow_template_id: str | None
    llm_enabled: bool


class ScreeningRulesDefinitionOut(ApiModel):
    id: str
    version_number: int
    must_have: list[str]
    nice_to_have: list[str]


class JobDefinitionOut(ApiModel):
    job: JobOut
    jd: JobJdDefinitionOut | None
    rules: ScreeningRulesDefinitionOut | None


class CandidateOut(ApiModel):
    id: str
    display_name: str
    current_title: str | None
    location: str | None
    owner_id: str | None
    version: int
    updated_at: str
    contacts: list[ContactOut]


class CandidateApplicationSummaryOut(ApiModel):
    id: str
    job_id: str
    job_title: str
    resume_id: str
    owner_id: str
    owner_name: str
    stage: str
    source: str
    human_conclusion: str | None
    version: int
    updated_at: str
    next_interview_round: str | None
    rule_score: int | None
    recommendation: str | None
    route_result: Literal["review", "deferred"] | None
    ai_score: int | None
    ai_recommendation: str | None
    llm_status: str | None
    llm_error_code: str | None
    llm_evaluation: LlmEvaluationOut | None


class CandidateListOut(CandidateOut):
    application: CandidateApplicationSummaryOut | None


class ApplicationOut(ApiModel):
    id: str
    candidate_id: str
    job_id: str
    resume_id: str
    owner_id: str
    stage: str
    source: str
    source_application_id: str | None
    human_conclusion: str | None
    version: int
    updated_at: str


class ApplicationHistoryOut(ApplicationOut):
    job_title: str
    next_interview_round: str | None = None
    route_result: Literal["review", "deferred"] | None
    ai_score: int | None
    ai_recommendation: str | None
    llm_status: str | None
    llm_error_code: str | None
    llm_evaluation: LlmEvaluationOut | None


class VersionOut(ApiModel):
    id: str
    version_number: int
    content: dict[str, Any]


class NoteOut(ApiModel):
    id: str
    application_id: str
    body: str
    author_id: str
    created_at: str | None = None


class ResumeProfileOut(ApiModel):
    summary: str | None
    summary_origin: Literal["resume", "generated"] | None = None
    skills: list[str]
    experience: str | None
    education: str | None
    status: Literal["ready", "partial", "unavailable"]
    source: Literal["rules", "llm", "ocr_rules", "ocr_llm"] = "rules"


class ResumeOut(ApiModel):
    id: str
    candidate_id: str
    version_number: int
    created_at: str
    profile: ResumeProfileOut


class TimelineEventOut(ApiModel):
    id: str
    event_type: str
    summary: str
    actor_id: str
    created_at: str


class FunnelOut(ApiModel):
    job_id: str
    stages: dict[str, int]
    total: int


class TicketOut(ApiModel):
    token: str
    expires_in: int


class PreviewOut(ApiModel):
    resume_id: str
    text: str


WorkbenchStage = Literal["new", "review", "contact", "interview_pending", "interviewing", "decision", "passed"]


class WorkbenchCandidateOut(ApiModel):
    application_id: str
    candidate_id: str
    job_id: str
    display_name: str
    current_title: str | None
    location: str | None
    source: str
    stage: WorkbenchStage
    updated_at: datetime
    next_interview_round: str | None = None


class WorkbenchStageOut(ApiModel):
    count: int = Field(ge=0)
    items: list[WorkbenchCandidateOut] = Field(max_length=5)


class WorkbenchReviewTaskOut(WorkbenchCandidateOut):
    task_id: str
    ai_status: Literal["succeeded", "failed"]
    config_warning: bool
    candidate_link: str


class WorkbenchReviewStageOut(ApiModel):
    count: int = Field(ge=0)
    items: list[WorkbenchReviewTaskOut] = Field(max_length=5)


class WorkbenchNotificationOut(WorkbenchCandidateOut):
    notification_version: str = Field(pattern=r"^[0-9a-f]{64}$")


class WorkbenchReviewNotificationOut(WorkbenchReviewTaskOut):
    notification_version: str = Field(pattern=r"^[0-9a-f]{64}$")


class WorkbenchNotificationStageOut(ApiModel):
    count: int = Field(ge=0)
    items: list[WorkbenchNotificationOut] = Field(max_length=5)


class WorkbenchReviewNotificationStageOut(ApiModel):
    count: int = Field(ge=0)
    items: list[WorkbenchReviewNotificationOut] = Field(max_length=5)


class WorkbenchNotificationsOut(ApiModel):
    review: WorkbenchReviewNotificationStageOut
    interview_pending: WorkbenchNotificationStageOut
    decision: WorkbenchNotificationStageOut
    passed: WorkbenchNotificationStageOut


class WorkbenchStagesOut(ApiModel):
    new: WorkbenchStageOut
    review: WorkbenchStageOut
    contact: WorkbenchStageOut
    interview_pending: WorkbenchStageOut
    interviewing: WorkbenchStageOut
    decision: WorkbenchStageOut
    passed: WorkbenchStageOut


class WorkbenchJobOut(ApiModel):
    id: str
    title: str
    department_name: str | None
    status: Literal["open"]
    updated_at: datetime
    active_count: int = Field(ge=0)
    stages: WorkbenchStagesOut


class WorkbenchTasksOut(ApiModel):
    review: WorkbenchReviewStageOut
    interview_pending: WorkbenchStageOut
    decision: WorkbenchStageOut
    passed: WorkbenchStageOut


class WorkbenchInterviewsOut(ApiModel):
    available: Literal[False]
    upcoming: list[Any] = Field(default_factory=list, max_length=0)
    pending_feedback: list[Any] = Field(default_factory=list, max_length=0)


class WorkbenchOut(ApiModel):
    generated_at: datetime
    jobs: list[WorkbenchJobOut] = Field(max_length=20)
    tasks: WorkbenchTasksOut
    notifications: WorkbenchNotificationsOut
    interviews: WorkbenchInterviewsOut


class JobResource(ApiModel): data: JobOut
class JobDefinitionResource(ApiModel): data: JobDefinitionOut
class JobCollection(ApiModel): data: list[JobListOut]; meta: JobMeta
class CandidateResource(ApiModel): data: CandidateOut
class CandidateCollection(ApiModel): data: list[CandidateListOut]; meta: CandidateMeta
class ApplicationResource(ApiModel): data: ApplicationOut
class ApplicationCollection(ApiModel): data: list[ApplicationHistoryOut]; meta: Meta
class VersionResource(ApiModel): data: VersionOut
class VersionCollection(ApiModel): data: list[VersionOut]; meta: Meta
class NoteResource(ApiModel): data: NoteOut
class NoteCollection(ApiModel): data: list[NoteOut]; meta: Meta
class ResumeCollection(ApiModel): data: list[ResumeOut]; meta: Meta
class TimelineCollection(ApiModel): data: list[TimelineEventOut]; meta: Meta
class FunnelResource(ApiModel): data: FunnelOut
class TicketResource(ApiModel): data: TicketOut
class PreviewResource(ApiModel): data: PreviewOut
class WorkbenchResource(ApiModel): data: WorkbenchOut


class Problem(ApiModel):
    type: str
    title: str
    status: int
    detail: str
    code: str
    trace_id: str
    errors: list[dict[str, str]] = Field(default_factory=list)
