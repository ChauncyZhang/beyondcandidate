from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Protocol
from uuid import UUID

from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from server.app.interviews.models import Interview, InterviewParticipant


class AvailabilityProvider(Protocol):
    def availability(
        self,
        *,
        db: Session,
        organization_id: UUID,
        participant_ids: list[UUID],
        starts_at: datetime,
        ends_at: datetime,
        buffer_minutes: int,
        exclude_interview_id: UUID | None,
    ) -> list[dict]: ...


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None and value.utcoffset() is not None else value.replace(tzinfo=timezone.utc)


class InternalInterviewAvailabilityProvider:
    """Returns opaque internal interview blocks without event or candidate details."""

    def availability(
        self,
        *,
        db: Session,
        organization_id: UUID,
        participant_ids: list[UUID],
        starts_at: datetime,
        ends_at: datetime,
        buffer_minutes: int,
        exclude_interview_id: UUID | None,
    ) -> list[dict]:
        statement = (
            select(InterviewParticipant.user_id, Interview.starts_at, Interview.ends_at)
            .join(
                Interview,
                and_(
                    Interview.organization_id == InterviewParticipant.organization_id,
                    Interview.id == InterviewParticipant.interview_id,
                ),
            )
            .where(
                Interview.organization_id == organization_id,
                InterviewParticipant.user_id.in_(participant_ids),
                Interview.status != "cancelled",
                Interview.starts_at < ends_at + timedelta(minutes=buffer_minutes),
                Interview.ends_at > starts_at - timedelta(minutes=buffer_minutes),
            )
            .order_by(InterviewParticipant.user_id, Interview.starts_at, Interview.id)
        )
        if exclude_interview_id is not None:
            statement = statement.where(Interview.id != exclude_interview_id)
        busy_by_participant: dict[UUID, list[dict]] = {participant_id: [] for participant_id in participant_ids}
        for participant_id, busy_start, busy_end in db.execute(statement):
            busy_by_participant[participant_id].append(
                {"starts_at": _aware(busy_start).isoformat(), "ends_at": _aware(busy_end).isoformat()}
            )
        return [
            {
                "participant_id": str(participant_id),
                "status": "confirmed",
                "busy": busy_by_participant[participant_id],
            }
            for participant_id in participant_ids
        ]


INTERNAL_AVAILABILITY_PROVIDER = InternalInterviewAvailabilityProvider()


def privacy_safe_availability(rows: list[dict], participant_ids: list[UUID]) -> list[dict]:
    by_participant = {str(row.get("participant_id")): row for row in rows if isinstance(row, dict)}
    safe_rows: list[dict] = []
    for participant_id in participant_ids:
        row = by_participant.get(str(participant_id))
        if not row or row.get("status") not in {"confirmed", "unknown"} or not isinstance(row.get("busy"), list):
            safe_rows.append({"participant_id": str(participant_id), "status": "unknown", "busy": []})
            continue
        busy: list[dict] = []
        valid = True
        for block in row["busy"]:
            if not isinstance(block, dict):
                valid = False
                break
            try:
                starts_at = datetime.fromisoformat(str(block["starts_at"]).replace("Z", "+00:00"))
                ends_at = datetime.fromisoformat(str(block["ends_at"]).replace("Z", "+00:00"))
                if starts_at.tzinfo is None or ends_at.tzinfo is None or ends_at <= starts_at:
                    raise ValueError
            except (KeyError, TypeError, ValueError):
                valid = False
                break
            busy.append({"starts_at": starts_at.isoformat(), "ends_at": ends_at.isoformat()})
        safe_rows.append({
            "participant_id": str(participant_id),
            "status": row["status"] if valid else "unknown",
            "busy": busy if valid else [],
        })
    return safe_rows
