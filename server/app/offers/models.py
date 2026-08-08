import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, CheckConstraint, DateTime, ForeignKeyConstraint, Index, Integer, String, Text, UniqueConstraint, Uuid, text
from sqlalchemy.orm import Mapped, mapped_column

from server.app.identity.models import Base


def now() -> datetime:
    return datetime.now(timezone.utc)


class OfferRecord:
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class OfferTemplate(OfferRecord, Base):
    __tablename__ = "offer_templates"
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    content: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="active", nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    __table_args__ = (
        UniqueConstraint("organization_id", "id"),
        UniqueConstraint("organization_id", "name"),
        CheckConstraint("status in ('active','inactive')", name="ck_offer_templates_status"),
        CheckConstraint("version >= 1", name="ck_offer_templates_version"),
    )


class OrganizationSpecialOfferApprover(OfferRecord, Base):
    __tablename__ = "organization_special_offer_approvers"
    approver_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    __table_args__ = (
        UniqueConstraint("organization_id", "id"),
        UniqueConstraint("organization_id", "position"),
        UniqueConstraint("organization_id", "approver_id"),
        CheckConstraint("position >= 1", name="ck_organization_special_offer_approvers_position"),
        ForeignKeyConstraint(["organization_id", "approver_id"], ["users.organization_id", "users.id"]),
    )


class Offer(OfferRecord, Base):
    __tablename__ = "offers"
    application_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    job_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    template_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    current_version_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="draft", nullable=False)
    is_special: Mapped[bool] = mapped_column(default=False, nullable=False)
    special_reason: Mapped[str | None] = mapped_column(Text)
    candidate_response_deadline: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    __table_args__ = (
        UniqueConstraint("organization_id", "id"),
        CheckConstraint("status in ('draft','pending_approval','changes_requested','ready_to_send','sent','withdrawn','expired')", name="ck_offers_status"),
        CheckConstraint("version >= 1", name="ck_offers_version"),
        CheckConstraint("(is_special and special_reason is not null and length(trim(special_reason)) > 0) or (not is_special and special_reason is null)", name="ck_offers_special_reason"),
        ForeignKeyConstraint(["organization_id", "application_id", "job_id"], ["applications.organization_id", "applications.id", "applications.job_id"]),
        ForeignKeyConstraint(["organization_id", "job_id"], ["jobs.organization_id", "jobs.id"]),
        ForeignKeyConstraint(["organization_id", "template_id"], ["offer_templates.organization_id", "offer_templates.id"]),
        ForeignKeyConstraint(["organization_id", "current_version_id", "id"], ["offer_versions.organization_id", "offer_versions.id", "offer_versions.offer_id"], deferrable=True, initially="DEFERRED"),
        Index("ix_offers_expiry", "organization_id", "status", "candidate_response_deadline"),
        Index(
            "uq_offers_active_application",
            "organization_id",
            "application_id",
            unique=True,
            postgresql_where=text("status in ('draft','pending_approval','changes_requested','ready_to_send','sent')"),
            sqlite_where=text("status in ('draft','pending_approval','changes_requested','ready_to_send','sent')"),
        ),
    )


class OfferVersion(OfferRecord, Base):
    __tablename__ = "offer_versions"
    offer_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[dict] = mapped_column(JSON, nullable=False)
    template_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    candidate_response_deadline: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    is_special: Mapped[bool] = mapped_column(nullable=False)
    special_reason: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    pdf_object_key: Mapped[str | None] = mapped_column(String(512))
    pdf_sha256: Mapped[str | None] = mapped_column(String(64))
    pdf_size_bytes: Mapped[int | None] = mapped_column(Integer)
    pdf_rendered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        UniqueConstraint("organization_id", "id"),
        UniqueConstraint("organization_id", "offer_id", "version_number"),
        UniqueConstraint("organization_id", "id", "offer_id"),
        UniqueConstraint("organization_id", "offer_id", "id", "version_number"),
        CheckConstraint(
            "(pdf_object_key is null and pdf_sha256 is null and pdf_size_bytes is null and pdf_rendered_at is null) or "
            "(pdf_object_key is not null and pdf_sha256 is not null and length(pdf_sha256) = 64 and "
            "pdf_sha256 = lower(pdf_sha256) and pdf_sha256 not like '%[^0-9a-f]%' and "
            "pdf_size_bytes > 0 and pdf_rendered_at is not null)",
            name="ck_offer_versions_pdf_receipt",
        ),
        ForeignKeyConstraint(["organization_id", "offer_id"], ["offers.organization_id", "offers.id"], ondelete="CASCADE"),
        ForeignKeyConstraint(["organization_id", "created_by"], ["users.organization_id", "users.id"]),
        ForeignKeyConstraint(["organization_id", "template_id"], ["offer_templates.organization_id", "offer_templates.id"]),
    )


class OfferApproval(OfferRecord, Base):
    __tablename__ = "offer_approvals"
    offer_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    offer_version_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    round_number: Mapped[int] = mapped_column(Integer, nullable=False)
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    assignee_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        UniqueConstraint("organization_id", "id"),
        UniqueConstraint("organization_id", "offer_id", "round_number", "sequence"),
        CheckConstraint("round_number >= 1 and version_number >= 1 and sequence >= 1", name="ck_offer_approvals_order"),
        CheckConstraint("status in ('waiting','pending','approved','rejected')", name="ck_offer_approvals_status"),
        CheckConstraint("status != 'rejected' or (reason is not null and length(trim(reason)) > 0)", name="ck_offer_approvals_rejection_reason"),
        ForeignKeyConstraint(["organization_id", "offer_id"], ["offers.organization_id", "offers.id"], ondelete="CASCADE"),
        ForeignKeyConstraint(["organization_id", "offer_id", "offer_version_id", "version_number"], ["offer_versions.organization_id", "offer_versions.offer_id", "offer_versions.id", "offer_versions.version_number"]),
        ForeignKeyConstraint(["organization_id", "assignee_id"], ["users.organization_id", "users.id"]),
    )


class OfferResponse(OfferRecord, Base):
    __tablename__ = "offer_responses"
    offer_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    responded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    __table_args__ = (
        UniqueConstraint("organization_id", "id"),
        UniqueConstraint("organization_id", "offer_id"),
        CheckConstraint("status in ('accepted','declined')", name="ck_offer_responses_status"),
        ForeignKeyConstraint(["organization_id", "offer_id"], ["offers.organization_id", "offers.id"], ondelete="CASCADE"),
    )


class OfferEvent(OfferRecord, Base):
    __tablename__ = "offer_events"
    offer_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    __table_args__ = (
        UniqueConstraint("organization_id", "id"),
        ForeignKeyConstraint(["organization_id", "offer_id"], ["offers.organization_id", "offers.id"], ondelete="CASCADE"),
        ForeignKeyConstraint(["organization_id", "actor_user_id"], ["users.organization_id", "users.id"]),
        Index("ix_offer_events_offer_created", "organization_id", "offer_id", "created_at"),
    )
