from __future__ import annotations

import enum
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import CheckConstraint, JSON, DateTime, Enum, ForeignKey, ForeignKeyConstraint, Integer, String, UniqueConstraint, Uuid
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class UserStatus(str, enum.Enum):
    ACTIVE = "active"
    DISABLED = "disabled"
    INVITED = "invited"


class Timestamped:
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class Organization(Timestamped, Base):
    __tablename__ = "organizations"
    slug: Mapped[str] = mapped_column(String(100), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(32), default="active")
    retention_policy_id: Mapped[uuid.UUID] = mapped_column(Uuid, default=uuid.uuid4, nullable=False)


class Department(Timestamped, Base):
    __tablename__ = "departments"
    __table_args__ = (
        CheckConstraint("status IN ('active','inactive')", name="ck_departments_status"),
        UniqueConstraint(
            "organization_id", "parent_id", "name", postgresql_nulls_not_distinct=True
        ),
        UniqueConstraint("organization_id", "id"),
        ForeignKeyConstraint(
            ["organization_id", "parent_id"],
            ["departments.organization_id", "departments.id"],
        ),
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"))
    parent_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    name: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(16), default="active")


class WorkflowTemplate(Timestamped, Base):
    __tablename__ = "workflow_templates"
    __table_args__ = (
        CheckConstraint("status IN ('active','inactive')", name="ck_workflow_templates_status"),
        CheckConstraint("version >= 1", name="ck_workflow_templates_version"),
        UniqueConstraint("organization_id", "id"),
        UniqueConstraint("organization_id", "name"),
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE")
    )
    name: Mapped[str] = mapped_column(String(200))
    rounds: Mapped[list[str]] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(16), default="active")
    version: Mapped[int] = mapped_column(Integer, default=1)


class User(Timestamped, Base):
    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("organization_id", "normalized_email"),
        UniqueConstraint("organization_id", "id"),
        ForeignKeyConstraint(
            ["organization_id", "department_id"],
            ["departments.organization_id", "departments.id"],
        ),
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"))
    department_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    email: Mapped[str] = mapped_column(String(320))
    normalized_email: Mapped[str] = mapped_column(String(320))
    display_name: Mapped[str] = mapped_column(String(200))
    password_hash: Mapped[str] = mapped_column(String(512))
    status: Mapped[UserStatus] = mapped_column(Enum(UserStatus, native_enum=False), default=UserStatus.ACTIVE)
    authorization_version: Mapped[int] = mapped_column(Integer, default=1)
    failed_login_count: Mapped[int] = mapped_column(Integer, default=0)
    failed_login_window_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    organization: Mapped[Organization] = relationship()
    roles: Mapped[list[UserRole]] = relationship(back_populates="user", cascade="all, delete-orphan")


class UserRole(Timestamped, Base):
    __tablename__ = "user_roles"
    __table_args__ = (
        CheckConstraint(
            "role IN ('system_admin','recruiting_admin','recruiter','hiring_manager','interviewer')"
        ),
        UniqueConstraint("user_id", "role"),
    )
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    role: Mapped[str] = mapped_column(String(32))
    user: Mapped[User] = relationship(back_populates="roles")


class UserSession(Timestamped, Base):
    __tablename__ = "user_sessions"
    organization_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"))
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    csrf_token_hash: Mapped[str] = mapped_column(String(64))
    idle_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    absolute_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    authorization_version: Mapped[int] = mapped_column(Integer)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revocation_reason: Mapped[str | None] = mapped_column(String(64))
    user: Mapped[User] = relationship()
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "user_id"],
            ["users.organization_id", "users.id"],
            ondelete="CASCADE",
        ),
    )


class PasswordInvitation(Base):
    __tablename__ = "password_invitations"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    user: Mapped[User] = relationship()
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "user_id"],
            ["users.organization_id", "users.id"],
            ondelete="CASCADE",
        ),
    )


class Job(Timestamped, Base):
    __tablename__ = "jobs"
    organization_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"))
    title: Mapped[str] = mapped_column(String(200))
    department_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    headcount: Mapped[int] = mapped_column(Integer, default=1)
    priority: Mapped[str] = mapped_column(String(16), default="normal")
    hiring_owner_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    workflow_template_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    owner_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    status: Mapped[str] = mapped_column(String(32), default="draft")
    version: Mapped[int] = mapped_column(Integer, default=1)
    __table_args__ = (
        UniqueConstraint("organization_id", "id"),
        ForeignKeyConstraint(
            ["organization_id", "owner_id"],
            ["users.organization_id", "users.id"],
        ),
        ForeignKeyConstraint(
            ["organization_id", "department_id"],
            ["departments.organization_id", "departments.id"],
        ),
        ForeignKeyConstraint(
            ["organization_id", "hiring_owner_id"],
            ["users.organization_id", "users.id"],
        ),
        ForeignKeyConstraint(
            ["organization_id", "workflow_template_id"],
            ["workflow_templates.organization_id", "workflow_templates.id"],
        ),
    )


class JobCollaborator(Timestamped, Base):
    __tablename__ = "job_collaborators"
    __table_args__ = (
        CheckConstraint("access_role IN ('job_owner','job_recruiter','job_manager')"),
        UniqueConstraint("job_id", "user_id", "access_role"),
        ForeignKeyConstraint(
            ["organization_id", "job_id"],
            ["jobs.organization_id", "jobs.id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["organization_id", "user_id"],
            ["users.organization_id", "users.id"],
            ondelete="CASCADE",
        ),
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"))
    job_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    access_role: Mapped[str] = mapped_column(String(32))


class AuditLog(Base):
    __tablename__ = "audit_logs"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("organizations.id", ondelete="SET NULL"))
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    category: Mapped[str] = mapped_column(String(32), default="system", nullable=False)
    event_type: Mapped[str] = mapped_column(String(100))
    outcome: Mapped[str] = mapped_column(String(32))
    resource_type: Mapped[str | None] = mapped_column(String(64))
    resource_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    ip_hash: Mapped[str | None] = mapped_column(String(64))
    trace_id: Mapped[str | None] = mapped_column(String(64))
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
