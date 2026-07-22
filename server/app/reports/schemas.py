from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ExportCreate(StrictModel):
    job_id: UUID | None = None
    from_: datetime | None = Field(default=None, alias="from")
    to: datetime | None = None


class TicketConsume(StrictModel):
    token: str = Field(min_length=20, max_length=512)


class StageMetric(StrictModel):
    stage: str
    current_count: int
    average_time_in_stage_seconds: float


class InterviewMetrics(StrictModel):
    count: int
    required_feedback_completed: int
    required_feedback_total: int
    required_feedback_completion_rate: float
    average_feedback_turnaround_seconds: float


class FunnelData(StrictModel):
    total_applications: int
    stages: list[StageMetric]
    interviews: InterviewMetrics
    can_export: bool


class FunnelResource(StrictModel):
    data: FunnelData


class RateMetric(StrictModel):
    succeeded: int
    total: int
    success_rate: float


class PassMetric(StrictModel):
    passed: int
    total: int
    pass_rate: float


class ScreeningQualityData(StrictModel):
    resume_parsing: RateMetric
    rule_screening: PassMetric
    llm: RateMetric


class ScreeningQualityResource(StrictModel):
    data: ScreeningQualityData


class ExportData(StrictModel):
    id: UUID
    status: str
    format: str
    row_count: int
    created_at: datetime
    completed_at: datetime | None


class ExportResource(StrictModel):
    data: ExportData


class TicketData(StrictModel):
    token: str
    expires_in: int


class TicketResource(StrictModel):
    data: TicketData
