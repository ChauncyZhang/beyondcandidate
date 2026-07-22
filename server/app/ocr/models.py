import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKeyConstraint, Integer, LargeBinary, String, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from server.app.identity.models import Base


def now() -> datetime:
    return datetime.now(timezone.utc)


class OcrProviderConfig(Base):
    __tablename__ = "ocr_provider_configs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    provider_id: Mapped[str] = mapped_column(String(64), nullable=False)
    base_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    encrypted_api_key: Mapped[bytes | None] = mapped_column(LargeBinary)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    last_test_status: Mapped[str | None] = mapped_column(String(20))
    last_test_error_code: Mapped[str | None] = mapped_column(String(64))
    last_test_latency_ms: Mapped[int | None] = mapped_column(Integer)
    last_tested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    updated_by: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=now, onupdate=now)

    __table_args__ = (
        UniqueConstraint("organization_id"),
        UniqueConstraint("organization_id", "id"),
        ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        ForeignKeyConstraint(["organization_id", "created_by"], ["users.organization_id", "users.id"]),
        ForeignKeyConstraint(["organization_id", "updated_by"], ["users.organization_id", "users.id"]),
        CheckConstraint("version >= 1", name="ck_ocr_provider_configs_version"),
        CheckConstraint("not enabled or encrypted_api_key is not null", name="ck_ocr_provider_configs_enabled_key"),
        CheckConstraint(
            "last_test_status is null or last_test_status in ('succeeded','failed')",
            name="ck_ocr_provider_configs_test_status",
        ),
        CheckConstraint(
            "last_test_latency_ms is null or last_test_latency_ms >= 0",
            name="ck_ocr_provider_configs_latency",
        ),
    )
