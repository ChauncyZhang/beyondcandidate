from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ParticipantInput(StrictModel):
    user_id: UUID
    role: Literal["interviewer", "observer"] = "interviewer"
    required_feedback: bool = True

    @model_validator(mode="after")
    def validate_role(self):
        if self.role == "observer" and self.required_feedback:
            raise ValueError("observers cannot require feedback")
        return self


class ScheduleInput(StrictModel):
    starts_at: datetime
    ends_at: datetime
    participant_ids: list[UUID] = Field(min_length=1, max_length=20)
    buffer_minutes: int = Field(default=15, ge=0, le=120)

    @model_validator(mode="after")
    def validate_schedule(self):
        if self.starts_at.tzinfo is None or self.ends_at.tzinfo is None:
            raise ValueError("schedule timestamps must include a timezone")
        if self.ends_at <= self.starts_at:
            raise ValueError("ends_at must be after starts_at")
        return self


class NewInterviewConflictInput(ScheduleInput):
    application_id: UUID


class InterviewCreate(StrictModel):
    application_id: UUID
    round_name: str = Field(min_length=1, max_length=100)
    method: Literal["video", "onsite", "phone"]
    timezone: str = Field(min_length=1, max_length=64)
    starts_at: datetime
    ends_at: datetime
    location: str | None = Field(default=None, max_length=1000)
    meeting_url: str | None = Field(default=None, max_length=2000)
    participants: list[ParticipantInput] = Field(min_length=1, max_length=20)
    allow_soft_conflict: bool = False

    @model_validator(mode="after")
    def validate_interview(self):
        if self.starts_at.tzinfo is None or self.ends_at.tzinfo is None:
            raise ValueError("schedule timestamps must include a timezone")
        if self.ends_at <= self.starts_at:
            raise ValueError("ends_at must be after starts_at")
        if self.method == "video" and not self.meeting_url:
            raise ValueError("meeting_url is required for video interviews")
        if self.method == "onsite" and not self.location:
            raise ValueError("location is required for onsite interviews")
        if len({item.user_id for item in self.participants}) != len(self.participants):
            raise ValueError("participants must be unique")
        return self


class InterviewPatch(StrictModel):
    round_name: str | None = Field(default=None, min_length=1, max_length=100)
    method: Literal["video", "onsite", "phone"] | None = None
    timezone: str | None = Field(default=None, min_length=1, max_length=64)
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    location: str | None = Field(default=None, max_length=1000)
    meeting_url: str | None = Field(default=None, max_length=2000)
    participants: list[ParticipantInput] | None = Field(default=None, min_length=1, max_length=20)
    allow_soft_conflict: bool = False

    @model_validator(mode="after")
    def validate_patch(self):
        for value in (self.starts_at, self.ends_at):
            if value is not None and value.tzinfo is None:
                raise ValueError("schedule timestamps must include a timezone")
        if self.starts_at is not None and self.ends_at is not None and self.ends_at <= self.starts_at:
            raise ValueError("ends_at must be after starts_at")
        if self.participants is not None and len({item.user_id for item in self.participants}) != len(self.participants):
            raise ValueError("participants must be unique")
        return self


class InterviewTransition(StrictModel):
    target: Literal["confirmed", "completed", "cancelled", "no_show"]
    reason: str | None = Field(default=None, max_length=1000)


class FeedbackDraft(StrictModel):
    ratings: dict[str, int] = Field(default_factory=dict)
    strengths: str | None = Field(default=None, max_length=4000)
    risks: str | None = Field(default=None, max_length=4000)
    conclusion: Literal["strong_recommend", "recommend", "hold", "no_hire"] | None = None
    notes: str | None = Field(default=None, max_length=4000)

    @field_validator("ratings")
    @classmethod
    def validate_ratings(cls, value: dict[str, int]) -> dict[str, int]:
        if any(score < 1 or score > 4 for score in value.values()):
            raise ValueError("ratings must be between 1 and 4")
        return value


class FeedbackAmendment(FeedbackDraft):
    reason: str = Field(min_length=1, max_length=1000)


class DataResource(BaseModel):
    data: dict[str, Any]


class DataCollection(BaseModel):
    data: list[dict[str, Any]]
    meta: dict[str, Any]
