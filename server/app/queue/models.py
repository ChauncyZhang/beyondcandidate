import uuid
from datetime import datetime
from sqlalchemy import JSON, CheckConstraint, DateTime, ForeignKeyConstraint, Index, Integer, String, UniqueConstraint, Uuid, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from server.app.identity.models import Base

JSON_DOCUMENT = JSON().with_variant(JSONB(), "postgresql")

class BackgroundJob(Base):
    __tablename__ = "background_jobs"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    type: Mapped[str] = mapped_column(String(100)); payload: Mapped[dict] = mapped_column(JSON_DOCUMENT, default=dict)
    status: Mapped[str] = mapped_column(String(20), default="queued"); priority: Mapped[int] = mapped_column(Integer, default=0)
    attempts: Mapped[int] = mapped_column(Integer, default=0); max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    run_after: Mapped[datetime] = mapped_column(DateTime(timezone=True)); lease_owner: Mapped[str | None] = mapped_column(String(200))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True)); heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    dedupe_key: Mapped[str | None] = mapped_column(String(255)); last_error_code: Mapped[str | None] = mapped_column(String(100)); trace_id: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True)); updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    __table_args__ = (ForeignKeyConstraint(["organization_id"], ["organizations.id"]), UniqueConstraint("organization_id", "id"), CheckConstraint("status in ('queued','running','succeeded','failed','cancelled','dead_letter')", name="ck_background_jobs_status"), CheckConstraint("attempts >= 0 AND max_attempts > 0 AND attempts <= max_attempts", name="ck_background_jobs_attempts"), Index("ix_background_jobs_claim", "organization_id", text("priority DESC"), "run_after", "created_at", postgresql_where=text("status = 'queued'")), Index("ix_background_jobs_stale_lease", "organization_id", "lease_expires_at", postgresql_where=text("status = 'running'")), Index("uq_background_jobs_active_dedupe", "organization_id", "type", "dedupe_key", unique=True, postgresql_where=text("dedupe_key IS NOT NULL AND status IN ('queued','running')"), sqlite_where=text("dedupe_key IS NOT NULL AND status IN ('queued','running')")))

class JobAttempt(Base):
    __tablename__ = "job_attempts"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4); organization_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    job_id: Mapped[uuid.UUID] = mapped_column(Uuid); attempt_no: Mapped[int] = mapped_column(Integer); started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True)); worker_id: Mapped[str] = mapped_column(String(200)); result: Mapped[str | None] = mapped_column(String(30)); safe_error_code: Mapped[str | None] = mapped_column(String(100)); duration_ms: Mapped[int | None] = mapped_column(Integer)
    __table_args__ = (ForeignKeyConstraint(["organization_id", "job_id"], ["background_jobs.organization_id", "background_jobs.id"], ondelete="CASCADE"), UniqueConstraint("job_id", "attempt_no", name="uq_job_attempt_number"))

class OutboxEvent(Base):
    __tablename__ = "outbox_events"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4); organization_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    topic: Mapped[str] = mapped_column(String(100)); aggregate_type: Mapped[str] = mapped_column(String(100)); aggregate_id: Mapped[uuid.UUID] = mapped_column(Uuid); payload: Mapped[dict] = mapped_column(JSON_DOCUMENT)
    status: Mapped[str] = mapped_column(String(20), default="queued"); available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True)); published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True)); failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True)); attempts: Mapped[int] = mapped_column(Integer, default=0); max_attempts: Mapped[int] = mapped_column(Integer, default=5)
    lease_owner: Mapped[str | None] = mapped_column(String(200)); lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True)); heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True)); safe_error_code: Mapped[str | None] = mapped_column(String(100)); created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True)); updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    __table_args__ = (ForeignKeyConstraint(["organization_id"], ["organizations.id"]), CheckConstraint("status in ('queued','running','published','failed')", name="ck_outbox_events_status"), CheckConstraint("attempts >= 0 AND max_attempts > 0 AND attempts <= max_attempts", name="ck_outbox_events_attempts"), Index("ix_outbox_events_claim", "organization_id", "available_at", "created_at", postgresql_where=text("status = 'queued'")), Index("ix_outbox_events_stale_lease", "organization_id", "lease_expires_at", postgresql_where=text("status = 'running'")))

class QueueClaimCursor(Base):
    __tablename__ = "queue_claim_cursors"
    kind: Mapped[str] = mapped_column(String(20), primary_key=True)
    last_organization_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    __table_args__ = (CheckConstraint("kind in ('job','outbox')", name="ck_queue_claim_cursors_kind"),)
