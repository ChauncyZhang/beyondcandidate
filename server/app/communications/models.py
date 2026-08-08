import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKeyConstraint, Index, Integer, JSON, LargeBinary, String, Text, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from server.app.identity.models import Base


def now() -> datetime:
    return datetime.now(timezone.utc)


class EmailProviderConfig(Base):
    __tablename__ = "email_provider_configs"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    host: Mapped[str] = mapped_column(String(253), nullable=False)
    port: Mapped[int] = mapped_column(Integer, nullable=False)
    tls_mode: Mapped[str] = mapped_column(String(16), nullable=False)
    username: Mapped[str] = mapped_column(String(320), nullable=False)
    encrypted_password: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_by: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    updated_by: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=now, onupdate=now)
    __table_args__ = (
        UniqueConstraint("organization_id", "version"),
        UniqueConstraint("organization_id", "id", "version"),
        ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        ForeignKeyConstraint(["organization_id", "created_by"], ["users.organization_id", "users.id"]),
        ForeignKeyConstraint(["organization_id", "updated_by"], ["users.organization_id", "users.id"]),
        CheckConstraint("port between 1 and 65535", name="ck_email_provider_configs_port"),
        CheckConstraint("tls_mode in ('starttls','tls')", name="ck_email_provider_configs_tls_mode"),
        CheckConstraint("version >= 1", name="ck_email_provider_configs_version"),
    )


class EmailTemplate(Base):
    __tablename__ = "email_templates"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    key: Mapped[str] = mapped_column(String(100), nullable=False)
    subject_template: Mapped[str] = mapped_column(String(998), nullable=False)
    body_template: Mapped[str] = mapped_column(Text, nullable=False)
    variable_allowlist: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_by: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    updated_by: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=now, onupdate=now)
    __table_args__ = (
        UniqueConstraint("organization_id", "key"), UniqueConstraint("organization_id", "id"),
        ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        ForeignKeyConstraint(["organization_id", "created_by"], ["users.organization_id", "users.id"]),
        ForeignKeyConstraint(["organization_id", "updated_by"], ["users.organization_id", "users.id"]),
        CheckConstraint("version >= 1", name="ck_email_templates_version"),
    )


class EmailDelivery(Base):
    __tablename__ = "email_deliveries"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    provider_config_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    provider_config_version: Mapped[int] = mapped_column(Integer, nullable=False)
    template_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    template_version: Mapped[int | None] = mapped_column(Integer)
    recipient_ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    recipient_masked: Mapped[str] = mapped_column(String(320), nullable=False)
    sender_email: Mapped[str] = mapped_column(String(320), nullable=False)
    sender_name: Mapped[str] = mapped_column(String(200), nullable=False)
    reply_to_email: Mapped[str] = mapped_column(String(320), nullable=False)
    reply_to_name: Mapped[str] = mapped_column(String(200), nullable=False)
    rendered_subject: Mapped[str] = mapped_column(String(998), nullable=False)
    rendered_body: Mapped[str] = mapped_column(Text, nullable=False)
    resource_type: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    business_dedupe_key: Mapped[str] = mapped_column(String(255), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    parent_delivery_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="queued")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    safe_error_code: Mapped[str | None] = mapped_column(String(64))
    provider_receipt_id: Mapped[str | None] = mapped_column(String(255))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=now, onupdate=now)
    __table_args__ = (
        UniqueConstraint("organization_id", "id"), UniqueConstraint("organization_id", "business_dedupe_key"),
        ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        ForeignKeyConstraint(
            ["organization_id", "provider_config_id", "provider_config_version"],
            ["email_provider_configs.organization_id", "email_provider_configs.id", "email_provider_configs.version"],
        ),
        ForeignKeyConstraint(["organization_id", "template_id"], ["email_templates.organization_id", "email_templates.id"]),
        ForeignKeyConstraint(["organization_id", "parent_delivery_id"], ["email_deliveries.organization_id", "email_deliveries.id"]),
        ForeignKeyConstraint(["organization_id", "created_by"], ["users.organization_id", "users.id"]),
        CheckConstraint("status in ('queued','sent','failed')", name="ck_email_deliveries_status"),
        CheckConstraint("attempts between 0 and 3", name="ck_email_deliveries_attempts"),
        CheckConstraint("provider_config_version >= 1", name="ck_email_deliveries_provider_version"),
        CheckConstraint("version >= 1", name="ck_email_deliveries_version"),
        CheckConstraint(
            "(template_id is null and template_version is null) or (template_id is not null and template_version is not null)",
            name="ck_email_deliveries_template_version_pair",
        ),
        Index("ix_email_deliveries_history", "organization_id", "created_at"),
    )
