from datetime import datetime, timezone

from sqlalchemy import select

from server.app.recruiting.models import ApplicationReviewTask


LLM_TERMINAL_SAFE_ERROR_CODES = frozenset(
    {
        "internal_error",
        "llm_config_changed",
        "llm_config_deleted",
        "llm_config_disabled",
        "llm_handler_failed",
        "llm_input_invalid",
        "llm_job_not_allowed",
        "llm_job_payload_invalid",
        "llm_key_decryption_failed",
        "llm_prompt_invalid",
        "llm_provider_auth_failed",
        "llm_provider_quota_or_rate_limited",
        "llm_provider_response_invalid",
        "llm_provider_unavailable",
        "provider_address_forbidden",
        "provider_allowlist_invalid",
        "provider_auth_failed",
        "provider_dns_failed",
        "provider_dns_invalid",
        "provider_model_not_found",
        "provider_or_model_not_allowed",
        "provider_port_forbidden",
        "provider_quota_or_rate_limited",
        "provider_redirect_rejected",
        "provider_request_rejected",
        "provider_response_invalid",
        "provider_response_too_large",
        "provider_unavailable",
        "provider_url_forbidden",
        "screening_prompt_invalid",
        "screening_request_invalid",
    }
)


def normalize_llm_terminal_safe_error_code(value: object) -> str:
    return value if value in LLM_TERMINAL_SAFE_ERROR_CODES else "internal_error"


def ensure_review_task(
    db,
    *,
    application,
    job,
    ai_status: str,
    safe_error_code: str | None = None,
    create_if_missing: bool = True,
):
    if (
        job.organization_id != application.organization_id
        or job.id != application.job_id
    ):
        raise ValueError("review_task_context_mismatch")
    if ai_status not in {"succeeded", "failed"}:
        raise ValueError("review_task_ai_status_invalid")
    safe_error_code = (
        normalize_llm_terminal_safe_error_code(safe_error_code)
        if ai_status == "failed"
        else None
    )
    existing = db.scalar(
        select(ApplicationReviewTask)
        .where(
            ApplicationReviewTask.organization_id == application.organization_id,
            ApplicationReviewTask.application_id == application.id,
            ApplicationReviewTask.status == "open",
        )
        .with_for_update()
    )
    if existing is not None:
        if ai_status == "succeeded":
            existing.ai_status = ai_status
            existing.safe_error_code = None
        return existing
    if not create_if_missing:
        return None

    task = ApplicationReviewTask(
        organization_id=application.organization_id,
        application_id=application.id,
        assignee_id=job.hiring_owner_id or job.owner_id,
        status="open",
        ai_status=ai_status,
        safe_error_code=safe_error_code,
    )
    db.add(task)
    return task


def close_review_task(db, *, organization_id, application_id):
    task = db.scalar(
        select(ApplicationReviewTask)
        .where(
            ApplicationReviewTask.organization_id == organization_id,
            ApplicationReviewTask.application_id == application_id,
            ApplicationReviewTask.status == "open",
        )
        .with_for_update()
    )
    if task is None:
        return None
    task.status = "closed"
    task.closed_at = datetime.now(timezone.utc)
    return task
