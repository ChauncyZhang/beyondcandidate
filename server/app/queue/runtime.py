import asyncio
from bisect import bisect_right

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from server.app.identity.models import Organization
from server.app.queue.models import QueueClaimCursor
from server.app.queue.repository import LEASE_REAP_BATCH_SIZE, QueueRepository
from server.app.queue.payloads import DEFAULT_PAYLOAD_POLICIES, PayloadPolicyRegistry


class DatabaseQueueGateway:
    """Short-transaction async adapter used by the single-job worker loop."""
    def __init__(self, database_url: str, policies: PayloadPolicyRegistry = DEFAULT_PAYLOAD_POLICIES, terminal_callbacks=None) -> None:
        sync_url = database_url.replace("+asyncpg", "+psycopg")
        self._sessions = sessionmaker(create_engine(sync_url, pool_pre_ping=True), expire_on_commit=False)
        self._policies = policies
        self._terminal_callbacks = dict(terminal_callbacks or {})

    async def claim_job(self, *, worker_id: str, lease_seconds: int):
        return await asyncio.to_thread(self._claim, "job", worker_id, lease_seconds)

    async def claim_outbox(self, *, worker_id: str, lease_seconds: int):
        return await asyncio.to_thread(self._claim, "outbox", worker_id, lease_seconds)

    def _claim(self, kind: str, worker_id: str, lease_seconds: int):
        with self._sessions.begin() as session:
            cursor = session.scalar(select(QueueClaimCursor).where(QueueClaimCursor.kind == kind).with_for_update())
            if cursor is None:
                raise RuntimeError("queue claim cursor is missing")
            organization_ids = session.scalars(select(Organization.id).order_by(Organization.id)).all()
            if not organization_ids:
                return None
            start = bisect_right(organization_ids, cursor.last_organization_id) % len(organization_ids) if cursor.last_organization_id else 0
            ordered_ids = organization_ids[start:] + organization_ids[:start]
            reap_budget = LEASE_REAP_BATCH_SIZE
            last_scanned = None
            for organization_id in ordered_ids:
                repository = QueueRepository(session, policies=self._policies,terminal_callbacks=self._terminal_callbacks)
                if kind == "job":
                    reaped = repository.reap_expired_jobs(organization_id, limit=reap_budget)
                    item = repository.claim(organization_id, worker_id, lease_seconds=lease_seconds, recover_expired=False)
                else:
                    reaped = repository.reap_expired_outbox(organization_id, limit=reap_budget)
                    item = repository.claim_outbox(organization_id, worker_id, lease_seconds=lease_seconds, recover_expired=False)
                reap_budget -= reaped
                last_scanned = organization_id
                if item:
                    cursor.last_organization_id = organization_id
                    cursor.updated_at = repository.database_now()
                    session.expunge(item)
                    return item
                if reap_budget == 0:
                    break
            if last_scanned is not None:
                cursor.last_organization_id = last_scanned
                cursor.updated_at = QueueRepository(session).database_now()
        return None

    async def succeed(self, job, worker_id: str) -> None:
        await asyncio.to_thread(self._complete, job, worker_id, None, False)

    async def fail(self, job, worker_id: str, *, safe_code: str, retryable: bool) -> None:
        await asyncio.to_thread(self._complete, job, worker_id, safe_code, retryable)

    def _complete(self, job, worker_id: str, safe_code: str | None, retryable: bool) -> None:
        with self._sessions.begin() as session:
            repository = QueueRepository(session, policies=self._policies,terminal_callbacks=self._terminal_callbacks)
            if safe_code is None: repository.succeed(job.organization_id, job.id, worker_id)
            else: repository.fail(job.organization_id, job.id, worker_id, safe_code=safe_code, retryable=retryable)

    async def heartbeat(self, job, worker_id: str, *, lease_seconds: int) -> None:
        def extend() -> None:
            with self._sessions.begin() as session: QueueRepository(session, policies=self._policies,terminal_callbacks=self._terminal_callbacks).heartbeat(job.organization_id, job.id, worker_id, lease_seconds=lease_seconds)
        await asyncio.to_thread(extend)

    async def publish_outbox(self, event, worker_id: str) -> None:
        await asyncio.to_thread(self._outbox_complete, event, worker_id, None, False)

    async def fail_outbox(self, event, worker_id: str, *, safe_code: str, retryable: bool) -> None:
        await asyncio.to_thread(self._outbox_complete, event, worker_id, safe_code, retryable)

    def _outbox_complete(self, event, worker_id: str, safe_code: str | None, retryable: bool) -> None:
        with self._sessions.begin() as session:
            repository = QueueRepository(session, policies=self._policies,terminal_callbacks=self._terminal_callbacks)
            if safe_code is None: repository.publish_outbox(event.organization_id, event.id, worker_id)
            else: repository.fail_outbox(event.organization_id, event.id, worker_id, safe_code=safe_code, retryable=retryable)

    async def heartbeat_outbox(self, event, worker_id: str, *, lease_seconds: int) -> None:
        def extend() -> None:
            with self._sessions.begin() as session: QueueRepository(session, policies=self._policies,terminal_callbacks=self._terminal_callbacks).heartbeat_outbox(event.organization_id, event.id, worker_id, lease_seconds=lease_seconds)
        await asyncio.to_thread(extend)
