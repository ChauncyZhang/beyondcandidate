from __future__ import annotations

from datetime import date
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from server.app.recruiting.security import ContactCipher


class OnboardingData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    gender: str
    phone: str = Field(min_length=1, max_length=64)
    email: str = Field(min_length=3, max_length=320)
    home_address: str = Field(min_length=1, max_length=1000)

    @field_validator("gender")
    @classmethod
    def validate_gender(cls, value: str) -> str:
        if value not in {"male", "female"}:
            raise ValueError("gender must be male or female")
        return value

    @field_validator("phone")
    @classmethod
    def normalize_phone(cls, value: str) -> str:
        return ContactCipher.normalize("phone", value)

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        return ContactCipher.normalize("email", value)

    @field_validator("home_address")
    @classmethod
    def normalize_address(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("home_address must not be blank")
        return value


class OnboardingUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=200)
    gender: str | None = None
    phone: str | None = Field(default=None, min_length=1, max_length=64)
    email: str | None = Field(default=None, min_length=3, max_length=320)
    home_address: str | None = Field(default=None, min_length=1, max_length=1000)

    @field_validator("gender")
    @classmethod
    def validate_gender(cls, value: str | None) -> str | None:
        if value is not None and value not in {"male", "female"}:
            raise ValueError("gender must be male or female")
        return value

    @field_validator("phone")
    @classmethod
    def normalize_phone(cls, value: str | None) -> str | None:
        return ContactCipher.normalize("phone", value) if value is not None else None

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str | None) -> str | None:
        return ContactCipher.normalize("email", value) if value is not None else None

    @field_validator("name", "home_address")
    @classmethod
    def normalize_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("onboarding text fields must not be blank")
        return value

    @model_validator(mode="after")
    def validate_partial_update(self):
        if not self.model_fields_set:
            raise ValueError("at least one onboarding field is required")
        if any(getattr(self, field) is None for field in self.model_fields_set):
            raise ValueError("onboarding fields must not be null")
        return self


class OnboardingUpdateCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    onboarding_data: OnboardingUpdate | None = None
    expected_start_date: date | None = None

    @model_validator(mode="after")
    def validate_update(self):
        if self.onboarding_data is None and self.expected_start_date is None:
            raise ValueError("onboarding_data or expected_start_date is required")
        return self


class FeishuFieldMapping(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    control_id: str = Field(min_length=1, max_length=255)
    control_type: str = Field(alias="type", min_length=1, max_length=100)
    options: dict[str, str] | None = None

    @field_validator("control_id", "control_type")
    @classmethod
    def strip_value(cls, value: str) -> str:
        return value.strip()


class FeishuDepartmentMappingWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")

    department_id: UUID
    feishu_department_id: str = Field(min_length=1, max_length=255)

    @field_validator("feishu_department_id")
    @classmethod
    def strip_department_id(cls, value: str) -> str:
        return value.strip()


REQUIRED_ONBOARDING_SEMANTICS = {
    "candidate_name",
    "gender",
    "department",
    "job_title",
    "phone",
    "email",
    "home_address",
}


class FeishuOnboardingConfigWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")

    approval_code: str = Field(min_length=1, max_length=255)
    field_mapping: dict[str, FeishuFieldMapping]
    department_mappings: list[FeishuDepartmentMappingWrite] = Field(default_factory=list, max_length=500)
    enabled: bool = False

    @field_validator("approval_code")
    @classmethod
    def strip_approval_code(cls, value: str) -> str:
        return value.strip()

    @model_validator(mode="after")
    def validate_mapping(self):
        if set(self.field_mapping) != REQUIRED_ONBOARDING_SEMANTICS:
            raise ValueError("field_mapping must contain every required onboarding semantic")
        control_ids = [item.control_id for item in self.field_mapping.values()]
        if len(set(control_ids)) != len(control_ids):
            raise ValueError("field_mapping control IDs must be unique")
        department_ids = [item.department_id for item in self.department_mappings]
        if len(set(department_ids)) != len(department_ids):
            raise ValueError("department mappings must be unique")
        gender = self.field_mapping["gender"]
        if gender.options is None or set(gender.options) != {"male", "female"}:
            raise ValueError("gender options must map male and female")
        if any(not value.strip() for value in gender.options.values()):
            raise ValueError("gender option values must not be blank")
        if len(set(gender.options.values())) != len(gender.options):
            raise ValueError("gender option values must be unique")
        return self
