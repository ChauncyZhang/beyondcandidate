from pydantic import BaseModel, ConfigDict, Field, field_validator


class ApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class OcrConfigUpdate(ApiModel):
    provider_id: str = Field(min_length=2, max_length=64, pattern=r"^[a-z][a-z0-9_-]{1,63}$")
    base_url: str = Field(min_length=8, max_length=2048)
    model: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
    enabled: bool = False
    api_key: str | None = Field(default=None, min_length=1, max_length=4096)

    @field_validator("provider_id", mode="before")
    @classmethod
    def normalize_provider_id(cls, value: str) -> str:
        return value.strip().lower() if isinstance(value, str) else value

    @field_validator("base_url")
    @classmethod
    def normalize_base_url(cls, value: str) -> str:
        return value.strip().rstrip("/")


class OcrConfigOut(ApiModel):
    configured: bool
    provider_id: str | None
    base_url: str | None
    model: str | None
    enabled: bool
    version: int
    last_test_status: str | None
    last_test_error_code: str | None
    last_test_latency_ms: int | None
    last_tested_at: str | None
    created_by: str | None
    updated_by: str | None
    created_at: str | None
    updated_at: str | None
    key_configured: bool | None = None


class OcrConfigResource(ApiModel):
    data: OcrConfigOut


class OcrTestOut(ApiModel):
    status: str
    safe_error_code: str | None
    latency_ms: int | None


class OcrTestResource(ApiModel):
    data: OcrTestOut
