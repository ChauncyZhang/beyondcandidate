from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class GrantInput(StrictModel):
    user_id: UUID
    access_role: Literal["viewer", "manager"] = "viewer"


class PoolCreate(StrictModel):
    name: str = Field(min_length=1, max_length=200)
    purpose: str = Field(min_length=1, max_length=4000)
    visibility: Literal["private", "recruiting_team", "granted"]
    owner_id: UUID
    suitable_roles: list[str] = Field(min_length=1, max_length=50)
    retention_days: int = Field(ge=30, le=3650)
    grants: list[GrantInput] = Field(default_factory=list, max_length=100)

    @field_validator("name", "purpose")
    @classmethod
    def strip_required(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("value must not be blank")
        return value.strip()

    @field_validator("suitable_roles")
    @classmethod
    def clean_roles(cls, values: list[str]) -> list[str]:
        cleaned = [value.strip() for value in values]
        if any(not value for value in cleaned) or len(set(cleaned)) != len(cleaned):
            raise ValueError("suitable roles must be unique and nonblank")
        return cleaned

    @model_validator(mode="after")
    def validate_grants(self):
        if len({grant.user_id for grant in self.grants}) != len(self.grants):
            raise ValueError("grant users must be unique")
        return self


class PoolPatch(StrictModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    purpose: str | None = Field(default=None, min_length=1, max_length=4000)
    visibility: Literal["private", "recruiting_team", "granted"] | None = None
    owner_id: UUID | None = None
    suitable_roles: list[str] | None = Field(default=None, min_length=1, max_length=50)
    retention_days: int | None = Field(default=None, ge=30, le=3650)
    grants: list[GrantInput] | None = Field(default=None, max_length=100)

    @field_validator("name", "purpose")
    @classmethod
    def strip_optional(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("value must not be blank")
        return value.strip() if value is not None else None

    @model_validator(mode="after")
    def validate_collections(self):
        if self.suitable_roles is not None:
            cleaned = [value.strip() for value in self.suitable_roles]
            if any(not value for value in cleaned) or len(set(cleaned)) != len(cleaned):
                raise ValueError("suitable roles must be unique and nonblank")
            self.suitable_roles = cleaned
        if self.grants is not None and len({grant.user_id for grant in self.grants}) != len(self.grants):
            raise ValueError("grant users must be unique")
        return self


class MembershipCreate(StrictModel):
    candidate_id: UUID
    source_application_id: UUID | None = None
    owner_id: UUID
    suitable_roles: list[str] = Field(min_length=1, max_length=50)
    tags: list[str] = Field(default_factory=list, max_length=100)
    reason: str = Field(min_length=1, max_length=4000)
    next_contact_at: datetime | None = None
    retention_until: datetime

    @field_validator("suitable_roles", "tags")
    @classmethod
    def clean_items(cls, values: list[str]) -> list[str]:
        cleaned = [value.strip() for value in values]
        if any(not value for value in cleaned) or len(set(cleaned)) != len(cleaned):
            raise ValueError("items must be unique and nonblank")
        return cleaned

    @model_validator(mode="after")
    def validate_dates(self):
        if self.retention_until.tzinfo is None or (self.next_contact_at is not None and self.next_contact_at.tzinfo is None):
            raise ValueError("timestamps must include a timezone")
        return self


class MembershipPatch(StrictModel):
    owner_id: UUID | None = None
    suitable_roles: list[str] | None = Field(default=None, min_length=1, max_length=50)
    tags: list[str] | None = Field(default=None, max_length=100)
    reason: str | None = Field(default=None, min_length=1, max_length=4000)
    next_contact_at: datetime | None = None
    retention_until: datetime | None = None
    status: Literal["active", "do_not_contact", "blocked"] | None = None

    @field_validator("suitable_roles", "tags")
    @classmethod
    def clean_optional_items(cls, values: list[str] | None) -> list[str] | None:
        if values is None:
            return None
        cleaned = [value.strip() for value in values]
        if any(not value for value in cleaned) or len(set(cleaned)) != len(cleaned):
            raise ValueError("items must be unique and nonblank")
        return cleaned

    @model_validator(mode="after")
    def validate_dates(self):
        if self.next_contact_at is not None and self.next_contact_at.tzinfo is None:
            raise ValueError("next_contact_at must include a timezone")
        if self.retention_until is not None and self.retention_until.tzinfo is None:
            raise ValueError("retention_until must include a timezone")
        return self


class MembershipRemoval(StrictModel):
    reason: str = Field(min_length=1, max_length=1000)


class ReactivationInput(StrictModel):
    job_id: UUID
    resume_id: UUID | None = None


class ReviewReferralInput(StrictModel):
    pass


class DataResource(BaseModel):
    data: dict[str, Any]


class DataCollection(BaseModel):
    data: list[dict[str, Any]]
    meta: dict[str, Any]
