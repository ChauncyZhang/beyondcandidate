from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import select

from server.app.governance.audit import append_audit
from server.app.governance.authorization import can_view_recruiting_resource
from server.app.governance.models import RetentionPolicy
from server.app.governance.retention import (
    aware,
    candidate_due_dates,
    lock_all_candidate_retention_facts,
    recalculate_due_dates,
)
from server.app.identity.models import Job, User
from server.app.identity.policy import Principal
from server.app.recruiting.models import Candidate


class GovernanceError(Exception):
    code = "governance_error"


class InvalidGovernanceToken(GovernanceError):
    code = "validation_failed"


class ResourceVersionConflict(GovernanceError):
    code = "resource_version_conflict"


class RetentionPreviewRequired(GovernanceError):
    code = "retention_preview_required"


class RetentionPreviewInvalid(GovernanceError):
    code = "retention_preview_invalid"


class RetentionPreviewExpired(GovernanceError):
    code = "retention_preview_expired"


class RetentionPreviewStale(GovernanceError):
    code = "retention_preview_stale"


class RetentionPreviewStaleImpact(GovernanceError):
    code = "retention_preview_stale_impact"


def utc_rfc3339(value: datetime) -> str:
    return aware(value).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class GovernanceTokenCodec:
    def __init__(self, secret: bytes) -> None:
        self._secret = secret

    def encode(self, payload: dict[str, Any]) -> str:
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        signature = hmac.new(self._secret, raw, hashlib.sha256).digest()
        return base64.urlsafe_b64encode(signature + raw).decode().rstrip("=")

    def decode(self, token: str) -> dict[str, Any]:
        try:
            raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
            signature, payload = raw[:32], raw[32:]
            expected = hmac.new(self._secret, payload, hashlib.sha256).digest()
            if len(signature) != 32 or not hmac.compare_digest(signature, expected):
                raise InvalidGovernanceToken
            decoded = json.loads(payload)
            if not isinstance(decoded, dict):
                raise InvalidGovernanceToken
            return decoded
        except (ValueError, TypeError, json.JSONDecodeError):
            raise InvalidGovernanceToken from None


def derive_governance_key(root_secret: bytes, purpose: str) -> bytes:
    if purpose not in {"audit-cursor", "retention-preview"}:
        raise ValueError("unsupported governance key purpose")
    return hmac.new(
        root_secret,
        f"ux09/governance/{purpose}/v1".encode(),
        hashlib.sha256,
    ).digest()


SUMMARY_BY_EVENT = {
    "authentication.login": "Authentication attempt recorded.",
    "authentication.logout": "Authentication session ended.",
    "authorization.denied": "Authorization was denied.",
    "candidate.created": "Candidate record created.",
    "application.created": "Application record created.",
    "job.created": "Job record created.",
    "resume.previewed": "Resume preview recorded.",
    "resume.download_ticket_issued": "Resume download authorized.",
    "resume.downloaded": "Resume download recorded.",
    "retention_policy.updated": "Retention policy updated.",
    "retention_policy.recalculation_failed": "Retention recalculation failed.",
}


def audit_summary(event_type: str, metadata: Any) -> str:
    base = SUMMARY_BY_EVENT.get(event_type, "Governance event recorded.")
    if event_type == "retention_policy.updated" and isinstance(metadata, dict):
        version = metadata.get("new_version")
        if isinstance(version, int) and not isinstance(version, bool):
            return f"Retention policy updated to version {version}."
    return base


def safe_network_ref(ip_hash: str | None) -> str | None:
    if ip_hash is None or not re.fullmatch(r"[0-9a-f]{64}", ip_hash):
        return None
    return ip_hash[:12]


def resource_projection(db, principal: Principal, resource_type: str | None, resource_id: UUID | None):
    if resource_type is None or resource_id is None:
        return None
    if resource_type == "retention_policy" and principal.roles & {"system_admin", "recruiting_admin"}:
        return {"type": resource_type, "id": resource_id, "label": "Retention policy"}
    if not can_view_recruiting_resource(db, principal, resource_type, resource_id):
        return None
    label = None
    if resource_type == "candidate":
        label = db.scalar(
            select(Candidate.display_name).where(
                Candidate.organization_id == principal.organization_id,
                Candidate.id == resource_id,
            )
        )
    elif resource_type == "job":
        label = db.scalar(
            select(Job.title).where(
                Job.organization_id == principal.organization_id,
                Job.id == resource_id,
            )
        )
    return {"type": resource_type, "id": resource_id, "label": label}


def policy_projection(db, policy: RetentionPolicy) -> dict[str, Any]:
    updater = db.get(User, policy.updated_by) if policy.updated_by else None
    return {
        "id": policy.id,
        "terminal_days": policy.terminal_days,
        "talent_pool_days": policy.talent_pool_days,
        "backup_window_days": policy.backup_window_days,
        "version": policy.version,
        "updated_at": aware(policy.updated_at),
        "updated_by": {
            "id": updater.id if updater else None,
            "display_name": updater.display_name if updater else "System migration",
        },
    }


def affected_candidate_ids(db, organization_id: UUID, terminal_days: int) -> tuple[list[UUID], dict[UUID, datetime | None]]:
    due = candidate_due_dates(db, organization_id, terminal_days)
    current = dict(
        db.execute(
            select(Candidate.id, Candidate.retention_due_at).where(
                Candidate.organization_id == organization_id
            )
        ).all()
    )

    def normalized(value):
        return aware(value) if value is not None else None

    affected = sorted(
        (candidate_id for candidate_id, value in due.items() if normalized(value) != normalized(current.get(candidate_id))),
        key=str,
    )
    return affected, due


def impact_digest(candidate_ids: list[UUID]) -> str:
    raw = "\n".join(sorted(str(candidate_id) for candidate_id in candidate_ids)).encode()
    return hashlib.sha256(raw).hexdigest()


def preview_retention(db, principal: Principal, values: dict[str, int], codec: GovernanceTokenCodec, now: datetime):
    policy = db.scalar(
        select(RetentionPolicy).where(RetentionPolicy.organization_id == principal.organization_id)
    )
    shortening = any(values[name] < getattr(policy, name) for name in values)
    affected, _ = affected_candidate_ids(db, principal.organization_id, values["terminal_days"])
    expires_at = aware(now) + timedelta(minutes=10)
    token = codec.encode(
        {
            "kind": "retention_impact",
            "organization_id": str(principal.organization_id),
            "version": policy.version,
            "values": values,
            "impact_hash": impact_digest(affected),
            "expires_at": int(expires_at.timestamp()),
        }
    )
    return {
        "current_version": policy.version,
        "shortening": shortening,
        "affected_candidate_count": len(affected),
        "impact_token": token,
        "expires_at": expires_at,
    }


def _verify_preview(
    token: str | None,
    codec: GovernanceTokenCodec,
    principal: Principal,
    policy: RetentionPolicy,
    values: dict[str, int],
    affected: list[UUID],
    now: datetime,
) -> None:
    if token is None:
        raise RetentionPreviewRequired
    try:
        payload = codec.decode(token)
    except InvalidGovernanceToken as error:
        raise RetentionPreviewInvalid from error
    required = {"kind", "organization_id", "version", "values", "impact_hash", "expires_at"}
    if set(payload) != required or payload.get("kind") != "retention_impact":
        raise RetentionPreviewInvalid
    if payload.get("organization_id") != str(principal.organization_id) or payload.get("values") != values:
        raise RetentionPreviewInvalid
    if not isinstance(payload.get("expires_at"), int):
        raise RetentionPreviewInvalid
    if payload["expires_at"] <= int(aware(now).timestamp()):
        raise RetentionPreviewExpired
    if payload.get("version") != policy.version:
        raise RetentionPreviewStale
    if payload.get("impact_hash") != impact_digest(affected):
        raise RetentionPreviewStaleImpact


def update_retention_policy(
    db,
    principal: Principal,
    values: dict[str, int],
    impact_token: str | None,
    expected_version: int,
    codec: GovernanceTokenCodec,
    now: datetime,
    trace_id: str | None,
) -> dict[str, Any]:
    policy = db.scalar(
        select(RetentionPolicy)
        .where(RetentionPolicy.organization_id == principal.organization_id)
        .with_for_update()
    )
    if policy.version != expected_version:
        raise ResourceVersionConflict
    lock_all_candidate_retention_facts(db, principal.organization_id)
    shortening = any(values[name] < getattr(policy, name) for name in values)
    affected, due = affected_candidate_ids(db, principal.organization_id, values["terminal_days"])
    if shortening:
        _verify_preview(impact_token, codec, principal, policy, values, affected, now)
    previous_version = policy.version
    for name, value in values.items():
        setattr(policy, name, value)
    policy.version += 1
    policy.updated_by = principal.user_id
    policy.updated_at = aware(now)
    recalculate_due_dates(db, principal.organization_id, due)
    append_audit(
        db,
        actor=principal,
        category="governance",
        event_type="retention_policy.updated",
        outcome="success",
        trace_id=trace_id,
        resource_type="retention_policy",
        resource_id=policy.id,
        metadata={
            "previous_version": previous_version,
            "new_version": policy.version,
            **values,
            "affected_candidate_count": len(affected),
            "shortened": shortening,
        },
    )
    db.flush()
    return policy_projection(db, policy)
