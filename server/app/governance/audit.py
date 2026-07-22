from __future__ import annotations

import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from server.app.identity.models import AuditLog


class AuditValidationError(ValueError):
    pass


@dataclass(frozen=True)
class MetadataField:
    value_type: type
    max_length: int | None = None


_INT = MetadataField(int)
_BOOL = MetadataField(bool)
_SHORT = MetadataField(str, 32)
_CODE = MetadataField(str, 64)

EVENT_METADATA_ALLOWLIST: dict[str, dict[str, MetadataField]] = {
    "retention_policy.updated": {
        "previous_version": _INT,
        "new_version": _INT,
        "terminal_days": _INT,
        "talent_pool_days": _INT,
        "backup_window_days": _INT,
        "affected_candidate_count": _INT,
        "shortened": _BOOL,
    },
    "retention_policy.recalculation_failed": {
        "safe_error_code": _CODE,
        "candidate_count": _INT,
    },
    "authorization.denied": {"permission": _CODE},
    "application.stage_changed": {"from_stage": _SHORT, "to_stage": _SHORT},
    "job.stage_changed": {"from_stage": _SHORT, "to_stage": _SHORT},
    "screening.terminal_routed": {
        "application_id": _CODE,
        "item_id": _CODE,
        "evaluation_id": _CODE,
        "invocation_id": _CODE,
        "from_stage": _SHORT,
        "to_stage": _SHORT,
        "ai_status": _SHORT,
        "recommendation": _SHORT,
        "score": _INT,
        "safe_error_code": _CODE,
    },
    "candidate.created": {},
    "application.created": {},
    "job.created": {},
    "resume.previewed": {},
    "resume.download_ticket_issued": {},
    "resume.downloaded": {},
    "governance.deletion_requested": {
        "request_version": _INT,
        "safe_error_code": _CODE,
    },
    "governance.deletion_approved": {
        "request_version": _INT,
        "terminated_active_application_count": _INT,
        "safe_error_code": _CODE,
    },
    "governance.legal_hold_placed": {
        "request_version": _INT,
        "safe_error_code": _CODE,
    },
    "governance.legal_hold_released": {
        "hold_version": _INT,
        "safe_error_code": _CODE,
    },
    "governance.request_rejected": {"safe_error_code": _CODE},
    "governance.deletion_requests_listed": {"safe_error_code": _CODE},
    "governance.deletion_request_read": {"safe_error_code": _CODE},
    "governance.candidate_status_read": {"safe_error_code": _CODE},
    "governance.deletion_started": {"request_version": _INT},
    "governance.deletion_completed": {"request_version": _INT},
    "governance.deletion_failed": {
        "request_version": _INT,
        "safe_error_code": _CODE,
    },
}

_ALLOWED_CATEGORIES = frozenset({"system", "recruiting", "governance"})
_ALLOWED_OUTCOMES = frozenset({"success", "denied", "failure"})
_EVENT_PATTERN = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$")
_RESOURCE_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_SENSITIVE_KEY_PATTERN = re.compile(
    r"(?:^|_)(?:password|secret|token|email|phone|address|contact|name|resume_text|"
    r"feedback_text|sql|credential|api_key|access_key|private_key|storage_key)(?:$|_)",
    re.IGNORECASE,
)

CATEGORY_PREFIXES = {
    "governance": ("retention_policy.", "governance."),
    "recruiting": (
        "candidate.",
        "application.",
        "job.",
        "resume.",
        "screening.",
        "interview.",
        "talent_pool.",
        "report_export.",
    ),
    "system": ("authentication.", "authorization.", "llm."),
}


def category_for_event(event_type: str) -> str:
    if event_type.startswith(CATEGORY_PREFIXES["governance"]):
        return "governance"
    if event_type.startswith(CATEGORY_PREFIXES["recruiting"]):
        return "recruiting"
    if event_type.startswith(CATEGORY_PREFIXES["system"]):
        return "system"
    return "system"


def _validate_metadata(event_type: str, metadata: Mapping[str, Any]) -> dict[str, Any]:
    fields = EVENT_METADATA_ALLOWLIST.get(event_type)
    if fields is None:
        raise AuditValidationError("event_type has no metadata allowlist")

    validated: dict[str, Any] = {}
    for key, value in metadata.items():
        if _SENSITIVE_KEY_PATTERN.search(key):
            raise AuditValidationError(f"metadata key {key!r} may contain PII or secret data")
        field = fields.get(key)
        if field is None:
            raise AuditValidationError(f"metadata key {key!r} is not allowlisted for {event_type}")
        if isinstance(value, (dict, list, tuple, set)) or value is None:
            raise AuditValidationError(f"metadata key {key!r} must be a scalar")
        if field.value_type is int:
            valid_type = isinstance(value, int) and not isinstance(value, bool)
        else:
            valid_type = isinstance(value, field.value_type)
        if not valid_type:
            raise AuditValidationError(
                f"metadata key {key!r} must be {field.value_type.__name__}"
            )
        if field.max_length is not None and len(value) > field.max_length:
            raise AuditValidationError(
                f"metadata key {key!r} exceeds {field.max_length} characters"
            )
        validated[key] = value
    return validated


def append_audit(
    db: Session,
    *,
    actor: Any,
    category: str,
    event_type: str,
    outcome: str,
    trace_id: str | None,
    resource_type: str | None = None,
    resource_id: uuid.UUID | str | None = None,
    ip_hash: str | None = None,
    metadata: Mapping[str, Any] = {},
) -> AuditLog:
    if category != category_for_event(event_type):
        raise AuditValidationError("category does not match event_type")
    if category not in _ALLOWED_CATEGORIES:
        raise AuditValidationError(f"invalid audit category: {category!r}")
    if outcome not in _ALLOWED_OUTCOMES:
        raise AuditValidationError(f"invalid audit outcome: {outcome!r}")
    if not _EVENT_PATTERN.fullmatch(event_type) or event_type not in EVENT_METADATA_ALLOWLIST:
        raise AuditValidationError(f"invalid or unsupported event_type: {event_type!r}")
    if trace_id is not None and (not trace_id or len(trace_id) > 64):
        raise AuditValidationError("trace_id must contain 1..64 characters")
    if resource_type is not None and not _RESOURCE_PATTERN.fullmatch(resource_type):
        raise AuditValidationError("resource_type must be a stable snake_case identifier")
    if resource_id is not None and resource_type is None:
        raise AuditValidationError("resource_type is required when resource_id is set")
    try:
        normalized_resource_id = uuid.UUID(str(resource_id)) if resource_id is not None else None
    except ValueError as exc:
        raise AuditValidationError("resource_id must be a UUID") from exc
    if ip_hash is not None and not _HASH_PATTERN.fullmatch(ip_hash):
        raise AuditValidationError("ip_hash must be a lowercase SHA-256 hexadecimal digest")

    organization_id = getattr(actor, "organization_id", None)
    actor_user_id = getattr(actor, "user_id", getattr(actor, "id", None))
    if not isinstance(organization_id, uuid.UUID) or not isinstance(actor_user_id, uuid.UUID):
        raise AuditValidationError("actor must provide UUID organization_id and user_id")

    log = AuditLog(
        organization_id=organization_id,
        actor_user_id=actor_user_id,
        category=category,
        event_type=event_type,
        outcome=outcome,
        trace_id=trace_id,
        resource_type=resource_type,
        resource_id=normalized_resource_id,
        ip_hash=ip_hash,
        metadata_json=_validate_metadata(event_type, metadata),
    )
    db.add(log)
    return log
