import json
import os
import base64
import re
from typing import Literal
from urllib.parse import unquote, urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator


PLACEHOLDERS = {
    "",
    "change-me",
    "changeme",
    "example",
    "password",
    "placeholder",
    "secret",
}
PLACEHOLDER_FRAGMENTS = PLACEHOLDERS - {""}
PLACEHOLDER_PREFIXES = ("change-me", "changeme", "placeholder", "replace-me")
ORGANIZATION_SLUG_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,98}[a-z0-9])?$")


def is_example_placeholder(value: str) -> bool:
    normalized = value.strip().lower()
    return normalized in PLACEHOLDERS or normalized.startswith(PLACEHOLDER_PREFIXES)


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    environment: Literal["development", "test", "production"] = "development"
    database_url: str = "postgresql+asyncpg://app:change-me@postgres/app"
    object_storage_endpoint: str = "minio:9000"
    object_storage_access_key: str = "change-me"
    object_storage_secret_key: str = "change-me"
    object_storage_bucket: str = "resumes"
    contact_encryption_key: SecretStr = SecretStr("change-me")
    contact_lookup_secret: SecretStr = SecretStr("change-me")
    llm_config_encryption_key: SecretStr = SecretStr("change-me")
    feishu_config_encryption_key: SecretStr = SecretStr("change-me")
    llm_provider_allowlist: dict[str, dict[str, object]] = Field(default_factory=dict)
    default_organization_slug: str | None = Field(default=None, min_length=1, max_length=100)
    default_organization_name: str | None = Field(default=None, min_length=1, max_length=200)
    object_storage_secure: bool = False
    object_storage_connect_timeout_seconds: float = Field(default=1, gt=0)
    object_storage_read_timeout_seconds: float = Field(default=3, gt=0)
    object_storage_total_timeout_seconds: float = Field(default=4, gt=0)
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost"])
    worker_check_interval_seconds: float = Field(default=30, ge=0)
    readiness_timeout_seconds: float = Field(default=5, gt=0)
    worker_id: str = Field(default="worker", min_length=1, max_length=200)
    worker_lease_seconds: int = Field(default=60, gt=0)
    worker_heartbeat_seconds: int = Field(default=20, gt=0)
    worker_poll_interval_seconds: float = Field(default=1, ge=0)
    worker_shutdown_timeout_seconds: float = Field(default=30, gt=0)
    worker_cancel_timeout_seconds: float = Field(default=5, gt=0, le=30)
    parser_max_source_bytes: int = Field(default=10 * 1024 * 1024, ge=1024, le=100 * 1024 * 1024)
    parser_max_text_chars: int = Field(default=500_000, ge=1000, le=5_000_000)
    parser_pdf_max_pages: int = Field(default=100, ge=1, le=1000)
    parser_docx_max_entries: int = Field(default=1000, ge=10, le=10000)
    parser_docx_max_uncompressed_bytes: int = Field(default=50 * 1024 * 1024, ge=1024, le=500 * 1024 * 1024)
    parser_docx_max_compression_ratio: int = Field(default=100, ge=1, le=1000)
    parser_hard_timeout_seconds: float = Field(default=15, gt=0, le=120)
    clamav_host: str = Field(default="clamav", min_length=1, max_length=253)
    clamav_port: int = Field(default=3310, ge=1, le=65535)
    clamav_connect_timeout_seconds: float = Field(default=2, gt=0, le=30)
    clamav_read_timeout_seconds: float = Field(default=10, gt=0, le=60)
    clamav_total_timeout_seconds: float = Field(default=15, gt=0, le=120)

    @field_validator("default_organization_slug", "default_organization_name", mode="before")
    @classmethod
    def normalize_optional_organization_identity(cls, value: object) -> object:
        if isinstance(value, str):
            normalized = value.strip()
            return normalized or None
        return value

    @field_validator("default_organization_slug")
    @classmethod
    def validate_default_organization_slug(cls, value: str | None) -> str | None:
        if value is not None and ORGANIZATION_SLUG_PATTERN.fullmatch(value) is None:
            raise ValueError("default organization slug must use lowercase letters, digits, and hyphens")
        return value

    @model_validator(mode="after")
    def validate_default_organization_identity(self) -> "Settings":
        if (self.default_organization_slug is None) != (self.default_organization_name is None):
            raise ValueError("default organization slug and name must be configured together")
        return self

    @model_validator(mode="after")
    def validate_worker_timing(self) -> "Settings":
        if self.worker_heartbeat_seconds * 3 > self.worker_lease_seconds:
            raise ValueError("worker heartbeat must be no more than one-third of lease duration")
        if not 0 < self.worker_poll_interval_seconds <= 60:
            raise ValueError("worker poll interval must be between 0 and 60 seconds")
        if self.worker_shutdown_timeout_seconds > 300:
            raise ValueError("worker shutdown timeout must be at most 300 seconds")
        if self.clamav_total_timeout_seconds < self.clamav_read_timeout_seconds:
            raise ValueError("ClamAV total timeout must cover read timeout")
        return self

    @model_validator(mode="after")
    def validate_production_safety(self) -> "Settings":
        if self.environment != "production":
            return self
        credentials = (
            self.object_storage_access_key.strip().lower(),
            self.object_storage_secret_key.strip().lower(),
            self.contact_encryption_key.get_secret_value().strip().lower(),
            self.contact_lookup_secret.get_secret_value().strip().lower(),
            self.llm_config_encryption_key.get_secret_value().strip().lower(),
            self.feishu_config_encryption_key.get_secret_value().strip().lower(),
        )
        database_url = urlsplit(self.database_url)
        if not database_url.scheme or not database_url.hostname:
            raise ValueError("production database URL is invalid")
        if not database_url.password:
            raise ValueError("production database password is required")
        database_password = unquote(database_url.password).strip().lower()
        if (
            any(is_example_placeholder(value) for value in credentials)
            or is_example_placeholder(database_password)
            or any(marker in database_password for marker in PLACEHOLDER_FRAGMENTS)
        ):
            raise ValueError("production credentials must not use placeholders")
        if not all(credentials):
            raise ValueError("production credentials are required")
        contact_values = [self.contact_encryption_key.get_secret_value(), self.contact_lookup_secret.get_secret_value(), self.llm_config_encryption_key.get_secret_value(), self.feishu_config_encryption_key.get_secret_value()]
        if any(re.fullmatch(r"[A-Za-z0-9_-]{43}=", value) is None for value in contact_values):
            raise ValueError("encryption keys must use padded base64url")
        try:
            decoded = [base64.b64decode(value, altchars=b"-_", validate=True) for value in contact_values]
        except (ValueError, base64.binascii.Error):
            raise ValueError("encryption keys must be base64url") from None
        if any(len(value) != 32 for value in decoded):
            raise ValueError("encryption keys must decode to 32 bytes")
        if any(len(set(value)) < 16 for value in decoded):
            raise ValueError("encryption keys must be high entropy")
        if len({bytes(value) for value in decoded}) != len(decoded):
            raise ValueError("encryption keys must be independent")
        if "*" in self.cors_origins:
            raise ValueError("wildcard CORS is forbidden in production")
        if any(not origin.startswith("https://") for origin in self.cors_origins):
            raise ValueError("production CORS origins must use HTTPS")
        return self

    @classmethod
    def from_environment(cls) -> "Settings":
        values: dict[str, object] = {}
        mapping = {
            "APP_ENVIRONMENT": "environment",
            "DATABASE_URL": "database_url",
            "OBJECT_STORAGE_ENDPOINT": "object_storage_endpoint",
            "OBJECT_STORAGE_ACCESS_KEY": "object_storage_access_key",
            "OBJECT_STORAGE_SECRET_KEY": "object_storage_secret_key",
            "OBJECT_STORAGE_BUCKET": "object_storage_bucket",
            "CONTACT_ENCRYPTION_KEY": "contact_encryption_key",
            "CONTACT_LOOKUP_SECRET": "contact_lookup_secret",
            "LLM_CONFIG_ENCRYPTION_KEY": "llm_config_encryption_key",
            "FEISHU_CONFIG_ENCRYPTION_KEY": "feishu_config_encryption_key",
            "DEFAULT_ORGANIZATION_SLUG": "default_organization_slug",
            "DEFAULT_ORGANIZATION_NAME": "default_organization_name",
            "OBJECT_STORAGE_SECURE": "object_storage_secure",
            "OBJECT_STORAGE_CONNECT_TIMEOUT_SECONDS": "object_storage_connect_timeout_seconds",
            "OBJECT_STORAGE_READ_TIMEOUT_SECONDS": "object_storage_read_timeout_seconds",
            "OBJECT_STORAGE_TOTAL_TIMEOUT_SECONDS": "object_storage_total_timeout_seconds",
            "WORKER_CHECK_INTERVAL_SECONDS": "worker_check_interval_seconds",
            "READINESS_TIMEOUT_SECONDS": "readiness_timeout_seconds",
            "WORKER_ID": "worker_id",
            "WORKER_LEASE_SECONDS": "worker_lease_seconds",
            "WORKER_HEARTBEAT_SECONDS": "worker_heartbeat_seconds",
            "WORKER_POLL_INTERVAL_SECONDS": "worker_poll_interval_seconds",
            "WORKER_SHUTDOWN_TIMEOUT_SECONDS": "worker_shutdown_timeout_seconds",
            "WORKER_CANCEL_TIMEOUT_SECONDS": "worker_cancel_timeout_seconds",
            "PARSER_MAX_SOURCE_BYTES": "parser_max_source_bytes",
            "PARSER_MAX_TEXT_CHARS": "parser_max_text_chars",
            "PARSER_PDF_MAX_PAGES": "parser_pdf_max_pages",
            "PARSER_DOCX_MAX_ENTRIES": "parser_docx_max_entries",
            "PARSER_DOCX_MAX_UNCOMPRESSED_BYTES": "parser_docx_max_uncompressed_bytes",
            "PARSER_DOCX_MAX_COMPRESSION_RATIO": "parser_docx_max_compression_ratio",
            "PARSER_HARD_TIMEOUT_SECONDS": "parser_hard_timeout_seconds",
            "CLAMAV_HOST": "clamav_host",
            "CLAMAV_PORT": "clamav_port",
            "CLAMAV_CONNECT_TIMEOUT_SECONDS": "clamav_connect_timeout_seconds",
            "CLAMAV_READ_TIMEOUT_SECONDS": "clamav_read_timeout_seconds",
            "CLAMAV_TOTAL_TIMEOUT_SECONDS": "clamav_total_timeout_seconds",
        }
        for env_name, field_name in mapping.items():
            if env_name in os.environ:
                values[field_name] = os.environ[env_name]
        if "CORS_ORIGINS" in os.environ:
            values["cors_origins"] = json.loads(os.environ["CORS_ORIGINS"])
        if "LLM_PROVIDER_ALLOWLIST_JSON" in os.environ:
            values["llm_provider_allowlist"] = json.loads(os.environ["LLM_PROVIDER_ALLOWLIST_JSON"])
        return cls.model_validate(values)


class GovernanceSettings(BaseModel):
    """Worker-only credentials for privileged deletion execution."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    environment: Literal["development", "test", "production"] = "development"
    database_url: SecretStr = SecretStr("")
    storage_endpoint: str = "minio:9000"
    storage_secure: bool = False
    delete_access_key: str = ""
    delete_secret_key: SecretStr = SecretStr("")
    resume_bucket: str = "resumes"
    resume_prefix: str = "clean/"
    object_storage_bucket: str = "resumes"
    export_bucket: str = "resumes"
    export_prefix: str = "exports/"
    ledger_access_key: str = ""
    ledger_secret_key: SecretStr = SecretStr("")
    ledger_bucket: str = "governance-ledger"
    ledger_prefix: str = "deletions/"
    signing_key: SecretStr = SecretStr("")
    retention_sweep_batch_size: int = Field(default=100, ge=1, le=1_000)
    recovery_max_ledgers: int = Field(default=10_000, ge=1, le=100_000)

    @model_validator(mode="after")
    def validate_credentials(self) -> "GovernanceSettings":
        database = urlsplit(self.database_url.get_secret_value())
        if not database.scheme or not database.hostname or not database.username:
            raise ValueError("governance database URL is invalid")
        if not database.password:
            raise ValueError("governance database password is required")
        credentials = (
            self.delete_access_key,
            self.delete_secret_key.get_secret_value(),
            self.ledger_access_key,
            self.ledger_secret_key.get_secret_value(),
            self.signing_key.get_secret_value(),
        )
        database_password = unquote(database.password)
        if self.environment == "production":
            if (
                any(not value or is_example_placeholder(value) for value in credentials)
                or is_example_placeholder(database_password)
                or any(marker in database_password.strip().lower() for marker in PLACEHOLDER_FRAGMENTS)
            ):
                raise ValueError("production governance credentials are required")
            signing_key = self.signing_key.get_secret_value()
            if (
                len(signing_key.encode("utf-8")) < 32
                or signing_key != signing_key.strip()
                or any(character.isspace() for character in signing_key)
                or any(
                    marker in signing_key.lower() for marker in PLACEHOLDER_FRAGMENTS
                )
                or len(set(signing_key)) < 12
            ):
                raise ValueError("production ledger signing key must be high entropy")
            if self.export_bucket != self.object_storage_bucket:
                raise ValueError("production governance export bucket must match object storage bucket")
            if self.export_prefix != "exports/":
                raise ValueError("production governance export prefix must be exports/")
        if self.delete_access_key == self.ledger_access_key:
            raise ValueError("governance access keys must be independent")
        secret_values = (
            self.delete_secret_key.get_secret_value(),
            self.ledger_secret_key.get_secret_value(),
            self.signing_key.get_secret_value(),
        )
        if any(
            hmac_compare(left, right)
            for index, left in enumerate(secret_values)
            for right in secret_values[index + 1 :]
        ):
            raise ValueError("governance secret keys must be independent")
        if any(
            not value or value.startswith("/") or not value.endswith("/")
            for value in (self.resume_prefix, self.export_prefix, self.ledger_prefix)
        ):
            raise ValueError("governance prefixes must be non-empty relative paths ending in /")
        return self

    def validate_runtime_separation(
        self,
        *,
        database_url: str,
        object_access_key: str,
        object_secret_key: str,
    ) -> None:
        ordinary = urlsplit(database_url)
        governance = urlsplit(self.database_url.get_secret_value())
        if ordinary.username == governance.username:
            raise ValueError("governance database user must differ from application database user")
        if ordinary.password and governance.password and hmac_compare(
            unquote(ordinary.password), unquote(governance.password)
        ):
            raise ValueError("governance database passwords must differ")
        if object_access_key in {self.delete_access_key, self.ledger_access_key} or any(
            hmac_compare(object_secret_key, value.get_secret_value())
            for value in (self.delete_secret_key, self.ledger_secret_key)
        ):
            raise ValueError("governance object credentials must differ from application object credentials")

    @classmethod
    def from_environment(cls, ordinary: Settings | None = None) -> "GovernanceSettings":
        mapping = {
            "APP_ENVIRONMENT": "environment",
            "GOVERNANCE_DATABASE_URL": "database_url",
            "GOVERNANCE_STORAGE_ENDPOINT": "storage_endpoint",
            "GOVERNANCE_STORAGE_SECURE": "storage_secure",
            "GOVERNANCE_DELETE_ACCESS_KEY": "delete_access_key",
            "GOVERNANCE_DELETE_SECRET_KEY": "delete_secret_key",
            "GOVERNANCE_RESUME_BUCKET": "resume_bucket",
            "GOVERNANCE_RESUME_PREFIX": "resume_prefix",
            "OBJECT_STORAGE_BUCKET": "object_storage_bucket",
            "GOVERNANCE_EXPORT_BUCKET": "export_bucket",
            "GOVERNANCE_EXPORT_PREFIX": "export_prefix",
            "GOVERNANCE_LEDGER_ACCESS_KEY": "ledger_access_key",
            "GOVERNANCE_LEDGER_SECRET_KEY": "ledger_secret_key",
            "GOVERNANCE_LEDGER_BUCKET": "ledger_bucket",
            "GOVERNANCE_LEDGER_PREFIX": "ledger_prefix",
            "GOVERNANCE_LEDGER_SIGNING_KEY": "signing_key",
            "GOVERNANCE_RETENTION_SWEEP_BATCH_SIZE": "retention_sweep_batch_size",
            "GOVERNANCE_RECOVERY_MAX_LEDGERS": "recovery_max_ledgers",
        }
        values = {
            field_name: os.environ[env_name]
            for env_name, field_name in mapping.items()
            if env_name in os.environ
        }
        settings = cls.model_validate(values)
        if ordinary is not None:
            settings.validate_runtime_separation(
                database_url=ordinary.database_url,
                object_access_key=ordinary.object_storage_access_key,
                object_secret_key=ordinary.object_storage_secret_key,
            )
        return settings


def hmac_compare(left: str, right: str) -> bool:
    import hmac

    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))
