import uuid
from datetime import datetime, timezone

from sqlalchemy import CheckConstraint, DateTime, ForeignKeyConstraint, Index, Integer, JSON, String, Text, UniqueConstraint, Uuid, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from server.app.identity.models import Base


JSON_DOCUMENT = JSON().with_variant(JSONB(), "postgresql")


def now() -> datetime:
    return datetime.now(timezone.utc)


class TalentPool(Base):
    __tablename__ = "talent_pools"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    name: Mapped[str] = mapped_column(String(200))
    purpose: Mapped[str] = mapped_column(Text)
    visibility: Mapped[str] = mapped_column(String(32), default="recruiting_team")
    owner_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    system_key: Mapped[str | None] = mapped_column(String(64))
    suitable_roles: Mapped[list] = mapped_column(JSON_DOCUMENT, default=list)
    retention_days: Mapped[int] = mapped_column(Integer, default=730)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)
    __table_args__ = (
        UniqueConstraint("organization_id", "id"),
        UniqueConstraint("organization_id", "name", name="uq_talent_pools_tenant_name"),
        CheckConstraint("visibility in ('private','recruiting_team','granted')", name="ck_talent_pools_visibility"),
        CheckConstraint("retention_days between 30 and 3650", name="ck_talent_pools_retention"),
        CheckConstraint("version >= 1", name="ck_talent_pools_version"),
        ForeignKeyConstraint(["organization_id", "owner_id"], ["users.organization_id", "users.id"]),
        Index("ix_talent_pools_tenant_updated", "organization_id", "updated_at", "id"),
        Index("uq_talent_pools_system_key", "organization_id", "system_key", unique=True, postgresql_where=text("system_key IS NOT NULL"), sqlite_where=text("system_key IS NOT NULL")),
    )


class TalentPoolGrant(Base):
    __tablename__ = "talent_pool_grants"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    pool_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    access_role: Mapped[str] = mapped_column(String(20), default="viewer")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    __table_args__ = (
        UniqueConstraint("organization_id", "pool_id", "user_id", name="uq_talent_pool_grant_user"),
        CheckConstraint("access_role in ('viewer','manager')", name="ck_talent_pool_grants_access"),
        ForeignKeyConstraint(["organization_id", "pool_id"], ["talent_pools.organization_id", "talent_pools.id"], ondelete="CASCADE"),
        ForeignKeyConstraint(["organization_id", "user_id"], ["users.organization_id", "users.id"], ondelete="CASCADE"),
        Index("ix_talent_pool_grants_tenant_user", "organization_id", "user_id", "pool_id"),
    )


class TalentPoolMembership(Base):
    __tablename__ = "talent_pool_memberships"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    pool_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    candidate_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    source_application_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    owner_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    suitable_roles: Mapped[list] = mapped_column(JSON_DOCUMENT, default=list)
    tags: Mapped[list] = mapped_column(JSON_DOCUMENT, default=list)
    reason: Mapped[str] = mapped_column(Text)
    next_contact_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retention_until: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(24), default="active")
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)
    __table_args__ = (
        UniqueConstraint("organization_id", "id"),
        UniqueConstraint("organization_id", "pool_id", "candidate_id", name="uq_talent_pool_membership_candidate"),
        CheckConstraint("status in ('active','do_not_contact','blocked')", name="ck_talent_pool_memberships_status"),
        CheckConstraint("version >= 1", name="ck_talent_pool_memberships_version"),
        ForeignKeyConstraint(["organization_id", "pool_id"], ["talent_pools.organization_id", "talent_pools.id"], ondelete="CASCADE"),
        ForeignKeyConstraint(["organization_id", "candidate_id"], ["candidates.organization_id", "candidates.id"]),
        ForeignKeyConstraint(
            ["organization_id", "source_application_id", "candidate_id"],
            ["applications.organization_id", "applications.id", "applications.candidate_id"],
        ),
        ForeignKeyConstraint(["organization_id", "owner_id"], ["users.organization_id", "users.id"]),
        Index("ix_talent_memberships_tenant_pool_updated", "organization_id", "pool_id", "updated_at", "id"),
        Index("ix_talent_memberships_tenant_followup", "organization_id", "next_contact_at"),
        Index("ix_talent_memberships_tenant_retention", "organization_id", "retention_until"),
    )
