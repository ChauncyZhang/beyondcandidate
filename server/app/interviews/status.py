"""Read-only interview status projection; persisted workflow state stays unchanged."""

from datetime import datetime, timezone

from sqlalchemy import and_, case

from server.app.interviews.models import Interview


SCHEDULED_STATUSES = ("scheduled", "rescheduled", "confirmed")


def effective_status(interview: Interview, *, now: datetime) -> str:
    ends_at = interview.ends_at
    # SQLite returns naive timestamps; scheduling stores them in UTC.
    if ends_at.tzinfo is None:
        ends_at = ends_at.replace(tzinfo=timezone.utc)
    if interview.status in SCHEDULED_STATUSES and ends_at <= now:
        return "pending_feedback"
    return interview.status


def effective_status_expression(*, now: datetime):
    return case(
        (and_(Interview.status.in_(SCHEDULED_STATUSES), Interview.ends_at <= now), "pending_feedback"),
        else_=Interview.status,
    )
