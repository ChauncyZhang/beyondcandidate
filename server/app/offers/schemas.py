from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


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
    content: dict[str, Any] = Field(min_length=1)
