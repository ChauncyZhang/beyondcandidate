from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

from sqlalchemy import select

from server.app.governance.audit import append_audit
from server.app.governance.retention import recalculate_candidate_retention
from server.app.identity.models import AuditLog, Job, User
from server.app.recruiting.models import (
    Application,
    ApplicationStageEvent,
    Candidate,
)
from server.app.recruiting.tasks import (
    ensure_review_task,
    normalize_llm_terminal_safe_error_code,
)
from server.app.screening.models import ScreeningItem, ScreeningRun
from server.app.talent.service import ensure_deferred_membership


class ScreeningRoutingConflict(Exception):
    pass


@dataclass(frozen=True)
class ScreeningOutcome:
    recommendation: str
    stage: Literal["review", "deferred"]


@dataclass(frozen=True)
class ScreeningRoutingResult:
    recommendation: str
    stage: str
    score: int | None
    routed: bool


def derive_screening_outcome(score: int) -> ScreeningOutcome:
    if not 0 <= score <= 100:
        raise ValueError("score_out_of_range")
    if score >= 85:
        return ScreeningOutcome("优先评审", "review")
    if score >= 60:
        return ScreeningOutcome("建议评审", "review")
    return ScreeningOutcome("暂缓", "deferred")


def _requested_outcome(score, ai_status, safe_error_code):
    if ai_status == "failed":
        if score is not None or not safe_error_code:
            raise ValueError("invalid_failed_screening_outcome")
        return ScreeningOutcome("AI评分不可用", "review")
    if ai_status != "succeeded" or score is None or safe_error_code is not None:
        raise ValueError("invalid_screening_outcome")
    return derive_screening_outcome(score)


def _latest_route_audit(db, application):
    return db.scalar(
        select(AuditLog)
        .where(
            AuditLog.organization_id == application.organization_id,
            AuditLog.event_type == "screening.terminal_routed",
            AuditLog.resource_type == "application",
            AuditLog.resource_id == application.id,
        )
        .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        .limit(1)
    )


def _previous_result(application, audit):
    metadata = audit.metadata_json if audit is not None else {}
    recommendation = metadata.get("recommendation")
    if not isinstance(recommendation, str):
        recommendation = "暂缓" if application.stage == "deferred" else "已流转"
    persisted_score = metadata.get("score")
    if not isinstance(persisted_score, int) or isinstance(persisted_score, bool):
        persisted_score = None
    return ScreeningRoutingResult(
        recommendation=recommendation,
        stage=application.stage,
        score=persisted_score,
        routed=False,
    )


def _is_fail_open_success_retry(audit, item, ai_status):
    if audit is None or ai_status != "succeeded":
        return False
    metadata = audit.metadata_json
    return (
        metadata.get("item_id") == str(item.id)
        and metadata.get("ai_status") == "failed"
        and metadata.get("to_stage") == "review"
    )


def route_llm_screening_terminal(
    db,
    *,
    organization_id,
    item_id,
    actor_user_id,
    score: int | None,
    ai_status: str,
    safe_error_code: str | None,
    trace_id: str,
    evaluation_id=None,
    invocation_id=None,
    transferable_capabilities=(),
):
    item_identity = db.execute(
        select(
            ScreeningItem.candidate_id,
            ScreeningItem.application_id,
            ScreeningItem.run_id,
        ).where(
            ScreeningItem.organization_id == organization_id,
            ScreeningItem.id == item_id,
        )
    ).one_or_none()
    if (
        item_identity is None
        or item_identity.candidate_id is None
        or item_identity.application_id is None
    ):
        raise ScreeningRoutingConflict("screening_item_unavailable")

    candidate = db.scalar(
        select(Candidate)
        .where(
            Candidate.organization_id == organization_id,
            Candidate.id == item_identity.candidate_id,
            Candidate.deleted_at.is_(None),
        )
        .with_for_update()
    )
    if candidate is None:
        raise ScreeningRoutingConflict("candidate_unavailable")
    item = db.scalar(
        select(ScreeningItem)
        .where(
            ScreeningItem.organization_id == organization_id,
            ScreeningItem.id == item_id,
            ScreeningItem.candidate_id == candidate.id,
            ScreeningItem.application_id == item_identity.application_id,
        )
        .with_for_update()
    )
    if item is None:
        raise ScreeningRoutingConflict("screening_item_changed")
    application = db.scalar(
        select(Application)
        .where(
            Application.organization_id == organization_id,
            Application.id == item.application_id,
            Application.candidate_id == candidate.id,
        )
        .with_for_update()
    )
    if application is None:
        raise ScreeningRoutingConflict("application_unavailable")
    previous_audit = _latest_route_audit(db, application)
    is_fail_open_retry = application.stage != "new" and _is_fail_open_success_retry(
        previous_audit, item, ai_status
    )
    if application.stage != "new" and not is_fail_open_retry:
        return _previous_result(application, previous_audit)

    outcome = _requested_outcome(score, ai_status, safe_error_code)
    if ai_status == "succeeded" and (evaluation_id is None or invocation_id is None):
        raise ValueError("screening_evidence_ids_required")
    if ai_status == "failed":
        safe_error_code = normalize_llm_terminal_safe_error_code(safe_error_code)

    actor = db.scalar(
        select(User).where(
            User.organization_id == organization_id,
            User.id == actor_user_id,
        )
    )
    run = db.scalar(
        select(ScreeningRun).where(
            ScreeningRun.organization_id == organization_id,
            ScreeningRun.id == item.run_id,
        )
    )
    job = db.scalar(
        select(Job).where(
            Job.organization_id == organization_id,
            Job.id == application.job_id,
        )
    )
    if actor is None or run is None or job is None:
        raise ScreeningRoutingConflict("routing_context_unavailable")

    if not is_fail_open_retry:
        application.stage = outcome.stage
        application.version += 1
        application.updated_at = datetime.now(timezone.utc)
    routed_stage = "review" if is_fail_open_retry else outcome.stage
    metadata = {
        "application_id": str(application.id),
        "item_id": str(item.id),
        "from_stage": "new",
        "to_stage": routed_stage,
        "ai_status": ai_status,
        "recommendation": outcome.recommendation,
    }
    if score is not None:
        metadata["score"] = score
    if evaluation_id is not None:
        metadata["evaluation_id"] = str(evaluation_id)
    if invocation_id is not None:
        metadata["invocation_id"] = str(invocation_id)
    if safe_error_code is not None:
        metadata["safe_error_code"] = safe_error_code
    if not is_fail_open_retry:
        db.add(
            ApplicationStageEvent(
                organization_id=organization_id,
                application_id=application.id,
                actor_user_id=actor_user_id,
                event_type="application.stage_changed",
                payload=dict(metadata),
            )
        )
    append_audit(
        db,
        actor=actor,
        category="recruiting",
        event_type="screening.terminal_routed",
        outcome="success",
        resource_type="application",
        resource_id=application.id,
        trace_id=trace_id,
        metadata=metadata,
    )
    if routed_stage == "review":
        ensure_review_task(
            db,
            application=application,
            job=job,
            ai_status=ai_status,
            safe_error_code=safe_error_code,
            create_if_missing=not is_fail_open_retry,
        )
    else:
        ensure_deferred_membership(
            db,
            application=application,
            candidate=candidate,
            job=job,
            run=run,
            score=score,
            transferable_capabilities=transferable_capabilities,
        )
    db.flush()
    if not is_fail_open_retry:
        recalculate_candidate_retention(db, organization_id, candidate.id)
    return ScreeningRoutingResult(
        recommendation=outcome.recommendation,
        stage=application.stage,
        score=score,
        routed=True,
    )
