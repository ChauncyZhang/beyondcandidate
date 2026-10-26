from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import JSON, CheckConstraint, DateTime, ForeignKeyConstraint, Integer, String, UniqueConstraint, Uuid, select
from sqlalchemy.orm import Mapped, mapped_column

from server.app.identity.models import Base, User, UserStatus, utcnow


class MemberOffboarding(Base):
    __tablename__ = "member_offboardings"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    successor_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    removed_by: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    removed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    unresolved: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    __table_args__ = (
        UniqueConstraint("organization_id", "user_id"),
        UniqueConstraint("organization_id", "id"),
        ForeignKeyConstraint(["organization_id", "user_id"], ["users.organization_id", "users.id"]),
        ForeignKeyConstraint(["organization_id", "successor_user_id"], ["users.organization_id", "users.id"]),
        ForeignKeyConstraint(["organization_id", "removed_by"], ["users.organization_id", "users.id"]),
        CheckConstraint("user_id != successor_user_id", name="ck_member_offboardings_distinct_successor"),
        CheckConstraint("status in ('pending','completed')", name="ck_member_offboardings_status"),
        CheckConstraint("version >= 1", name="ck_member_offboardings_version"),
    )


def offboarding_summary(record: MemberOffboarding) -> dict:
    return {
        "removed_at": record.removed_at.isoformat(),
        "successor_user_id": str(record.successor_user_id) if record.successor_user_id else None,
        "handover_status": record.status,
        "pending_count": len(record.unresolved),
    }


def handover_destination(db, organization_id, user_id) -> User | None:
    """Read the effective destination; domain eligibility is checked separately."""
    visited = set()
    while user_id is not None and user_id not in visited:
        visited.add(user_id)
        user = db.scalar(select(User).where(User.organization_id == organization_id, User.id == user_id))
        if user is None:
            return None
        if user.status == UserStatus.ACTIVE:
            return user
        if user.status != UserStatus.DISABLED:
            return None
        record = db.scalar(select(MemberOffboarding).where(
            MemberOffboarding.organization_id == organization_id, MemberOffboarding.user_id == user_id))
        user_id = record.successor_user_id if record else None
    return None
