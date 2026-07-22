import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKeyConstraint, Index, Integer, JSON, String, Text, UniqueConstraint, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from server.app.identity.models import Base


JSON_DOCUMENT = JSON().with_variant(JSONB(), "postgresql")


def now() -> datetime:
    return datetime.now(timezone.utc)


class Interview(Base):
    __tablename__ = "interviews"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    application_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    round_name: Mapped[str] = mapped_column(String(100))
    method: Mapped[str] = mapped_column(String(20))
    timezone: Mapped[str] = mapped_column(String(64))
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    location: Mapped[str | None] = mapped_column(String(1000))
    meeting_url: Mapped[str | None] = mapped_column(String(2000))
    status: Mapped[str] = mapped_column(String(24), default="draft")
    notification_status: Mapped[str] = mapped_column(String(24), default="not_sent")
    invitation_status: Mapped[str] = mapped_column(String(24), default="artifact_ready")
    owner_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    created_by: Mapped[uuid.UUID] = mapped_column(Uuid)
    version: Mapped[int] = mapped_column(Integer, default=1)
    calendar_sequence: Mapped[int] = mapped_column(Integer, default=0)
    calendar_organizer: Mapped[dict] = mapped_column(JSON_DOCUMENT, default=dict)
    calendar_attendees: Mapped[list] = mapped_column(JSON_DOCUMENT, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)
    __table_args__ = (
        UniqueConstraint("organization_id", "id"),
        CheckConstraint("method in ('video','onsite','phone')", name="ck_interviews_method"),
        CheckConstraint(
            "status in ('draft','scheduled','confirmed','completed','pending_feedback','feedback_completed','rescheduled','cancelled','no_show')",
            name="ck_interviews_status",
        ),
        CheckConstraint(
            "notification_status in ('not_sent','queued','sent','failed')",
            name="ck_interviews_notification_status",
        ),
        CheckConstraint("invitation_status in ('not_generated','artifact_ready','generation_failed')", name="ck_interviews_invitation_status"),
        CheckConstraint("ends_at > starts_at", name="ck_interviews_time_range"),
        CheckConstraint("version >= 1", name="ck_interviews_version"),
        CheckConstraint("calendar_sequence >= 0", name="ck_interviews_calendar_sequence"),
        ForeignKeyConstraint(["organization_id", "application_id"], ["applications.organization_id", "applications.id"]),
        ForeignKeyConstraint(["organization_id", "owner_id"], ["users.organization_id", "users.id"]),
        ForeignKeyConstraint(["organization_id", "created_by"], ["users.organization_id", "users.id"]),
        Index("ix_interviews_tenant_start_status", "organization_id", "starts_at", "status"),
        Index("ix_interviews_tenant_application_start", "organization_id", "application_id", "starts_at"),
    )


class InterviewParticipant(Base):
    __tablename__ = "interview_participants"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    interview_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    role: Mapped[str] = mapped_column(String(24), default="interviewer")
    required_feedback: Mapped[bool] = mapped_column(Boolean, default=True)
    attendance_status: Mapped[str] = mapped_column(String(24), default="invited")
    task_status: Mapped[str] = mapped_column(String(24), default="ready")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)
    __table_args__ = (
        UniqueConstraint("organization_id", "id"),
        UniqueConstraint("organization_id", "interview_id", "user_id", name="uq_interview_participant_user"),
        CheckConstraint("role in ('interviewer','observer')", name="ck_interview_participants_role"),
        CheckConstraint("attendance_status in ('invited','accepted','declined','attended','no_show')", name="ck_interview_participants_attendance"),
        CheckConstraint("task_status in ('ready','completed','cancelled')", name="ck_interview_participants_task_status"),
        ForeignKeyConstraint(["organization_id", "interview_id"], ["interviews.organization_id", "interviews.id"], ondelete="CASCADE"),
        ForeignKeyConstraint(["organization_id", "user_id"], ["users.organization_id", "users.id"]),
        Index("ix_interview_participants_tenant_user", "organization_id", "user_id", "interview_id"),
    )


class InterviewEvent(Base):
    __tablename__ = "interview_events"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    interview_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    actor_user_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    event_type: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSON_DOCUMENT, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    __table_args__ = (
        ForeignKeyConstraint(["organization_id", "interview_id"], ["interviews.organization_id", "interviews.id"], ondelete="CASCADE"),
        ForeignKeyConstraint(["organization_id", "actor_user_id"], ["users.organization_id", "users.id"]),
        Index("ix_interview_events_tenant_interview", "organization_id", "interview_id", "created_at"),
    )


class InterviewFeedback(Base):
    __tablename__ = "interview_feedbacks"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    interview_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    author_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="draft")
    ratings: Mapped[dict] = mapped_column(JSON_DOCUMENT, default=dict)
    strengths: Mapped[str | None] = mapped_column(Text)
    risks: Mapped[str | None] = mapped_column(Text)
    conclusion: Mapped[str | None] = mapped_column(String(32))
    notes: Mapped[str | None] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer, default=1)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)
    __table_args__ = (
        UniqueConstraint("organization_id", "id"),
        UniqueConstraint("organization_id", "interview_id", "author_id", name="uq_interview_feedback_author"),
        CheckConstraint("status in ('draft','submitted','amended')", name="ck_interview_feedbacks_status"),
        CheckConstraint("conclusion is null or conclusion in ('strong_recommend','recommend','hold','no_hire')", name="ck_interview_feedbacks_conclusion"),
        CheckConstraint("version >= 1", name="ck_interview_feedbacks_version"),
        CheckConstraint(
            "(status = 'draft' and submitted_at is null) or (status in ('submitted','amended') and submitted_at is not null)",
            name="ck_interview_feedbacks_submission",
        ),
        ForeignKeyConstraint(
            ["organization_id", "interview_id", "author_id"],
            ["interview_participants.organization_id", "interview_participants.interview_id", "interview_participants.user_id"],
        ),
        Index("ix_interview_feedbacks_tenant_interview_status", "organization_id", "interview_id", "status"),
    )


class InterviewFeedbackRevision(Base):
    __tablename__ = "interview_feedback_revisions"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    feedback_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    revision_number: Mapped[int] = mapped_column(Integer)
    previous_payload: Mapped[dict] = mapped_column(JSON_DOCUMENT)
    new_payload: Mapped[dict] = mapped_column(JSON_DOCUMENT)
    reason: Mapped[str] = mapped_column(String(1000))
    actor_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    __table_args__ = (
        UniqueConstraint("organization_id", "feedback_id", "revision_number", name="uq_interview_feedback_revision"),
        CheckConstraint("revision_number >= 1", name="ck_interview_feedback_revisions_number"),
        ForeignKeyConstraint(["organization_id", "feedback_id"], ["interview_feedbacks.organization_id", "interview_feedbacks.id"]),
        ForeignKeyConstraint(["organization_id", "actor_id"], ["users.organization_id", "users.id"]),
        Index("ix_interview_feedback_revisions_tenant_feedback", "organization_id", "feedback_id", "revision_number"),
    )
