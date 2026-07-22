import uuid

from sqlalchemy import select

from server.app.queue.service import normalize_safe_code
from server.app.reports.models import ExportRecord


def finalize_report_export_dead_letter(session, job, safe_code, now) -> None:
    try:
        export_id = uuid.UUID(str(job.payload["export_id"]))
    except (KeyError, TypeError, ValueError):
        return
    export = session.scalar(
        select(ExportRecord).where(
            ExportRecord.organization_id == job.organization_id,
            ExportRecord.id == export_id,
            ExportRecord.background_job_id == job.id,
        ).with_for_update()
    )
    if export is None or export.status == "succeeded":
        return
    export.status = "failed"
    export.safe_error_code = normalize_safe_code(safe_code)
    export.completed_at = export.completed_at or now
    export.updated_at = now


def report_terminal_callbacks() -> dict[str, object]:
    return {"reports.export": finalize_report_export_dead_letter}
