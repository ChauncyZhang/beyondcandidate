import uuid
from datetime import datetime

from sqlalchemy import JSON, CheckConstraint, DateTime, ForeignKeyConstraint, Index, Integer, String, Text, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from server.app.identity.models import Base
from server.app.recruiting.models import Record, now


class ApplicationTermination(Record, Base):
    __tablename__ = "application_terminations"

    application_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    candidate_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    job_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    actor_user_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    source_stage: Mapped[str] = mapped_column(String(32), nullable=False)
    reason_text: Mapped[str] = mapped_column(Text, nullable=False)
    terminated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    external_status: Mapped[str] = mapped_column(String(16), default="resolved", nullable=False)
    external_items: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    resolution_history: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    __table_args__ = (
        UniqueConstraint("organization_id", "id"),
        UniqueConstraint("organization_id", "application_id"),
        ForeignKeyConstraint(["organization_id", "application_id", "candidate_id", "job_id"], ["applications.organization_id", "applications.id", "applications.candidate_id", "applications.job_id"], ondelete="CASCADE"),
        ForeignKeyConstraint(["organization_id", "actor_user_id"], ["users.organization_id", "users.id"]),
        CheckConstraint("version >= 1", name="ck_application_terminations_version"),
        CheckConstraint("length(trim(reason_text)) between 1 and 2000", name="ck_application_terminations_reason"),
        CheckConstraint("external_status in ('pending','resolved')", name="ck_application_terminations_external_status"),
        Index("ix_application_terminations_pending", "organization_id", "external_status", "terminated_at"),
    )
