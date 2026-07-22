from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKeyConstraint, Index, Integer, LargeBinary, String, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from server.app.identity.models import Base, utcnow


class FeishuOrganizationConfig(Base):
    __tablename__ = "feishu_organization_configs"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, unique=True)
    app_id: Mapped[str] = mapped_column(String(128))
    encrypted_app_secret: Mapped[bytes | None] = mapped_column(LargeBinary)
    redirect_uri: Mapped[str] = mapped_column(String(2048))
    calendar_id: Mapped[str] = mapped_column(String(512), default="primary")
    encrypted_verification_token: Mapped[bytes | None] = mapped_column(LargeBinary)
    encrypted_encrypt_key: Mapped[bytes | None] = mapped_column(LargeBinary)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    version: Mapped[int] = mapped_column(Integer, default=1)
    last_test_status: Mapped[str | None] = mapped_column(String(32))
    last_tested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_test_error_code: Mapped[str | None] = mapped_column(String(100))
    created_by: Mapped[uuid.UUID] = mapped_column(Uuid)
    updated_by: Mapped[uuid.UUID] = mapped_column(Uuid)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    __table_args__ = (
        ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        ForeignKeyConstraint(["organization_id", "created_by"], ["users.organization_id", "users.id"]),
        ForeignKeyConstraint(["organization_id", "updated_by"], ["users.organization_id", "users.id"]),
        CheckConstraint("version >= 1", name="ck_feishu_configs_version"),
        CheckConstraint("not enabled or encrypted_app_secret is not null", name="ck_feishu_configs_enabled_secret"),
        CheckConstraint("last_test_status is null or last_test_status in ('succeeded','failed')", name="ck_feishu_configs_test_status"),
    )


class FeishuOAuthState(Base):
    __tablename__ = "feishu_oauth_states"
    state_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    initiating_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    purpose: Mapped[str] = mapped_column(String(16))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (
        ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        ForeignKeyConstraint(["organization_id", "initiating_user_id"], ["users.organization_id", "users.id"], ondelete="CASCADE"),
        CheckConstraint("purpose in ('login','bind')", name="ck_feishu_oauth_states_purpose"),
        Index("ix_feishu_oauth_states_expiry", "expires_at"),
    )


class FeishuIdentityBinding(Base):
    __tablename__ = "feishu_identity_bindings"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    union_id: Mapped[str | None] = mapped_column(String(128))
    open_id: Mapped[str] = mapped_column(String(128))
    tenant_key: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    __table_args__ = (
        ForeignKeyConstraint(["organization_id", "user_id"], ["users.organization_id", "users.id"], ondelete="CASCADE"),
        UniqueConstraint("organization_id", "user_id"),
        UniqueConstraint("organization_id", "union_id"),
        UniqueConstraint("organization_id", "open_id"),
    )


class FeishuInterviewSync(Base):
    __tablename__ = "feishu_interview_syncs"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    interview_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    external_calendar_id: Mapped[str | None] = mapped_column(String(512))
    external_event_id: Mapped[str | None] = mapped_column(String(512))
    desired_action: Mapped[str] = mapped_column(String(16), default="create")
    sync_status: Mapped[str] = mapped_column(String(32), default="disabled")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    idempotency_key: Mapped[uuid.UUID] = mapped_column(Uuid, default=uuid.uuid4)
    last_error_code: Mapped[str | None] = mapped_column(String(100))
    provider_revision: Mapped[str | None] = mapped_column(String(255))
    provider_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_attempted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    __table_args__ = (
        ForeignKeyConstraint(["organization_id", "interview_id"], ["interviews.organization_id", "interviews.id"], ondelete="CASCADE"),
        UniqueConstraint("organization_id", "interview_id"),
        CheckConstraint("desired_action in ('create','update','cancel')", name="ck_feishu_sync_action"),
        CheckConstraint("sync_status in ('disabled','pending','syncing','synced','failed','pending_confirmation','cancelled')", name="ck_feishu_sync_status"),
        CheckConstraint("attempts >= 0", name="ck_feishu_sync_attempts"),
        Index("ix_feishu_sync_external_event", "organization_id", "external_event_id"),
    )
