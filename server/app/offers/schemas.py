from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator, model_validator


class OfferSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")


class OfferCommand(OfferSchema):
    application_id: UUID
    template_id: UUID | None = None
    candidate_response_deadline: datetime
    content: dict[str, Any] = Field(min_length=1)
    is_special: bool = False
    special_reason: str | None = None

    @model_validator(mode="after")
    def validate_special_reason(self):
        if self.is_special:
            if not self.special_reason or not self.special_reason.strip():
                raise ValueError("special_reason is required for special offers")
            self.special_reason = self.special_reason.strip()
        elif self.special_reason is not None:
            raise ValueError("special_reason is only allowed for special offers")
        return self


class OfferVersionCommand(OfferSchema):
    content: dict[str, Any] | None = Field(default=None, min_length=1)
    candidate_response_deadline: datetime | None = None
    template_id: UUID | None = None
    is_special: bool | None = None
    special_reason: str | None = None

    @field_validator("content", mode="before")
    @classmethod
    def validate_content_when_present(cls, value):
        if value is None:
            raise ValueError("content must not be null when provided")
        if isinstance(value, dict):
            for field in ("body", "compensation"):
                if field in value and value[field] is None:
                    raise ValueError(f"content.{field} must not be null when provided")
        return value

    @field_validator("candidate_response_deadline", "is_special", mode="before")
    @classmethod
    def validate_required_snapshot_field_when_present(cls, value, info: ValidationInfo):
        if value is None:
            raise ValueError(f"{info.field_name} must not be null when provided")
        return value

    @field_validator("special_reason", mode="before")
    @classmethod
    def normalize_special_reason(cls, value: str | None, info: ValidationInfo) -> str | None:
        is_special = info.data.get("is_special")
        if value is None:
            if is_special is False:
                return None
            raise ValueError("special_reason may only be null when clearing special metadata")
        value = value.strip()
        if not value:
            raise ValueError("special_reason must not be blank when provided")
        if is_special is False:
            raise ValueError("special_reason is only allowed for special offers")
        return value


class OfferApprovalDecision(OfferSchema):
    decision: str
    reason: str | None = None

    @field_validator("decision")
    @classmethod
    def validate_decision(cls, value: str) -> str:
        if value not in {"approved", "rejected"}:
            raise ValueError("decision must be approved or rejected")
        return value

    @model_validator(mode="after")
    def validate_reason(self):
        if self.decision == "rejected" and not (self.reason and self.reason.strip()):
            raise ValueError("reason is required when requesting changes")
        self.reason = self.reason.strip() if self.reason else None
        return self


class OfferTemplateCommand(OfferSchema):
    name: str = Field(min_length=1, max_length=200)
    content: dict[str, Any] = Field(default_factory=dict)
    status: str = "active"

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("name must not be blank")
        return value

    @field_validator("status")
    @classmethod
    def validate_status(cls, value: str) -> str:
        if value not in {"active", "inactive"}:
            raise ValueError("status must be active or inactive")
        return value


class SpecialOfferApproversCommand(OfferSchema):
    approver_ids: list[UUID] = Field(default_factory=list, max_length=50)

    @field_validator("approver_ids")
    @classmethod
    def reject_duplicate_approvers(cls, value: list[UUID]) -> list[UUID]:
        if len(set(value)) != len(value):
            raise ValueError("approver_ids must not contain duplicates")
        return value
