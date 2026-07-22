from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from types import SimpleNamespace
from uuid import UUID

from sqlalchemy import select

from server.app.governance.audit import append_audit
from server.app.governance.deletion_models import (
    DeletionRecoveryCheckpoint,
    DeletionRecoveryRun,
    DeletionRequest,
)
from server.app.queue.repository import QueueRepository
from server.app.queue.service import normalize_safe_code


def finalize_deletion_dead_letter(session, job, safe_code, now) -> None:
    payload = getattr(job, "payload", None)
    if not isinstance(payload, dict) or set(payload) != {
        "organization_id",
        "deletion_request_id",
        "request_version",
    }:
        return
    try:
        organization_id = UUID(payload["organization_id"])
        request_id = UUID(payload["deletion_request_id"])
        request_version = payload["request_version"]
        if (
            isinstance(request_version, bool)
            or not isinstance(request_version, int)
            or request_version < 1
            or UUID(str(job.organization_id)) != organization_id
        ):
            return
    except (AttributeError, TypeError, ValueError):
        return

    request = session.scalar(
        select(DeletionRequest)
        .where(
            DeletionRequest.organization_id == organization_id,
            DeletionRequest.id == request_id,
            DeletionRequest.version == request_version,
            DeletionRequest.status == "executing",
        )
        .with_for_update()
    )
    if request is None or request.approved_by is None:
        return
    normalized = normalize_safe_code(safe_code)
    request.status = "failed"
    request.safe_error_code = normalized
    request.version += 1
    append_audit(
        session,
        actor=SimpleNamespace(
            organization_id=request.organization_id,
            user_id=request.approved_by,
        ),
        category="governance",
        event_type="governance.deletion_failed",
        outcome="failure",
        trace_id=getattr(job, "trace_id", None),
        resource_type="deletion_request",
        resource_id=request.id,
        metadata={
            "request_version": request.version,
            "safe_error_code": normalized,
        },
    )


def governance_terminal_callbacks() -> dict[str, object]:
    return {
        "governance.delete_candidate": finalize_deletion_dead_letter,
        "governance.retention_sweep": finalize_retention_sweep_dead_letter,
        "governance.redelete_after_restore": finalize_recovery_dead_letter,
    }


def finalize_retention_sweep_dead_letter(session, job, safe_code, now) -> None:
    payload = getattr(job, "payload", None)
    if not isinstance(payload, dict) or set(payload) != {
        "organization_id",
        "scheduled_date",
    }:
        return
    try:
        organization_id = UUID(payload["organization_id"])
        scheduled_date = date.fromisoformat(payload["scheduled_date"])
        if UUID(str(job.organization_id)) != organization_id:
            return
    except (AttributeError, TypeError, ValueError):
        return
    next_date = scheduled_date + timedelta(days=1)
    QueueRepository(session).enqueue(
        organization_id,
        "governance.retention_sweep",
        {
            "organization_id": str(organization_id),
            "scheduled_date": str(next_date),
        },
        run_after=datetime.combine(next_date, time.min, tzinfo=timezone.utc),
        dedupe_key=f"retention-sweep:{organization_id}:{next_date}",
        max_attempts=3,
    )


def finalize_recovery_dead_letter(session, job, safe_code, now) -> None:
    payload = getattr(job, "payload", None)
    if not isinstance(payload, dict) or set(payload) != {
        "organization_id",
        "recovery_run_id",
        "checkpoint_id",
    }:
        return
    try:
        organization_id = UUID(payload["organization_id"])
        run_id = UUID(payload["recovery_run_id"])
        checkpoint_id = UUID(payload["checkpoint_id"])
        if UUID(str(job.organization_id)) != organization_id:
            return
    except (AttributeError, TypeError, ValueError):
        return
    checkpoint = session.scalar(
        select(DeletionRecoveryCheckpoint)
        .where(
            DeletionRecoveryCheckpoint.organization_id == organization_id,
            DeletionRecoveryCheckpoint.id == checkpoint_id,
            DeletionRecoveryCheckpoint.run_id == run_id,
            DeletionRecoveryCheckpoint.status != "completed",
        )
        .with_for_update()
    )
    run = session.scalar(
        select(DeletionRecoveryRun)
        .where(
            DeletionRecoveryRun.organization_id == organization_id,
            DeletionRecoveryRun.id == run_id,
        )
        .with_for_update()
    )
    if checkpoint is None or run is None:
        return
    normalized = normalize_safe_code(safe_code)
    checkpoint.status = "failed"
    checkpoint.safe_error_code = normalized
    run.status = "failed"
    run.safe_error_code = normalized
