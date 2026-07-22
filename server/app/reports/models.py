import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, CheckConstraint, DateTime, ForeignKeyConstraint, Index, Integer, String, UniqueConstraint, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from server.app.identity.models import Base


JSON_DOCUMENT = JSON().with_variant(JSONB(), "postgresql")


def now() -> datetime:
    return datetime.now(timezone.utc)


class ExportRecord(Base):
    __tablename__ = "report_exports"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    requested_by: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    background_job_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    format: Mapped[str] = mapped_column(String(16), default="csv")
    status: Mapped[str] = mapped_column(String(20), default="queued")
    filters: Mapped[dict] = mapped_column(JSON_DOCUMENT, default=dict)
    object_key: Mapped[str | None] = mapped_column(String(512))
    generation_token: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    row_count: Mapped[int] = mapped_column(Integer, default=0)
    safe_error_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        UniqueConstraint("organization_id", "id"),
        UniqueConstraint("organization_id", "background_job_id", name="uq_report_exports_background_job"),
        CheckConstraint("format = 'csv'", name="ck_report_exports_format"),
        CheckConstraint("status in ('queued','running','succeeded','failed')", name="ck_report_exports_status"),
        CheckConstraint("row_count >= 0", name="ck_report_exports_row_count"),
        CheckConstraint(
            "(status = 'succeeded' and object_key is not null and completed_at is not null) "
            "or (status <> 'succeeded')",
            name="ck_report_exports_artifact",
        ),
        ForeignKeyConstraint(["organization_id", "requested_by"], ["users.organization_id", "users.id"]),
        ForeignKeyConstraint(
            ["organization_id", "background_job_id"],
            ["background_jobs.organization_id", "background_jobs.id"],
        ),
        Index("ix_report_exports_requester_created", "organization_id", "requested_by", "created_at"),
    )


class ExportCandidateMembership(Base):
    __tablename__ = "report_export_candidates"

    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    export_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    candidate_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "export_id"],
            ["report_exports.organization_id", "report_exports.id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["organization_id", "candidate_id"],
            ["candidates.organization_id", "candidates.id"],
        ),
        Index("ix_report_export_candidates_candidate", "organization_id", "candidate_id"),
    )


class ExportDownloadTicket(Base):
    __tablename__ = "report_export_download_tickets"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    export_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "export_id"],
            ["report_exports.organization_id", "report_exports.id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(["organization_id", "user_id"], ["users.organization_id", "users.id"]),
        Index("ix_report_export_tickets_expiry", "organization_id", "expires_at"),
    )
