import re
import uuid
from dataclasses import dataclass, field
from typing import Protocol
from collections.abc import Mapping


class UnsafePayload(ValueError): pass

TYPE_PATTERN = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+){1,7}$")
IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,99}$")


class FieldPolicy(Protocol):
    def validate(self, value: object) -> object: ...


@dataclass(frozen=True)
class OpaqueIdField:
    def validate(self, value: object) -> str:
        if not isinstance(value, str): raise UnsafePayload("opaque ID must be a UUID string")
        try: return str(uuid.UUID(value))
        except ValueError: raise UnsafePayload("opaque ID must be a UUID string") from None

@dataclass(frozen=True)
class IdentifierField:
    def validate(self, value: object) -> str:
        if not isinstance(value, str) or not IDENTIFIER_PATTERN.fullmatch(value): raise UnsafePayload("invalid version identifier")
        return value


@dataclass(frozen=True)
class EnumField:
    values: set[str]
    def validate(self, value: object) -> str:
        if not isinstance(value, str) or value not in self.values or not IDENTIFIER_PATTERN.fullmatch(value): raise UnsafePayload("invalid enum code")
        return value


@dataclass(frozen=True)
class IntegerField:
    minimum: int; maximum: int
    def validate(self, value: object) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or not self.minimum <= value <= self.maximum: raise UnsafePayload("integer outside policy bounds")
        return value


@dataclass(frozen=True)
class BooleanField:
    def validate(self, value: object) -> bool:
        if not isinstance(value, bool): raise UnsafePayload("boolean required")
        return value


@dataclass(frozen=True)
class NullableField:
    inner: FieldPolicy
    def validate(self, value: object) -> object: return None if value is None else self.inner.validate(value)


@dataclass(frozen=True)
class ListField:
    inner: FieldPolicy; maximum_items: int
    def validate(self, value: object) -> list[object]:
        if not isinstance(value, list) or len(value) > self.maximum_items: raise UnsafePayload("list outside policy bounds")
        return [self.inner.validate(item) for item in value]


@dataclass(frozen=True)
class MapField:
    inner: FieldPolicy; allowed_keys: set[str]
    def validate(self, value: object) -> dict[str, object]:
        if not isinstance(value, Mapping) or not set(value) <= self.allowed_keys: raise UnsafePayload("map contains unknown fields")
        return {str(key): self.inner.validate(item) for key, item in value.items()}


@dataclass(frozen=True)
class PayloadSchema:
    fields: Mapping[str, FieldPolicy]
    optional_fields: Mapping[str, FieldPolicy] = field(default_factory=dict)
    def validate(self, payload: Mapping[str, object]) -> dict[str, object]:
        if not isinstance(payload, Mapping) or not set(self.fields) <= set(payload) or not set(payload) <= set(self.fields) | set(self.optional_fields): raise UnsafePayload("payload fields do not match registered schema")
        policies = {**self.fields, **self.optional_fields}
        return {key: policies[key].validate(value) for key, value in payload.items()}


class PayloadPolicyRegistry:
    def __init__(self) -> None: self._jobs: dict[str, PayloadSchema] = {}; self._topics: dict[str, PayloadSchema] = {}
    def validate_type(self, value: str) -> str:
        if not isinstance(value, str) or len(value) > 100 or not TYPE_PATTERN.fullmatch(value): raise UnsafePayload("invalid type or topic")
        return value
    def validate_identifier(self, value: str | None, *, field: str) -> str | None:
        if value is None: return None
        if not isinstance(value, str) or not IDENTIFIER_PATTERN.fullmatch(value): raise UnsafePayload(f"invalid {field}")
        return value
    def register_job(self, job_type: str, schema: PayloadSchema) -> None: self._jobs[self.validate_type(job_type)] = schema
    def register_topic(self, topic: str, schema: PayloadSchema) -> None: self._topics[self.validate_type(topic)] = schema
    def validate_job(self, job_type: str, payload: Mapping[str, object]) -> dict[str, object]:
        job_type = self.validate_type(job_type)
        if job_type not in self._jobs: raise UnsafePayload("unregistered job type")
        return self._jobs[job_type].validate(payload)
    def validate_topic(self, topic: str, payload: Mapping[str, object]) -> dict[str, object]:
        topic = self.validate_type(topic)
        if topic not in self._topics: raise UnsafePayload("unregistered outbox topic")
        return self._topics[topic].validate(payload)


DEFAULT_PAYLOAD_POLICIES = PayloadPolicyRegistry()
DEFAULT_PAYLOAD_POLICIES.register_job("screening.parse_item", PayloadSchema({"organization_id":OpaqueIdField(),"screening_item_id":OpaqueIdField(),"parser_version":IdentifierField()}))
DEFAULT_PAYLOAD_POLICIES.register_job("screening.score_item", PayloadSchema({"organization_id":OpaqueIdField(),"screening_item_id":OpaqueIdField(),"jd_version_id":OpaqueIdField(),"rule_version_id":OpaqueIdField(),"rule_engine_version":IdentifierField()}))
DEFAULT_PAYLOAD_POLICIES.register_job("screening.profile_resume", PayloadSchema({"organization_id":OpaqueIdField(),"resume_id":OpaqueIdField()}))
DEFAULT_PAYLOAD_POLICIES.register_job("screening.llm_score_item", PayloadSchema({"organization_id":OpaqueIdField(),"screening_item_id":OpaqueIdField(),"screening_result_id":OpaqueIdField(),"config_id":OpaqueIdField(),"config_version":IntegerField(1,2147483647),"prompt_version_id":OpaqueIdField()},{"application_id":OpaqueIdField()}))
DEFAULT_PAYLOAD_POLICIES.register_job(
    "screening.llm_finalize_terminal",
    PayloadSchema(
        {
            "organization_id": OpaqueIdField(),
            "source_job_id": OpaqueIdField(),
            "screening_item_id": OpaqueIdField(),
            "terminal_safe_error_code": IdentifierField(),
            "terminal_disposition": EnumField({"route", "technical"}),
        },
        {
            "screening_result_id": OpaqueIdField(),
            "application_id": OpaqueIdField(),
            "config_id": OpaqueIdField(),
            "config_version": IntegerField(1, 2147483647),
            "prompt_version_id": OpaqueIdField(),
        },
    ),
)
DEFAULT_PAYLOAD_POLICIES.register_job(
    "governance.delete_candidate",
    PayloadSchema(
        {
            "organization_id": OpaqueIdField(),
            "deletion_request_id": OpaqueIdField(),
            "request_version": IntegerField(1, 2_147_483_647),
        }
    ),
)
DEFAULT_PAYLOAD_POLICIES.register_job(
    "governance.retention_sweep",
    PayloadSchema(
        {
            "organization_id": OpaqueIdField(),
            "scheduled_date": IdentifierField(),
        }
    ),
)
DEFAULT_PAYLOAD_POLICIES.register_job(
    "governance.redelete_after_restore",
    PayloadSchema(
        {
            "organization_id": OpaqueIdField(),
            "recovery_run_id": OpaqueIdField(),
            "checkpoint_id": OpaqueIdField(),
        }
    ),
)
for _feishu_topic in (
    "feishu.calendar.create",
    "feishu.calendar.update",
    "feishu.calendar.cancel",
):
    DEFAULT_PAYLOAD_POLICIES.register_topic(
        _feishu_topic,
        PayloadSchema(
            {
                "organization_id": OpaqueIdField(),
                "interview_id": OpaqueIdField(),
                "sync_id": OpaqueIdField(),
            }
        ),
    )
