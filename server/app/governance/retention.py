from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy import and_, case, func, select

from server.app.governance.models import RetentionPolicy
from server.app.interviews.models import Interview, InterviewFeedback
from server.app.recruiting.models import Application, Candidate, CandidateEvent
from server.app.talent.models import TalentPoolMembership


TERMINAL_APPLICATION_STAGES = ("hired", "rejected", "withdrawn")


def aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _maximum(values: list[datetime | None]) -> datetime | None:
    present = [aware(value) for value in values if value is not None]
    return max(present) if present else None


def lock_candidate_retention_facts(db, organization_id: UUID, candidate_id: UUID):
    return db.scalar(
        select(Candidate)
        .where(
            Candidate.organization_id == organization_id,
            Candidate.id == candidate_id,
        )
        .with_for_update()
    )


def lock_all_candidate_retention_facts(db, organization_id: UUID) -> list[UUID]:
    return list(
        db.scalars(
            select(Candidate.id)
            .where(Candidate.organization_id == organization_id)
            .order_by(Candidate.id)
            .with_for_update()
        )
    )


def candidate_due_dates(
    db,
    organization_id: UUID,
    terminal_days: int,
    candidate_ids: set[UUID] | None = None,
) -> dict[UUID, datetime | None]:
    candidate_filter = [Candidate.organization_id == organization_id]
    fact_filter = []
    if candidate_ids is not None:
        candidate_filter.append(Candidate.id.in_(candidate_ids))
        fact_filter.append(Application.candidate_id.in_(candidate_ids))
    candidates = db.scalars(select(Candidate).where(*candidate_filter)).all()
    applications = db.execute(
        select(
            Application.candidate_id,
            func.sum(
                case(
                    (Application.stage.not_in(TERMINAL_APPLICATION_STAGES), 1),
                    else_=0,
                )
            ),
            func.max(Application.updated_at),
        )
        .where(Application.organization_id == organization_id, *fact_filter)
        .group_by(Application.candidate_id)
    ).all()
    app_facts = {
        candidate_id: (int(active_count or 0), latest)
        for candidate_id, active_count, latest in applications
    }
    event_conditions = [CandidateEvent.organization_id == organization_id]
    if candidate_ids is not None:
        event_conditions.append(CandidateEvent.candidate_id.in_(candidate_ids))
    event_facts = dict(
        db.execute(
            select(CandidateEvent.candidate_id, func.max(CandidateEvent.created_at))
            .where(*event_conditions)
            .group_by(CandidateEvent.candidate_id)
        ).all()
    )
    interview_facts = dict(
        db.execute(
            select(Application.candidate_id, func.max(Interview.updated_at))
            .join(
                Interview,
                and_(
                    Interview.organization_id == Application.organization_id,
                    Interview.application_id == Application.id,
                ),
            )
            .where(Application.organization_id == organization_id, *fact_filter)
            .group_by(Application.candidate_id)
        ).all()
    )
    feedback_facts = dict(
        db.execute(
            select(Application.candidate_id, func.max(InterviewFeedback.updated_at))
            .join(
                Interview,
                and_(
                    Interview.organization_id == Application.organization_id,
                    Interview.application_id == Application.id,
                ),
            )
            .join(
                InterviewFeedback,
                and_(
                    InterviewFeedback.organization_id == Interview.organization_id,
                    InterviewFeedback.interview_id == Interview.id,
                ),
            )
            .where(
                Application.organization_id == organization_id,
                *fact_filter,
                InterviewFeedback.status.in_(("submitted", "amended")),
            )
            .group_by(Application.candidate_id)
        ).all()
    )
    talent_conditions = [
        TalentPoolMembership.organization_id == organization_id,
        TalentPoolMembership.status == "active",
    ]
    if candidate_ids is not None:
        talent_conditions.append(TalentPoolMembership.candidate_id.in_(candidate_ids))
    talent_facts = dict(
        db.execute(
            select(
                TalentPoolMembership.candidate_id,
                func.max(TalentPoolMembership.retention_until),
            )
            .where(*talent_conditions)
            .group_by(TalentPoolMembership.candidate_id)
        ).all()
    )
    due: dict[UUID, datetime | None] = {}
    for candidate in candidates:
        active_count, application_updated = app_facts.get(candidate.id, (0, None))
        terminal_due = None
        if active_count == 0:
            latest = _maximum(
                [
                    candidate.updated_at,
                    application_updated,
                    event_facts.get(candidate.id),
                    interview_facts.get(candidate.id),
                    feedback_facts.get(candidate.id),
                ]
            )
            terminal_due = latest + timedelta(days=terminal_days) if latest else None
        talent_due = talent_facts.get(candidate.id)
        due[candidate.id] = None if active_count else _maximum([terminal_due, talent_due])
    return due


def recalculate_due_dates(
    db, organization_id: UUID, due: dict[UUID, datetime | None]
) -> None:
    if not due:
        return
    table = Candidate.__table__
    expression = case(due, value=table.c.id, else_=table.c.retention_due_at)
    db.execute(
        table.update()
        .where(
            table.c.organization_id == organization_id,
            table.c.id.in_(due.keys()),
        )
        .values(retention_due_at=expression, updated_at=table.c.updated_at)
    )


def recalculate_candidate_retention(
    db, organization_id: UUID, candidate_id: UUID
) -> None:
    db.flush()
    terminal_days = db.scalar(
        select(RetentionPolicy.terminal_days).where(
            RetentionPolicy.organization_id == organization_id
        )
    )
    if terminal_days is None:
        return
    due = candidate_due_dates(db, organization_id, terminal_days, {candidate_id})
    recalculate_due_dates(db, organization_id, due)
    db.flush()
