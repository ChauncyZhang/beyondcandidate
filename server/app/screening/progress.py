LLM_PENDING = {"queued", "running"}
LLM_TERMINAL = {"not_requested", "skipped", "succeeded", "failed"}


def summarize_progress(items, total_count):
    rule_failed = sum(item.status in {"failed", "cancelled"} for item in items)
    rule_succeeded = sum(item.status == "scored" and item.llm_status in LLM_TERMINAL for item in items)
    processed = rule_succeeded + rule_failed
    degraded = any(item.status == "scored" and item.llm_status == "failed" for item in items)
    if processed == total_count:
        status = "partial" if degraded or (rule_succeeded and rule_failed) else "failed" if not rule_succeeded and rule_failed else "completed"
    elif any(item.status == "scored" and item.llm_status in LLM_PENDING for item in items):
        status = "llm_scoring"
    elif any(item.status in {"parsed", "scoring", "scored"} for item in items):
        status = "rule_scoring"
    else:
        status = "parsing"
    return processed, rule_succeeded, rule_failed, status


def aggregate_run(db, run):
    from datetime import datetime,timezone
    from sqlalchemy import select
    from server.app.screening.models import ScreeningItem

    items = list(db.scalars(select(ScreeningItem).where(ScreeningItem.organization_id == run.organization_id, ScreeningItem.run_id == run.id)))
    processed, succeeded, failed, status = summarize_progress(items, run.total_count)
    previous=(run.processed_count,run.succeeded_count,run.failed_count,run.status)
    run.processed_count = processed
    run.succeeded_count = succeeded
    run.failed_count = failed
    run.status = status
    if status in {"completed","partial","failed","cancelled"}: run.finished_at=run.finished_at or datetime.now(timezone.utc)
    else: run.finished_at=None
    if previous!=(processed,succeeded,failed,status): run.version+=1
