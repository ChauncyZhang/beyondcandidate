import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKeyConstraint, Index, String, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from server.app.identity.models import Base


def now() -> datetime:
    return datetime.now(timezone.utc)


class NotificationRead(Base):
    __tablename__ = "notification_reads"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    application_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    notification_version: Mapped[str] = mapped_column(String(64), nullable=False)
    read_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "user_id",
            "application_id",
            name="uq_notification_reads_user_application",
        ),
        ForeignKeyConstraint(
            ["organization_id", "user_id"],
            ["users.organization_id", "users.id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["organization_id", "application_id"],
            ["applications.organization_id", "applications.id"],
            ondelete="CASCADE",
        ),
        Index("ix_notification_reads_user", "organization_id", "user_id"),
    )
