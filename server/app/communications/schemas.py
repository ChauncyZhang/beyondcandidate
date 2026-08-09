from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator


class ApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EmailConfigUpdate(ApiModel):
    host: str = Field(min_length=1, max_length=253, pattern=r"^[A-Za-z0-9.-]+$")
    port: int = Field(ge=1, le=65535)
    tls_mode: str = Field(pattern=r"^(starttls|tls)$")
    username: str = Field(min_length=1, max_length=320)
    password: str | None = Field(default=None, min_length=1, max_length=4096)
    sender_address: EmailStr
    sender_name: str = Field(min_length=1, max_length=200)
    default_reply_to_email: EmailStr | None = None
    default_reply_to_name: str | None = Field(default=None, min_length=1, max_length=200)
    enabled: bool = False

    @field_validator("host", "username", "sender_name", "default_reply_to_name")
    @classmethod
    def strip_value(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value or "\r" in value or "\n" in value:
            raise ValueError("invalid email configuration value")
        return value

    @model_validator(mode="after")
    def reply_to_pair(self) -> "EmailConfigUpdate":
        if (self.default_reply_to_email is None) != (self.default_reply_to_name is None):
            raise ValueError("default reply-to email and name must be configured together")
        return self


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
    reply_to_email: EmailStr | None = None
    reply_to_name: str | None = Field(default=None, min_length=1, max_length=200)

    @field_validator("reply_to_name")
    @classmethod
    def validate_reply_to_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value or "\r" in value or "\n" in value:
            raise ValueError("invalid reply-to name")
        return value

    @model_validator(mode="after")
    def override_pair(self) -> "EmailTestSend":
        if (self.reply_to_email is None) != (self.reply_to_name is None):
            raise ValueError("reply-to override email and name must be supplied together")
        return self
