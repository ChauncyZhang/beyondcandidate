from __future__ import annotations

import uuid
from datetime import date, datetime, timezone

from sqlalchemy import CheckConstraint, Date, DateTime, ForeignKeyConstraint, LargeBinary, String, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from server.app.identity.models import Base


def now() -> datetime:
    return datetime.now(timezone.utc)


class OnboardingRecord(Base):
    __tablename__ = "onboarding_records"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    offer_response_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    offer_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    application_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    candidate_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    job_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    department_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    job_title: Mapped[str] = mapped_column(String(200), nullable=False)
    department_name: Mapped[str] = mapped_column(String(200), nullable=False)
    expected_start_date: Mapped[date] = mapped_column(Date, nullable=False)
    pii_schema_version: Mapped[int] = mapped_column(default=1, nullable=False)
    pii_ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="ready", nullable=False)
    version: Mapped[int] = mapped_column(default=1, nullable=False)
    generation: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    started_by: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    safe_error_code: Mapped[str | None] = mapped_column(String(100))
    feishu_instance_code: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)

    __table_args__ = (
        UniqueConstraint("organization_id", "id"),
        UniqueConstraint("organization_id", "offer_response_id"),
        UniqueConstraint("organization_id", "application_id"),
        ForeignKeyConstraint(
            ["organization_id", "offer_response_id"],
            ["offer_responses.organization_id", "offer_responses.id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["organization_id", "offer_id"],
            ["offers.organization_id", "offers.id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["organization_id", "application_id", "candidate_id", "job_id"],
            ["applications.organization_id", "applications.id", "applications.candidate_id", "applications.job_id"],
        ),
        ForeignKeyConstraint(
            ["organization_id", "job_id"],
            ["jobs.organization_id", "jobs.id"],
        ),
        ForeignKeyConstraint(
            ["organization_id", "department_id"],
            ["departments.organization_id", "departments.id"],
        ),
        ForeignKeyConstraint(
            ["organization_id", "started_by"],
            ["users.organization_id", "users.id"],
        ),
        CheckConstraint("status in ('ready','submitting','submitted','failed')", name="ck_onboarding_records_status"),
        CheckConstraint("version >= 1 and pii_schema_version = 1", name="ck_onboarding_records_version"),
        CheckConstraint(
            "(status = 'ready' and generation is null and started_by is null and started_at is null and submitted_at is null) or "
            "(status in ('submitting','failed') and generation is not null and started_by is not null and started_at is not null and submitted_at is null) or "
            "(status = 'submitted' and generation is not null and started_by is not null and started_at is not null and submitted_at is not null and feishu_instance_code is not null)",
            name="ck_onboarding_records_lifecycle",
        ),
    )
