from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


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

    @field_validator("special_reason")
    @classmethod
    def normalize_special_reason(cls, value: str | None) -> str | None:
        return value.strip() if value is not None else None
