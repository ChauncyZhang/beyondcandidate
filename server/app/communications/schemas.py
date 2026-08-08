from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator


class ApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EmailConfigUpdate(ApiModel):
    host: str = Field(min_length=1, max_length=253, pattern=r"^[A-Za-z0-9.-]+$")
    port: int = Field(ge=1, le=65535)
    tls_mode: str = Field(pattern=r"^(starttls|tls)$")
    username: str = Field(min_length=1, max_length=320)
    password: str | None = Field(default=None, min_length=1, max_length=4096)
    enabled: bool = False

    @field_validator("host", "username")
    @classmethod
    def strip_value(cls, value: str) -> str:
        return value.strip()


class EmailTemplateUpdate(ApiModel):
    subject_template: str = Field(min_length=1, max_length=998)
    body_template: str = Field(min_length=1, max_length=200_000)
    allowed_variables: list[str] = Field(max_length=64)
    enabled: bool = True

    @field_validator("allowed_variables")
    @classmethod
    def validate_variables(cls, values: list[str]) -> list[str]:
        if len(values) != len(set(values)) or any(not value or len(value) > 64 or not value.replace("_", "").isalnum() for value in values):
            raise ValueError("invalid template variables")
        return values


class EmailTestSend(ApiModel):
    recipient: EmailStr
    reply_to_email: EmailStr
    reply_to_name: str = Field(min_length=1, max_length=200)
