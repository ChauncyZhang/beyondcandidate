import uuid
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from server.app.queue.models import BackgroundJob, JobAttempt, OutboxEvent
from server.app.queue.payloads import DEFAULT_PAYLOAD_POLICIES, PayloadPolicyRegistry
from server.app.queue.service import normalize_safe_code, retry_delay

class LeaseRejected(RuntimeError): pass
TERMINAL_CALLBACK_TYPES={"screening.parse_item","screening.score_item","screening.llm_score_item","screening.llm_finalize_terminal","reports.export","governance.delete_candidate","governance.retention_sweep","governance.redelete_after_restore"}
LEASE_REAP_BATCH_SIZE = 100

class QueueRepository:
    def __init__(self, session: Session, *, jitter: Callable[[int], int] = lambda _: 0, policies: PayloadPolicyRegistry = DEFAULT_PAYLOAD_POLICIES, terminal_callbacks: Mapping[str,Callable] | None = None) -> None:
        self.session,self.jitter,self.policies=session,jitter,policies; self.terminal_callbacks=dict(terminal_callbacks or {})
        if not set(self.terminal_callbacks)<=TERMINAL_CALLBACK_TYPES: raise ValueError("terminal callback type is not allowlisted")
    def database_now(self) -> datetime:
        value=self.session.scalar(select(text("CURRENT_TIMESTAMP")))
        return datetime.fromisoformat(value) if isinstance(value,str) else value

    def enqueue(self, organization_id: uuid.UUID, job_type: str, payload: Mapping[str, object], *, priority: int = 0, max_attempts: int = 3, run_after: datetime | None = None, dedupe_key: str | None = None, trace_id: str | None = None) -> BackgroundJob:
        job_type = self.policies.validate_type(job_type); dedupe_key = self.policies.validate_identifier(dedupe_key, field="dedupe_key"); trace_id = self.policies.validate_identifier(trace_id, field="trace_id")
        validated = self.policies.validate_job(job_type, payload)
        query = select(BackgroundJob).where(BackgroundJob.organization_id == organization_id, BackgroundJob.type == job_type, BackgroundJob.dedupe_key == dedupe_key, BackgroundJob.status.in_(("queued", "running")))
        if dedupe_key and (existing := self.session.scalar(query)): return existing
        now = self.database_now(); job = BackgroundJob(organization_id=organization_id, type=job_type, payload=validated, priority=priority, max_attempts=max_attempts, run_after=run_after or now, dedupe_key=dedupe_key, trace_id=trace_id, created_at=now, updated_at=now)
        savepoint = self.session.begin_nested()
        try:
            self.session.add(job); self.session.flush(); savepoint.commit()
        except IntegrityError:
            savepoint.rollback()
            if not dedupe_key or not (existing := self.session.scalar(query)): raise
            return existing
        return job

    def _abandon_expired(self, organization_id: uuid.UUID, now: datetime, *, limit: int) -> int:
        if limit <= 0:
            return 0
        jobs = self.session.scalars(
            select(BackgroundJob)
            .where(BackgroundJob.organization_id == organization_id, BackgroundJob.status == "running", BackgroundJob.lease_expires_at < now)
            .order_by(BackgroundJob.lease_expires_at, BackgroundJob.created_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        ).all()
        for job in jobs:
            attempt = self.session.scalar(select(JobAttempt).where(JobAttempt.job_id == job.id, JobAttempt.attempt_no == job.attempts, JobAttempt.finished_at.is_(None)).with_for_update())
            if attempt: self._finish_attempt(attempt, now, "abandoned", "lease_expired")
            job.status = "queued" if job.attempts < job.max_attempts else "dead_letter"; job.last_error_code = "lease_expired"; job.run_after = now; self._clear_lease(job, now)
            if job.status=="dead_letter": self._terminal(job,"lease_expired",now)
        self.session.flush()

        return len(jobs)

    def reap_expired_jobs(self, organization_id: uuid.UUID, *, limit: int = LEASE_REAP_BATCH_SIZE) -> int:
        return self._abandon_expired(organization_id, self.database_now(), limit=limit)

    def claim(self, organization_id: uuid.UUID, worker_id: str, *, lease_seconds: int, recover_expired: bool = True) -> BackgroundJob | None:
        now = self.database_now()
        if recover_expired: self._abandon_expired(organization_id, now, limit=LEASE_REAP_BATCH_SIZE)
        job = self.session.scalar(select(BackgroundJob).where(BackgroundJob.organization_id == organization_id, BackgroundJob.status == "queued", BackgroundJob.run_after <= now).order_by(BackgroundJob.priority.desc(), BackgroundJob.run_after, BackgroundJob.created_at).limit(1).with_for_update(skip_locked=True))
        if not job: return None
        job.status = "running"; job.attempts += 1; job.lease_owner = worker_id; job.lease_expires_at = now + timedelta(seconds=lease_seconds); job.heartbeat_at = now; job.updated_at = now
        self.session.add(JobAttempt(organization_id=organization_id, job_id=job.id, attempt_no=job.attempts, started_at=now, worker_id=worker_id)); self.session.flush()
        return job

    def _owned(self, organization_id: uuid.UUID, job_id: uuid.UUID, worker_id: str):
        now = self.database_now(); job = self.session.scalar(select(BackgroundJob).where(BackgroundJob.id == job_id, BackgroundJob.organization_id == organization_id).with_for_update())
        if not job or job.status != "running" or job.lease_owner != worker_id or not job.lease_expires_at or job.lease_expires_at <= now: raise LeaseRejected("job lease is no longer owned")
        attempt = self.session.scalar(select(JobAttempt).where(JobAttempt.job_id == job.id, JobAttempt.attempt_no == job.attempts, JobAttempt.finished_at.is_(None)).with_for_update())
        if not attempt: raise LeaseRejected("active attempt is missing")
        return job, attempt, now

    def heartbeat(self, organization_id, job_id, worker_id, *, lease_seconds: int) -> None:
        job, _, now = self._owned(organization_id, job_id, worker_id); job.heartbeat_at = now; job.lease_expires_at = now + timedelta(seconds=lease_seconds); job.updated_at = now
    def succeed(self, organization_id, job_id, worker_id) -> None:
        job, attempt, now = self._owned(organization_id, job_id, worker_id); self._finish_attempt(attempt, now, "succeeded"); job.status = "succeeded"; self._clear_lease(job, now)
    def fail(self, organization_id, job_id, worker_id, *, safe_code: str, retryable: bool) -> None:
        safe_code = normalize_safe_code(safe_code)
        job, attempt, now = self._owned(organization_id, job_id, worker_id); self._finish_attempt(attempt, now, "failed", safe_code); job.last_error_code = safe_code; self._clear_lease(job, now)
        if retryable and job.attempts < job.max_attempts: job.status = "queued"; job.run_after = now + retry_delay(job.attempts, jitter=self.jitter)
        else: job.status = "dead_letter"; self._terminal(job,safe_code,now)
    def _terminal(self,job,safe_code,now):
        callback=self.terminal_callbacks.get(job.type)
        if callback: callback(self.session,job,safe_code,now)
    @staticmethod
    def _finish_attempt(attempt, now, result, safe_code=None): attempt.finished_at = now; attempt.result = result; attempt.safe_error_code = safe_code; attempt.duration_ms = max(0, int((now-attempt.started_at).total_seconds()*1000))
    @staticmethod
    def _clear_lease(job, now): job.lease_owner = None; job.lease_expires_at = None; job.heartbeat_at = None; job.updated_at = now

    def cancel(self, organization_id, job_id) -> bool:
        job = self.session.scalar(select(BackgroundJob).where(BackgroundJob.id == job_id, BackgroundJob.organization_id == organization_id).with_for_update())
        if not job: return False
        if job.status == "cancelled": return True
        if job.status in ("succeeded", "failed", "dead_letter"): return False
        now = self.database_now()
        if job.status == "running" and (attempt := self.session.scalar(select(JobAttempt).where(JobAttempt.job_id == job.id, JobAttempt.attempt_no == job.attempts, JobAttempt.finished_at.is_(None)))): self._finish_attempt(attempt, now, "cancelled")
        job.status = "cancelled"; self._clear_lease(job, now); return True

    def append_outbox(self, organization_id, topic, aggregate_type, aggregate_id, payload, *, max_attempts=5):
        topic = self.policies.validate_type(topic); aggregate_type = self.policies.validate_identifier(aggregate_type, field="aggregate_type")
        validated = self.policies.validate_topic(topic, payload)
        now = self.database_now(); event = OutboxEvent(organization_id=organization_id, topic=topic, aggregate_type=aggregate_type, aggregate_id=aggregate_id, payload=validated, status="queued", available_at=now, max_attempts=max_attempts, created_at=now, updated_at=now); self.session.add(event); self.session.flush(); return event
    def reap_expired_outbox(self, organization_id, *, limit: int = LEASE_REAP_BATCH_SIZE) -> int:
        return self._reap_expired_outbox(organization_id, self.database_now(), limit=limit)

    def claim_outbox(self, organization_id, worker_id, *, lease_seconds, recover_expired: bool = True):
        now = self.database_now()
        if recover_expired: self._reap_expired_outbox(organization_id, now, limit=LEASE_REAP_BATCH_SIZE)
        event = self.session.scalar(select(OutboxEvent).where(OutboxEvent.organization_id == organization_id, ((OutboxEvent.status == "queued") & (OutboxEvent.available_at <= now)) | ((OutboxEvent.status == "running") & (OutboxEvent.lease_expires_at < now)), OutboxEvent.attempts < OutboxEvent.max_attempts).order_by(OutboxEvent.available_at, OutboxEvent.created_at).limit(1).with_for_update(skip_locked=True))
        if event:
            if event.status == "running": event.safe_error_code = "lease_expired"
            event.status = "running"; event.lease_owner = worker_id; event.lease_expires_at = now + timedelta(seconds=lease_seconds); event.heartbeat_at = now; event.attempts += 1; event.updated_at = now; self.session.flush()
        return event
    def _reap_expired_outbox(self, organization_id, now, *, limit: int) -> int:
        if limit <= 0:
            return 0
        expired = self.session.scalars(
            select(OutboxEvent)
            .where(OutboxEvent.organization_id == organization_id, OutboxEvent.status == "running", OutboxEvent.lease_expires_at < now, OutboxEvent.attempts >= OutboxEvent.max_attempts)
            .order_by(OutboxEvent.lease_expires_at, OutboxEvent.created_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        ).all()
        for event in expired:
            event.status = "failed"; event.failed_at = now; event.safe_error_code = "delivery_abandoned"; event.lease_owner = None; event.lease_expires_at = None; event.heartbeat_at = None; event.updated_at = now
        self.session.flush()
        return len(expired)
    def publish_outbox(self, organization_id, event_id, worker_id):
        event, now = self._owned_outbox(organization_id, event_id, worker_id); event.status = "published"; event.published_at = now; event.lease_owner = None; event.lease_expires_at = None; event.heartbeat_at = None; event.safe_error_code = None; event.updated_at = now
    def fail_outbox(self, organization_id, event_id, worker_id, *, safe_code, retryable):
        safe_code = normalize_safe_code(safe_code); event, now = self._owned_outbox(organization_id, event_id, worker_id); event.safe_error_code = safe_code; event.lease_owner = None; event.lease_expires_at = None; event.heartbeat_at = None; event.updated_at = now
        if retryable and event.attempts < event.max_attempts: event.status = "queued"; event.available_at = now + retry_delay(event.attempts, jitter=self.jitter)
        else: event.status = "failed"; event.failed_at = now
    def heartbeat_outbox(self, organization_id, event_id, worker_id, *, lease_seconds):
        event, now = self._owned_outbox(organization_id, event_id, worker_id); event.heartbeat_at = now; event.lease_expires_at = now + timedelta(seconds=lease_seconds); event.updated_at = now
    def _owned_outbox(self, organization_id, event_id, worker_id):
        now = self.database_now(); event = self.session.scalar(select(OutboxEvent).where(OutboxEvent.id == event_id, OutboxEvent.organization_id == organization_id).with_for_update())
        if not event or event.status != "running" or event.lease_owner != worker_id or not event.lease_expires_at or event.lease_expires_at <= now: raise LeaseRejected("outbox lease is no longer owned")
        return event, now
