from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, mapped_column

from server.app.identity.models import Base, utcnow
from server.app.queue import models as queue_models  # noqa: F401


_PRIVATE_JSON = JSON().with_variant(postgresql.JSONB(), "postgresql")
_MANIFEST_HASH_REMAINDER = "manifest_hash"
for _hex_character in "0123456789abcdef":
    _MANIFEST_HASH_REMAINDER = (
        f"replace({_MANIFEST_HASH_REMAINDER}, '{_hex_character}', '')"
    )
_MANIFEST_HASH_CHECK = (
    "length(manifest_hash) = 64 AND "
    "manifest_hash = lower(manifest_hash) AND "
    f"length({_MANIFEST_HASH_REMAINDER}) = 0"
)


class DeletionRequest(Base):
    __tablename__ = "deletion_requests"
    __table_args__ = (
        UniqueConstraint("organization_id", "id"),
        ForeignKeyConstraint(
            ["organization_id", "candidate_id"],
            ["candidates.organization_id", "candidates.id"],
        ),
        ForeignKeyConstraint(
            ["organization_id", "requested_by"],
            ["users.organization_id", "users.id"],
        ),
        ForeignKeyConstraint(
            ["organization_id", "approved_by"],
            ["users.organization_id", "users.id"],
        ),
        CheckConstraint(
            "status in ('requested','approved','executing','completed','failed')"
        ),
        CheckConstraint("version >= 1"),
        CheckConstraint(
            "reason_code in ('retention_expired','candidate_request','administrator_request')"
        ),
        CheckConstraint(
            "requested_by IS NOT NULL OR reason_code = 'retention_expired' OR recovery_generation > 0"
        ),
        CheckConstraint("manifest_schema_version = 1"),
        CheckConstraint(_MANIFEST_HASH_CHECK),
        CheckConstraint("policy_version >= 1"),
        CheckConstraint("candidate_version >= 1"),
        CheckConstraint("recovery_generation >= 0"),
        Index(
            "uq_deletion_requests_open_candidate",
            "organization_id",
            "candidate_id",
            unique=True,
            postgresql_where=text("status <> 'completed'"),
            sqlite_where=text("status <> 'completed'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    candidate_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="requested")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    reason_code: Mapped[str] = mapped_column(String(32), nullable=False)
    requested_by: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    approved_by: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    execution_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    safe_error_code: Mapped[str | None] = mapped_column(String(64))
    impact_manifest: Mapped[dict] = mapped_column(_PRIVATE_JSON, nullable=False)
    manifest_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    manifest_schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    policy_version: Mapped[int] = mapped_column(Integer, nullable=False)
    candidate_version: Mapped[int] = mapped_column(Integer, nullable=False)
    recovery_generation: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    database_redaction_checksum: Mapped[str | None] = mapped_column(String(64))
    ledger_completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ledger_object_key: Mapped[str | None] = mapped_column(String(512))
    ledger_sha256: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )


class DeletionArtifact(Base):
    __tablename__ = "deletion_artifacts"
    __table_args__ = (
        UniqueConstraint("request_id", "kind", "storage_key"),
        ForeignKeyConstraint(
            ["organization_id", "request_id"],
            ["deletion_requests.organization_id", "deletion_requests.id"],
            ondelete="CASCADE",
        ),
        CheckConstraint("kind in ('resume_object','report_export_object')"),
        CheckConstraint("status in ('pending','deleted')"),
        CheckConstraint("attempts >= 0"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    request_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    storage_key: Mapped[str] = mapped_column(String(512), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    safe_error_code: Mapped[str | None] = mapped_column(String(64))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )


class LegalHold(Base):
    __tablename__ = "legal_holds"
    __table_args__ = (
        UniqueConstraint("organization_id", "id"),
        ForeignKeyConstraint(
            ["organization_id", "candidate_id"],
            ["candidates.organization_id", "candidates.id"],
        ),
        ForeignKeyConstraint(
            ["organization_id", "placed_by"],
            ["users.organization_id", "users.id"],
        ),
        ForeignKeyConstraint(
            ["organization_id", "released_by"],
            ["users.organization_id", "users.id"],
        ),
        CheckConstraint("length(reason) <= 1000"),
        CheckConstraint("released_reason IS NULL OR length(released_reason) <= 1000"),
        CheckConstraint("version >= 1"),
        Index(
            "uq_legal_holds_active_candidate",
            "organization_id",
            "candidate_id",
            unique=True,
            postgresql_where=text("released_at IS NULL"),
            sqlite_where=text("released_at IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    candidate_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    reason: Mapped[str] = mapped_column(String(1000), nullable=False)
    placed_by: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    placed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    released_by: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    released_reason: Mapped[str | None] = mapped_column(String(1000))
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )


class DeletionRecoveryRun(Base):
    __tablename__ = "deletion_recovery_runs"
    __table_args__ = (
        UniqueConstraint("organization_id", "id"),
        UniqueConstraint("organization_id", "restore_id"),
        ForeignKeyConstraint(
            ["organization_id", "queue_job_id"],
            ["background_jobs.organization_id", "background_jobs.id"],
        ),
        CheckConstraint("status in ('queued','running','completed','failed')"),
        CheckConstraint("restored_candidate_count >= 0"),
        CheckConstraint("requeued_request_count >= 0"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    restore_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    restored_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="queued")
    restored_candidate_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    requeued_request_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    safe_error_code: Mapped[str | None] = mapped_column(String(64))
    queued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    queue_job_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )


class DeletionRecoveryCheckpoint(Base):
    __tablename__ = "deletion_recovery_checkpoints"
    __table_args__ = (
        UniqueConstraint("organization_id", "id"),
        UniqueConstraint("run_id", "ledger_sha256"),
        ForeignKeyConstraint(
            ["organization_id", "run_id"],
            ["deletion_recovery_runs.organization_id", "deletion_recovery_runs.id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["organization_id", "queue_job_id"],
            ["background_jobs.organization_id", "background_jobs.id"],
        ),
        CheckConstraint("status in ('pending','running','completed','failed')"),
        CheckConstraint("attempts >= 0"),
        CheckConstraint("target_generation >= 1"),
        CheckConstraint(_MANIFEST_HASH_CHECK.replace("manifest_hash", "ledger_sha256")),
        Index("ix_deletion_recovery_checkpoints_run_status", "run_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    run_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    deletion_request_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    ledger_object_key: Mapped[str] = mapped_column(String(512), nullable=False)
    ledger_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    target_generation: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    safe_error_code: Mapped[str | None] = mapped_column(String(64))
    queue_job_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )
