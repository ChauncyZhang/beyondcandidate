from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, ForeignKeyConstraint, Integer, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from server.app.identity.models import Base, Organization, utcnow


class RetentionPolicy(Base):
    __tablename__ = "retention_policies"
    __table_args__ = (
        UniqueConstraint("organization_id"),
        UniqueConstraint("organization_id", "id"),
        CheckConstraint("terminal_days BETWEEN 30 AND 3650"),
        CheckConstraint("talent_pool_days BETWEEN 30 AND 3650"),
        CheckConstraint("backup_window_days BETWEEN 30 AND 3650"),
        CheckConstraint("version >= 1"),
        CheckConstraint("version = 1 OR updated_by IS NOT NULL"),
        ForeignKeyConstraint(
            ["organization_id", "updated_by"],
            ["users.organization_id", "users.id"],
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    terminal_days: Mapped[int] = mapped_column(Integer, default=365)
    talent_pool_days: Mapped[int] = mapped_column(Integer, default=730)
    backup_window_days: Mapped[int] = mapped_column(Integer, default=90)
    version: Mapped[int] = mapped_column(Integer, default=1)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


Organization.__table__.append_constraint(
    ForeignKeyConstraint(
        [Organization.__table__.c.id, Organization.__table__.c.retention_policy_id],
        [RetentionPolicy.__table__.c.organization_id, RetentionPolicy.__table__.c.id],
        name="fk_organizations_retention_policy",
        ondelete="RESTRICT",
        deferrable=True,
        initially="DEFERRED",
    )
)
