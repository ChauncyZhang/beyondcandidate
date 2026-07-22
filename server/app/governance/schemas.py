from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ActorOut(StrictModel):
    id: UUID | None
    display_name: str


class AuditResourceOut(StrictModel):
    type: str
    id: UUID
    label: str | None


class AuditLogOut(StrictModel):
    id: UUID
    created_at: datetime
    actor: ActorOut
    category: str
    event_type: str
    resource: AuditResourceOut | None
    outcome: str
    network_ref: str | None
    trace_id: str | None
    summary: str


class AuditMeta(StrictModel):
    next_cursor: str | None
    limit: int


class AuditCollection(StrictModel):
    data: list[AuditLogOut]
    meta: AuditMeta


class RetentionValues(StrictModel):
    terminal_days: int = Field(ge=30, le=3650)
    talent_pool_days: int = Field(ge=30, le=3650)
    backup_window_days: int = Field(ge=30, le=3650)


class RetentionPolicyPatch(RetentionValues):
    impact_token: str | None = Field(default=None, min_length=40, max_length=4096)


class RetentionPolicyOut(RetentionValues):
    id: UUID
    version: int
    updated_at: datetime
    updated_by: ActorOut


class RetentionPolicyResource(StrictModel):
    data: RetentionPolicyOut


class RetentionPreviewOut(StrictModel):
    current_version: int
    shortening: bool
    affected_candidate_count: int
    impact_token: str
    expires_at: datetime


class RetentionPreviewResource(StrictModel):
    data: RetentionPreviewOut


class DeletionRequestCreate(StrictModel):
    reason_code: str = Field(pattern=r"^(candidate_request|administrator_request)$")


class DeletionTransitionCreate(StrictModel):
    target_status: str = Field(pattern=r"^approved$")
    terminate_active_applications: bool = False


class LegalHoldCreate(StrictModel):
    reason: str = Field(min_length=1, max_length=1000)

    @field_validator("reason")
    @classmethod
    def reason_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("reason must not be blank")
        return value


class LegalHoldReleaseCreate(StrictModel):
    reason: str = Field(min_length=1, max_length=1000)

    @field_validator("reason")
    @classmethod
    def reason_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("reason must not be blank")
        return value


class ImpactCounts(StrictModel):
    contacts: int = Field(ge=0)
    resumes: int = Field(ge=0)
    applications: int = Field(ge=0)
    screening_records: int = Field(ge=0)
    interviews: int = Field(ge=0)
    feedback_records: int = Field(ge=0)
    talent_memberships: int = Field(ge=0)
    resume_objects: int = Field(ge=0)
    temporary_exports: int = Field(ge=0)


class DeletionImpactOut(StrictModel):
    schema_version: int
    candidate_ref: UUID
    candidate_version: int
    policy_version: int
    counts: ImpactCounts
    backup_window_ends_at: datetime


class DeletionRequestOut(StrictModel):
    id: UUID
    status: str
    version: int
    reason_code: str
    requested_at: datetime
    approved_at: datetime | None
    safe_error_code: str | None
    active_application_count: int | None = Field(default=None, ge=0)
    impact: DeletionImpactOut


class DeletionRequestResource(StrictModel):
    data: DeletionRequestOut


class DeletionRequestMeta(StrictModel):
    next_cursor: str | None
    limit: int


class DeletionRequestCollection(StrictModel):
    data: list[DeletionRequestOut]
    meta: DeletionRequestMeta


class LegalHoldOut(StrictModel):
    id: UUID
    status: str
    reason: str | None
    placed_at: datetime
    released_at: datetime | None
    version: int


class LegalHoldResource(StrictModel):
    data: LegalHoldOut


class GovernanceStatusOut(BaseModel):
    model_config = ConfigDict(extra="forbid", exclude_none=True)

    deletion_status: str | None
    deletion_request_id: UUID | None
    legal_hold_active: bool
    legal_hold_reason: str | None = None
    legal_hold_id: UUID | None = None
    legal_hold_version: int | None = Field(default=None, ge=1)


class GovernanceStatusResource(StrictModel):
    data: GovernanceStatusOut
