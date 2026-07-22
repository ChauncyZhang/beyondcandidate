from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import and_, func, select, text
from sqlalchemy.exc import DBAPIError

from server.app.governance.audit import append_audit
from server.app.governance.deletion_models import DeletionArtifact, DeletionRequest, LegalHold
from server.app.governance.models import RetentionPolicy
from server.app.interviews.models import Interview, InterviewFeedback, InterviewFeedbackRevision
from server.app.queue.models import BackgroundJob
from server.app.queue.repository import QueueRepository
from server.app.recruiting.models import (
    Application,
    Candidate,
    CandidateContact,
    FileObject,
    IdempotencyRecord,
    Resume,
)
from server.app.recruiting.service import (
    IdempotencyConflict,
    RecruitingService,
    withdraw_candidate_applications_for_deletion,
)
from server.app.reports.models import ExportCandidateMembership, ExportRecord
from server.app.screening.models import ScreeningItem, ScreeningResult
from server.app.talent.models import TalentPoolMembership


class DeletionDomainError(ValueError):
    """A non-disclosing deletion-domain rejection."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class DatabaseRedactionResult:
    checksum: str
    facts: tuple[int, int, int, int, int, int, int, int, int]


_SAFE_REDACTION_ERRORS = frozenset(
    {
        "redaction_context_invalid",
        "redaction_state_invalid",
        "redaction_manifest_stale",
        "redaction_tombstone_invalid",
        "redaction_not_authorized",
    }
)


def execute_database_redaction(
    connection,
    *,
    organization_id: UUID,
    request_id: UUID,
    candidate_id: UUID,
) -> DatabaseRedactionResult:
    """Invoke the executor-only routine and expose only checksum/count facts."""

    try:
        row = connection.execute(
            text(
                "SELECT * FROM redact_candidate_data("
                ":organization_id, :request_id, :candidate_id)"
            ),
            {
                "organization_id": organization_id,
                "request_id": request_id,
                "candidate_id": candidate_id,
            },
        ).one()
    except DBAPIError as error:
        diagnostic = getattr(getattr(error, "orig", None), "diag", None)
        code = getattr(diagnostic, "message_primary", None)
        if code not in _SAFE_REDACTION_ERRORS:
            code = "redaction_failed"
        raise DeletionDomainError(code) from None
    checksum = row[0]
    facts = tuple(int(value) for value in row[1:])
    if (
        not isinstance(checksum, str)
        or len(checksum) != 64
        or len(facts) != 9
        or any(value < 0 for value in facts)
    ):
        raise DeletionDomainError("redaction_result_invalid")
    return DatabaseRedactionResult(checksum=checksum, facts=facts)


_ALLOWED_TRANSITIONS = {
    "requested": frozenset({"approved"}),
    "approved": frozenset({"executing"}),
    "executing": frozenset({"completed", "failed"}),
    "failed": frozenset({"approved"}),
    "completed": frozenset({"completed"}),
}

_IMPACT_COUNT_KEYS = (
    "contacts",
    "resumes",
    "applications",
    "screening_records",
    "interviews",
    "feedback_records",
    "talent_memberships",
    "resume_objects",
    "temporary_exports",
)
_MAX_IMPACT_COUNT = 2_147_483_647


@dataclass(frozen=True)
class ApprovalResult:
    request: DeletionRequest
    enqueued: bool


def aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def idempotency_fingerprint(body: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def load_idempotency_after_lock(
    db,
    *,
    organization_id,
    user_id,
    operation: str,
    key: str,
    body: dict[str, Any],
) -> IdempotencyRecord | None:
    if db.get_bind().dialect.name == "postgresql":
        db.execute(
            text("select pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
            {
                "lock_key": (
                    f"{organization_id}:{user_id}:{operation}:{key}"
                )
            },
        )
    record = db.scalar(
        select(IdempotencyRecord)
        .where(
            IdempotencyRecord.organization_id == organization_id,
            IdempotencyRecord.user_id == user_id,
            IdempotencyRecord.operation == operation,
            IdempotencyRecord.idempotency_key == key,
        )
        .with_for_update()
    )
    if record is not None and aware(record.expires_at) <= datetime.now(timezone.utc):
        db.delete(record)
        db.flush()
        record = None
    if record is not None and record.request_hash != idempotency_fingerprint(body):
        raise IdempotencyConflict
    return record


def store_idempotency_after_lock(
    db,
    *,
    organization_id,
    user_id,
    operation: str,
    key: str,
    body: dict[str, Any],
    status_code: int,
    response_json: dict[str, Any],
) -> IdempotencyRecord:
    record = IdempotencyRecord(
        organization_id=organization_id,
        user_id=user_id,
        operation=operation,
        idempotency_key=key,
        request_hash=idempotency_fingerprint(body),
        status_code=status_code,
        response_json=response_json,
    )
    db.add(record)
    db.flush()
    return record


def lock_candidate(db, organization_id, candidate_id) -> Candidate | None:
    return db.scalar(
        select(Candidate)
        .where(
            Candidate.organization_id == organization_id,
            Candidate.id == candidate_id,
        )
        .with_for_update()
    )


def lock_candidate_deletion_requests(db, organization_id, candidate_id) -> list[DeletionRequest]:
    return list(
        db.scalars(
            select(DeletionRequest)
            .where(
                DeletionRequest.organization_id == organization_id,
                DeletionRequest.candidate_id == candidate_id,
            )
            .order_by(DeletionRequest.id)
            .with_for_update()
        )
    )


def lock_candidate_governance_rows(db, organization_id, candidate_id) -> None:
    lock_candidate_deletion_requests(db, organization_id, candidate_id)
    list(
        db.scalars(
            select(LegalHold)
            .where(
                LegalHold.organization_id == organization_id,
                LegalHold.candidate_id == candidate_id,
            )
            .order_by(LegalHold.id)
            .with_for_update()
        )
    )


def lock_deletion_request_context(
    db, organization_id, request_id
) -> tuple[Candidate, DeletionRequest] | None:
    candidate_id = db.scalar(
        select(DeletionRequest.candidate_id).where(
            DeletionRequest.organization_id == organization_id,
            DeletionRequest.id == request_id,
        )
    )
    if candidate_id is None:
        return None
    candidate = lock_candidate(db, organization_id, candidate_id)
    if candidate is None:
        return None
    request = db.scalar(
        select(DeletionRequest)
        .where(
            DeletionRequest.organization_id == organization_id,
            DeletionRequest.id == request_id,
        )
        .with_for_update()
    )
    return (candidate, request) if request is not None else None


def lock_approval_legal_holds(db, row: DeletionRequest) -> None:
    list(
        db.scalars(
            select(LegalHold)
            .where(
                LegalHold.organization_id == row.organization_id,
                LegalHold.candidate_id == row.candidate_id,
                LegalHold.released_at.is_(None),
            )
            .order_by(LegalHold.id)
            .with_for_update()
        )
    )


def lock_legal_hold_context(db, organization_id, hold_id) -> tuple[Candidate, LegalHold] | None:
    candidate_id = db.scalar(
        select(LegalHold.candidate_id).where(
            LegalHold.organization_id == organization_id,
            LegalHold.id == hold_id,
        )
    )
    if candidate_id is None:
        return None
    candidate = lock_candidate(db, organization_id, candidate_id)
    if candidate is None:
        return None
    hold = db.scalar(
        select(LegalHold)
        .where(
            LegalHold.organization_id == organization_id,
            LegalHold.id == hold_id,
        )
        .with_for_update()
    )
    return (candidate, hold) if hold is not None else None


def _sorted_ids(db, statement) -> list[str]:
    return sorted((str(value) for value in db.scalars(statement)), key=str)


def build_private_manifest(
    db, candidate: Candidate, *, now: datetime
) -> tuple[dict[str, Any], RetentionPolicy]:
    organization_id = candidate.organization_id
    candidate_id = candidate.id
    contacts = _sorted_ids(
        db,
        select(CandidateContact.id).where(
            CandidateContact.organization_id == organization_id,
            CandidateContact.candidate_id == candidate_id,
        ),
    )
    resumes = list(
        db.execute(
            select(Resume.id, FileObject.id, FileObject.storage_key)
            .join(
                FileObject,
                and_(
                    FileObject.organization_id == Resume.organization_id,
                    FileObject.id == Resume.file_object_id,
                ),
            )
            .where(
                Resume.organization_id == organization_id,
                Resume.candidate_id == candidate_id,
            )
        )
    )
    applications = _sorted_ids(
        db,
        select(Application.id).where(
            Application.organization_id == organization_id,
            Application.candidate_id == candidate_id,
        ),
    )
    screening_items = _sorted_ids(
        db,
        select(ScreeningItem.id).where(
            ScreeningItem.organization_id == organization_id,
            ScreeningItem.candidate_id == candidate_id,
        ),
    )
    screening_results = _sorted_ids(
        db,
        select(ScreeningResult.id)
        .join(
            ScreeningItem,
            and_(
                ScreeningItem.organization_id == ScreeningResult.organization_id,
                ScreeningItem.id == ScreeningResult.item_id,
            ),
        )
        .where(
            ScreeningItem.organization_id == organization_id,
            ScreeningItem.candidate_id == candidate_id,
        ),
    )
    interviews = _sorted_ids(
        db,
        select(Interview.id)
        .join(
            Application,
            and_(
                Application.organization_id == Interview.organization_id,
                Application.id == Interview.application_id,
            ),
        )
        .where(
            Application.organization_id == organization_id,
            Application.candidate_id == candidate_id,
        ),
    )
    feedback = _sorted_ids(
        db,
        select(InterviewFeedback.id)
        .join(
            Interview,
            and_(
                Interview.organization_id == InterviewFeedback.organization_id,
                Interview.id == InterviewFeedback.interview_id,
            ),
        )
        .join(
            Application,
            and_(
                Application.organization_id == Interview.organization_id,
                Application.id == Interview.application_id,
            ),
        )
        .where(
            Application.organization_id == organization_id,
            Application.candidate_id == candidate_id,
        ),
    )
    feedback_revisions = _sorted_ids(
        db,
        select(InterviewFeedbackRevision.id)
        .join(
            InterviewFeedback,
            and_(
                InterviewFeedback.organization_id == InterviewFeedbackRevision.organization_id,
                InterviewFeedback.id == InterviewFeedbackRevision.feedback_id,
            ),
        )
        .join(
            Interview,
            and_(
                Interview.organization_id == InterviewFeedback.organization_id,
                Interview.id == InterviewFeedback.interview_id,
            ),
        )
        .join(
            Application,
            and_(
                Application.organization_id == Interview.organization_id,
                Application.id == Interview.application_id,
            ),
        )
        .where(
            Application.organization_id == organization_id,
            Application.candidate_id == candidate_id,
        ),
    )
    memberships = _sorted_ids(
        db,
        select(TalentPoolMembership.id).where(
            TalentPoolMembership.organization_id == organization_id,
            TalentPoolMembership.candidate_id == candidate_id,
        ),
    )
    resume_objects = sorted(
        (
            {"row_id": str(file_id), "storage_key": storage_key}
            for _, file_id, storage_key in resumes
        ),
        key=lambda item: (item["storage_key"], item["row_id"]),
    )
    temporary_exports = sorted(
        (
            {"row_id": str(row_id), "storage_key": storage_key}
            for row_id, storage_key in db.execute(
                select(ExportRecord.id, ExportRecord.object_key).where(
                    ExportRecord.organization_id == organization_id,
                    ExportRecord.object_key.is_not(None),
                    ExportRecord.id.in_(
                        select(ExportCandidateMembership.export_id).where(
                            ExportCandidateMembership.organization_id == organization_id,
                            ExportCandidateMembership.candidate_id == candidate_id,
                        )
                    ),
                )
            )
        ),
        key=lambda item: (item["storage_key"], item["row_id"]),
    )
    policy = db.scalar(
        select(RetentionPolicy).where(RetentionPolicy.organization_id == organization_id)
    )
    if policy is None:
        raise DeletionDomainError("retention_policy_unavailable")
    manifest = {
        "schema_version": 1,
        "candidate_id": str(candidate_id),
        "candidate_version": candidate.version,
        "policy_version": policy.version,
        "backup_window_ends_at": _utc_isoformat(
            aware(now) + timedelta(days=policy.backup_window_days)
        ),
        "row_ids": {
            "contacts": contacts,
            "resumes": sorted(str(row_id) for row_id, _, _ in resumes),
            "applications": applications,
            "screening_items": screening_items,
            "screening_results": screening_results,
            "interviews": interviews,
            "feedback": feedback,
            "feedback_revisions": feedback_revisions,
            "talent_memberships": memberships,
        },
        "objects": {
            "resume_objects": resume_objects,
            "temporary_exports": temporary_exports,
        },
        "counts": {
            "contacts": len(contacts),
            "resumes": len(resumes),
            "applications": len(applications),
            "screening_records": len(screening_items) + len(screening_results),
            "interviews": len(interviews),
            "feedback_records": len(feedback) + len(feedback_revisions),
            "talent_memberships": len(memberships),
            "resume_objects": len(resume_objects),
            "temporary_exports": len(temporary_exports),
        },
    }
    return manifest, policy


def safe_request_projection(
    row: DeletionRequest, *, active_application_count: int | None = None
) -> dict[str, Any]:
    backup_window_ends_at = datetime.fromisoformat(
        row.impact_manifest["backup_window_ends_at"].replace("Z", "+00:00")
    )
    return {
        "id": row.id,
        "status": row.status,
        "version": row.version,
        "reason_code": row.reason_code,
        "requested_at": aware(row.requested_at),
        "approved_at": aware(row.approved_at) if row.approved_at else None,
        "safe_error_code": row.safe_error_code,
        "active_application_count": active_application_count,
        "impact": impact_manifest_projection(
            row.impact_manifest,
            request_id=row.id,
            candidate_version=row.candidate_version,
            policy_version=row.policy_version,
            backup_window_ends_at=backup_window_ends_at,
        ),
    }


def safe_hold_projection(row: LegalHold, *, include_reason: bool) -> dict[str, Any]:
    return {
        "id": row.id,
        "status": "active" if row.released_at is None else "released",
        "reason": row.reason if include_reason else None,
        "placed_at": aware(row.placed_at),
        "released_at": aware(row.released_at) if row.released_at else None,
        "version": row.version,
    }


def _clear_unstarted_artifacts_for_refresh(db, row: DeletionRequest) -> None:
    artifacts = list(
        db.scalars(
            select(DeletionArtifact)
            .where(
                DeletionArtifact.organization_id == row.organization_id,
                DeletionArtifact.request_id == row.id,
            )
            .with_for_update()
        )
    )
    if any(artifact.status != "pending" or artifact.attempts != 0 for artifact in artifacts):
        raise DeletionDomainError("artifact_checkpoint_conflict")
    for artifact in artifacts:
        db.delete(artifact)
    if artifacts:
        db.flush()


def create_deletion_request_locked(
    db,
    *,
    candidate: Candidate,
    principal,
    reason_code: str,
    now: datetime,
    trace_id: str | None,
) -> DeletionRequest:
    existing = db.scalar(
        select(DeletionRequest)
        .where(
            DeletionRequest.organization_id == candidate.organization_id,
            DeletionRequest.candidate_id == candidate.id,
            DeletionRequest.status != "completed",
        )
        .with_for_update()
    )
    if existing is not None:
        raise DeletionDomainError("deletion_request_open")
    completed = db.scalar(
        select(func.count())
        .select_from(DeletionRequest)
        .where(
            DeletionRequest.organization_id == candidate.organization_id,
            DeletionRequest.candidate_id == candidate.id,
            DeletionRequest.status == "completed",
        )
    )
    validate_new_request(has_completed_request=bool(completed))
    manifest, policy = build_private_manifest(db, candidate, now=now)
    row = DeletionRequest(
        organization_id=candidate.organization_id,
        candidate_id=candidate.id,
        reason_code=reason_code,
        requested_by=principal.user_id,
        requested_at=aware(now),
        impact_manifest=manifest,
        manifest_hash=canonical_manifest_hash(manifest),
        policy_version=policy.version,
        candidate_version=candidate.version,
    )
    db.add(row)
    db.flush()
    append_audit(
        db,
        actor=principal,
        category="governance",
        event_type="governance.deletion_requested",
        outcome="success",
        trace_id=trace_id,
        resource_type="deletion_request",
        resource_id=row.id,
        metadata={"request_version": row.version},
    )
    return row


def active_application_count(db, row: DeletionRequest) -> int:
    return int(
        db.scalar(
            select(func.count())
            .select_from(Application)
            .where(
                Application.organization_id == row.organization_id,
                Application.candidate_id == row.candidate_id,
                Application.stage.not_in(RecruitingService.TERMINAL),
            )
        )
        or 0
    )


def _active_hold(db, row: DeletionRequest) -> LegalHold | None:
    return db.scalar(
        select(LegalHold)
        .where(
            LegalHold.organization_id == row.organization_id,
            LegalHold.candidate_id == row.candidate_id,
            LegalHold.released_at.is_(None),
        )
        .with_for_update()
    )


def _replace_manifest(db, row: DeletionRequest, candidate: Candidate, now: datetime) -> None:
    manifest, policy = build_private_manifest(db, candidate, now=now)
    row.impact_manifest = manifest
    row.manifest_hash = canonical_manifest_hash(manifest)
    row.candidate_version = candidate.version
    row.policy_version = policy.version


def approve_deletion_request_locked(
    db,
    *,
    candidate: Candidate,
    row: DeletionRequest,
    principal,
    expected_version: int,
    terminate_active_applications: bool = False,
    now: datetime,
    trace_id: str | None,
) -> ApprovalResult:
    if row.version != expected_version:
        raise DeletionDomainError("resource_version_conflict")
    if row.status not in {"requested", "failed"}:
        raise DeletionDomainError("invalid_deletion_state_transition")
    active_applications = active_application_count(db, row)
    hold = _active_hold(db, row)
    current_manifest, _ = build_private_manifest(
        db, candidate, now=aware(row.requested_at)
    )
    current_hash = canonical_manifest_hash(current_manifest)
    if row.status == "requested" and (
        candidate.version != row.candidate_version
        or not hmac.compare_digest(row.manifest_hash, current_hash)
    ):
        _clear_unstarted_artifacts_for_refresh(db, row)
        row.impact_manifest = current_manifest
        row.manifest_hash = current_hash
        row.candidate_version = candidate.version
        row.policy_version = current_manifest["policy_version"]
        row.version += 1
        row.safe_error_code = "stale_manifest"
        append_audit(
            db,
            actor=principal,
            category="governance",
            event_type="governance.deletion_approved",
            outcome="failure",
            trace_id=trace_id,
            resource_type="deletion_request",
            resource_id=row.id,
            metadata={"request_version": row.version, "safe_error_code": "stale_manifest"},
        )
        db.flush()
        return ApprovalResult(row, False)
    retrying_failed = row.status == "failed"
    validate_approval(
        requester_id=row.requested_by,
        approver_id=principal.user_id,
        has_active_application=(
            active_applications > 0 and not terminate_active_applications
        ),
        has_active_hold=hold is not None,
        candidate_version=candidate.version,
        expected_candidate_version=(
            candidate.version if retrying_failed else row.candidate_version
        ),
        request_version=row.version,
        expected_request_version=expected_version,
        manifest_hash=current_hash if retrying_failed else row.manifest_hash,
        expected_manifest_hash=current_hash,
    )
    if retrying_failed:
        _clear_unstarted_artifacts_for_refresh(db, row)
        row.impact_manifest = current_manifest
        row.manifest_hash = current_hash
        row.candidate_version = candidate.version
        row.policy_version = current_manifest["policy_version"]
    terminated_active_applications = 0
    if terminate_active_applications and active_applications > 0:
        terminated_active_applications = withdraw_candidate_applications_for_deletion(
            db,
            row.organization_id,
            row.candidate_id,
            actor_user_id=principal.user_id,
            trace_id=trace_id,
        )
    advance_deletion_status(row.status, "approved")
    row.status = "approved"
    row.approved_by = principal.user_id
    row.approved_at = aware(now)
    row.safe_error_code = None
    row.version += 1
    QueueRepository(db).enqueue(
        row.organization_id,
        "governance.delete_candidate",
        {
            "organization_id": str(row.organization_id),
            "deletion_request_id": str(row.id),
            "request_version": row.version,
        },
        dedupe_key=f"candidate-delete:{row.id}:{row.version}",
        trace_id=trace_id,
        max_attempts=3,
    )
    append_audit(
        db,
        actor=principal,
        category="governance",
        event_type="governance.deletion_approved",
        outcome="success",
        trace_id=trace_id,
        resource_type="deletion_request",
        resource_id=row.id,
        metadata={
            "request_version": row.version,
            "terminated_active_application_count": terminated_active_applications,
        },
    )
    db.flush()
    return ApprovalResult(row, True)


def place_legal_hold_locked(
    db,
    *,
    candidate: Candidate,
    principal,
    reason: str,
    now: datetime,
    trace_id: str | None,
) -> LegalHold:
    request = db.scalar(
        select(DeletionRequest)
        .where(
            DeletionRequest.organization_id == candidate.organization_id,
            DeletionRequest.candidate_id == candidate.id,
            DeletionRequest.status != "completed",
        )
        .with_for_update()
    )
    current_hold = db.scalar(
        select(LegalHold)
        .where(
            LegalHold.organization_id == candidate.organization_id,
            LegalHold.candidate_id == candidate.id,
            LegalHold.released_at.is_(None),
        )
        .with_for_update()
    )
    if current_hold is not None:
        raise DeletionDomainError("legal_hold_active")
    request_version = None
    if request is not None:
        new_status, safe_error = hold_placement_outcome(request.status)
        if new_status != request.status:
            request.status = new_status
            request.safe_error_code = safe_error
            request.version += 1
            request_version = request.version
            job = db.scalar(
                select(BackgroundJob)
                .where(
                    BackgroundJob.organization_id == request.organization_id,
                    BackgroundJob.type == "governance.delete_candidate",
                    BackgroundJob.dedupe_key
                    == f"candidate-delete:{request.id}:{request.version - 1}",
                    BackgroundJob.status.in_(("queued", "running")),
                )
                .with_for_update()
            )
            if job is not None:
                QueueRepository(db).cancel(request.organization_id, job.id)
    hold = LegalHold(
        organization_id=candidate.organization_id,
        candidate_id=candidate.id,
        reason=reason,
        placed_by=principal.user_id,
        placed_at=aware(now),
    )
    db.add(hold)
    db.flush()
    metadata = {"request_version": request_version} if request_version is not None else {}
    append_audit(
        db,
        actor=principal,
        category="governance",
        event_type="governance.legal_hold_placed",
        outcome="success",
        trace_id=trace_id,
        resource_type="legal_hold",
        resource_id=hold.id,
        metadata=metadata,
    )
    return hold


def release_legal_hold_locked(
    db,
    *,
    hold: LegalHold,
    principal,
    reason: str,
    expected_version: int,
    now: datetime,
    trace_id: str | None,
) -> LegalHold:
    if hold.version != expected_version:
        raise DeletionDomainError("resource_version_conflict")
    if hold.released_at is not None:
        raise DeletionDomainError("legal_hold_already_released")
    hold.released_by = principal.user_id
    hold.released_at = aware(now)
    hold.released_reason = reason
    hold.version += 1
    append_audit(
        db,
        actor=principal,
        category="governance",
        event_type="governance.legal_hold_released",
        outcome="success",
        trace_id=trace_id,
        resource_type="legal_hold",
        resource_id=hold.id,
        metadata={"hold_version": hold.version},
    )
    db.flush()
    return hold


def append_failure_audit(
    db,
    *,
    principal,
    event_type: str,
    trace_id: str | None,
    safe_error_code: str,
    resource_type: str | None = None,
    resource_id=None,
) -> None:
    append_audit(
        db,
        actor=principal,
        category="governance",
        event_type=event_type,
        outcome="denied" if safe_error_code == "resource_not_found" else "failure",
        trace_id=trace_id,
        resource_type=resource_type,
        resource_id=resource_id,
        metadata={"safe_error_code": safe_error_code},
    )


def advance_deletion_status(current: str, target: str) -> str:
    if target not in _ALLOWED_TRANSITIONS.get(current, frozenset()):
        raise DeletionDomainError("invalid_deletion_state_transition")
    return target


def validate_new_request(*, has_completed_request: bool) -> None:
    if has_completed_request:
        raise DeletionDomainError("candidate_deletion_completed")


def validate_approval(
    *,
    requester_id: object,
    approver_id: object,
    has_active_application: bool,
    has_active_hold: bool,
    candidate_version: int,
    expected_candidate_version: int,
    request_version: int,
    expected_request_version: int,
    manifest_hash: str,
    expected_manifest_hash: str,
) -> None:
    if requester_id == approver_id:
        raise DeletionDomainError("self_approval_forbidden")
    if has_active_application:
        raise DeletionDomainError("active_application_exists")
    if has_active_hold:
        raise DeletionDomainError("legal_hold_active")
    if candidate_version != expected_candidate_version:
        raise DeletionDomainError("stale_candidate_version")
    if request_version != expected_request_version:
        raise DeletionDomainError("stale_request_version")
    if not hmac.compare_digest(manifest_hash, expected_manifest_hash):
        raise DeletionDomainError("stale_manifest_hash")


def hold_placement_outcome(status: str) -> tuple[str, str | None]:
    if status == "requested":
        return status, None
    if status == "approved":
        return "failed", "legal_hold_active"
    if status == "executing":
        raise DeletionDomainError("deletion_already_executing")
    raise DeletionDomainError("invalid_deletion_state_transition")


def _utc_isoformat(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def impact_manifest_projection(
    private_manifest: dict[str, Any],
    *,
    request_id: UUID,
    candidate_version: int,
    policy_version: int,
    backup_window_ends_at: datetime,
) -> dict[str, Any]:
    private_counts = private_manifest.get("counts")
    if not isinstance(private_counts, dict):
        private_counts = {}
    counts: dict[str, int] = {}
    for key in _IMPACT_COUNT_KEYS:
        value = private_counts.get(key, 0)
        if type(value) is not int or not 0 <= value <= _MAX_IMPACT_COUNT:
            raise DeletionDomainError("invalid_impact_manifest")
        counts[key] = value
    return {
        "schema_version": 1,
        "candidate_ref": str(request_id),
        "candidate_version": candidate_version,
        "policy_version": policy_version,
        "counts": counts,
        "backup_window_ends_at": _utc_isoformat(backup_window_ends_at),
    }


def canonical_manifest_hash(private_manifest: dict[str, Any]) -> str:
    try:
        canonical = json.dumps(
            private_manifest,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError):
        raise DeletionDomainError("invalid_impact_manifest") from None
    return hashlib.sha256(canonical).hexdigest()


def validate_recovery_generation(generation: object) -> int:
    if isinstance(generation, bool) or not isinstance(generation, int) or generation < 0:
        raise DeletionDomainError("invalid_recovery_generation")
    return generation
