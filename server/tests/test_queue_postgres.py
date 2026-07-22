import asyncio
import os
import subprocess
import threading
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.orm import Session

from server.app.queue.models import BackgroundJob, JobAttempt, OutboxEvent
from server.app.queue.repository import LeaseRejected, QueueRepository
from server.app.queue.payloads import OpaqueIdField, PayloadPolicyRegistry, PayloadSchema
from server.app.queue.payloads import UnsafePayload
from server.app.queue.runtime import DatabaseQueueGateway
from server.app.worker.main import Worker


pytestmark = pytest.mark.skipif(not os.getenv("POSTGRES_SMOKE_URL"), reason="PostgreSQL smoke URL not configured")


@pytest.fixture
def queue_db():
    async_url = os.environ["POSTGRES_SMOKE_URL"]
    env = {**os.environ, "DATABASE_URL": async_url}
    subprocess.run(["python", "-m", "alembic", "-c", "server/alembic.ini", "upgrade", "head"], check=True, env=env)
    engine = create_engine(async_url.replace("+asyncpg", "+psycopg"))
    with engine.begin() as connection:
        connection.execute(text("TRUNCATE outbox_events, job_attempts, background_jobs, organizations CASCADE"))
        connection.execute(text("UPDATE queue_claim_cursors SET last_organization_id=NULL, updated_at=now()"))
    yield engine
    engine.dispose()


def organization(session: Session, slug: str) -> uuid.UUID:
    organization_id = uuid.uuid4()
    session.execute(text("INSERT INTO organizations (id, slug, name, status, created_at, updated_at) VALUES (:id, :slug, :slug, 'active', now(), now())"), {"id": organization_id, "slug": slug})
    return organization_id


def policies() -> PayloadPolicyRegistry:
    registry = PayloadPolicyRegistry()
    for job_type in ("test.export", "test.work", "test.cancel"):
        registry.register_job(job_type, PayloadSchema({}))
    registry.register_topic("audit.created", PayloadSchema({"candidate_id": OpaqueIdField()}))
    registry.register_job("test.unknown", PayloadSchema({}))
    return registry


def repository(session: Session, *, jitter=lambda _: 0) -> QueueRepository:
    return QueueRepository(session, jitter=jitter, policies=policies())


def test_enqueue_and_outbox_are_transactional_and_dedupe_is_terminal_aware(queue_db) -> None:
    with Session(queue_db) as session:
        org = organization(session, "atomic")
        repo = repository(session)
        first = repo.enqueue(org, "test.export", {}, dedupe_key="same")
        assert repo.enqueue(org, "test.export", {}, dedupe_key="same").id == first.id
        repo.append_outbox(org, "audit.created", "candidate", uuid.uuid4(), {"candidate_id": str(uuid.uuid4())})
        session.rollback()
    with Session(queue_db) as session:
        assert session.scalar(select(BackgroundJob)) is None
        assert session.scalar(select(OutboxEvent)) is None
        org = organization(session, "terminal")
        repo = repository(session)
        first = repo.enqueue(org, "test.export", {}, dedupe_key="same")
        session.commit()
        claimed = repo.claim(org, "w1", lease_seconds=30)
        repo.succeed(org, claimed.id, "w1")
        session.commit()
        assert repo.enqueue(org, "test.export", {}, dedupe_key="same").id != first.id


def test_claim_is_exclusive_and_ordered(queue_db) -> None:
    with Session(queue_db) as session:
        org = organization(session, "claim")
        repo = repository(session)
        low = repo.enqueue(org, "test.work", {}, priority=1)
        high = repo.enqueue(org, "test.work", {}, priority=9)
        future = repo.enqueue(org, "test.work", {}, priority=99, run_after=repo.database_now() + timedelta(hours=1))
        session.commit()
        low_id, high_id, future_id = low.id, high.id, future.id
    barrier = threading.Barrier(2)
    claimed: list[uuid.UUID | None] = []
    def take(worker: str) -> None:
        with Session(queue_db) as session:
            barrier.wait()
            item = repository(session).claim(org, worker, lease_seconds=30)
            claimed.append(item.id if item else None)
            session.commit()
    threads = [threading.Thread(target=take, args=(f"w{i}",)) for i in range(2)]
    [thread.start() for thread in threads]
    [thread.join() for thread in threads]
    assert set(claimed) == {high_id, low_id}
    assert future_id not in claimed


def test_gateway_rotates_tenants_for_jobs_and_outbox(queue_db) -> None:
    with Session(queue_db) as session:
        organizations = sorted((organization(session, "fair-a"), organization(session, "fair-b")))
        first, second = organizations
        repo = repository(session)
        repo.enqueue(first, "test.work", {})
        repo.enqueue(first, "test.work", {})
        second_job = repo.enqueue(second, "test.work", {})
        repo.append_outbox(first, "audit.created", "candidate", uuid.uuid4(), {"candidate_id": str(uuid.uuid4())})
        repo.append_outbox(first, "audit.created", "candidate", uuid.uuid4(), {"candidate_id": str(uuid.uuid4())})
        second_event = repo.append_outbox(second, "audit.created", "candidate", uuid.uuid4(), {"candidate_id": str(uuid.uuid4())})
        session.commit()
        second_job_id, second_event_id = second_job.id, second_event.id

    async def claim_two_of_each():
        jobs = [
            await DatabaseQueueGateway(os.environ["POSTGRES_SMOKE_URL"], policies()).claim_job(worker_id="worker", lease_seconds=30)
            for _ in range(2)
        ]
        events = [
            await DatabaseQueueGateway(os.environ["POSTGRES_SMOKE_URL"], policies()).claim_outbox(worker_id="dispatcher", lease_seconds=30)
            for _ in range(2)
        ]
        return jobs, events

    jobs, events = asyncio.run(claim_two_of_each())
    assert jobs[1].id == second_job_id
    assert events[1].id == second_event_id


def test_gateway_reap_budget_is_global_for_jobs_and_outbox(queue_db) -> None:
    with Session(queue_db) as session:
        organizations = (organization(session, "reap-a"), organization(session, "reap-b"))
        repo = repository(session)
        jobs = []
        events = []
        for org in organizations:
            jobs.extend(repo.enqueue(org, "test.work", {}, max_attempts=1) for _ in range(101))
            events.extend(
                repo.append_outbox(org, "audit.created", "candidate", uuid.uuid4(), {"candidate_id": str(uuid.uuid4())}, max_attempts=1)
                for _ in range(101)
            )
        now = repo.database_now()
        expired_at = now - timedelta(seconds=1)
        for job in jobs:
            job.status = "running"
            job.attempts = 1
            job.lease_owner = "old-worker"
            job.lease_expires_at = expired_at
            job.heartbeat_at = expired_at
            session.add(JobAttempt(organization_id=job.organization_id, job_id=job.id, attempt_no=1, started_at=expired_at, worker_id="old-worker"))
        for event in events:
            event.status = "running"
            event.attempts = 1
            event.lease_owner = "old-dispatcher"
            event.lease_expires_at = expired_at
            event.heartbeat_at = expired_at
        session.commit()

    gateway = DatabaseQueueGateway(os.environ["POSTGRES_SMOKE_URL"], policies())

    async def reap_once():
        return (
            await gateway.claim_job(worker_id="new-worker", lease_seconds=30),
            await gateway.claim_outbox(worker_id="new-dispatcher", lease_seconds=30),
        )

    assert asyncio.run(reap_once()) == (None, None)
    with Session(queue_db) as session:
        assert session.scalar(select(text("count(*)")).select_from(BackgroundJob).where(BackgroundJob.status == "dead_letter")) == 100
        assert session.scalar(select(text("count(*)")).select_from(OutboxEvent).where(OutboxEvent.status == "failed")) == 100


def test_owner_checks_reclaim_retry_cancel_and_tenant_isolation(queue_db) -> None:
    with Session(queue_db) as session:
        org = organization(session, "owner")
        other = organization(session, "other")
        repo = repository(session, jitter=lambda _: 0)
        job = repo.enqueue(org, "test.work", {}, max_attempts=2)
        session.commit()
        claimed = repo.claim(org, "old", lease_seconds=30)
        with pytest.raises(LeaseRejected): repo.heartbeat(org, job.id, "wrong", lease_seconds=30)
        with pytest.raises(LeaseRejected): repo.succeed(other, job.id, "old")
        session.execute(text("UPDATE background_jobs SET lease_expires_at = now() - interval '1 second' WHERE id=:id"), {"id": job.id})
        session.commit()
        reclaimed = repo.claim(org, "new", lease_seconds=30)
        assert reclaimed.id == job.id and reclaimed.attempts == 2
        with pytest.raises(LeaseRejected): repo.succeed(org, job.id, "old")
        repo.fail(org, job.id, "new", safe_code="temporary", retryable=True)
        session.commit()
        stored = session.get(BackgroundJob, job.id)
        assert stored.status == "dead_letter"
        attempts = session.scalars(select(JobAttempt).where(JobAttempt.job_id == job.id).order_by(JobAttempt.attempt_no)).all()
        assert [attempt.result for attempt in attempts] == ["abandoned", "failed"]
        cancelled = repo.enqueue(org, "test.cancel", {})
        assert repo.cancel(org, cancelled.id)
        assert repo.cancel(org, cancelled.id)
        session.commit()
        assert repo.claim(org, "new", lease_seconds=30) is None


def test_expired_job_reaping_is_bounded(queue_db) -> None:
    with Session(queue_db) as session:
        org = organization(session, "bounded-reaper")
        repo = repository(session)
        for _ in range(101):
            repo.enqueue(org, "test.work", {}, max_attempts=2)
        session.commit()

        for _ in range(101):
            assert repo.claim(org, "old-worker", lease_seconds=30) is not None
        session.execute(text("UPDATE background_jobs SET lease_expires_at=now()-interval '1 second' WHERE organization_id=:org"), {"org": org})
        session.commit()

        assert repo.claim(org, "new-worker", lease_seconds=30) is not None
        session.commit()
        still_owned_by_old_worker = session.scalar(
            select(text("count(*)")).select_from(BackgroundJob).where(
                BackgroundJob.organization_id == org,
                BackgroundJob.status == "running",
                BackgroundJob.lease_owner == "old-worker",
            )
        )
        assert still_owned_by_old_worker == 1


def test_outbox_retries_and_publishes_once_and_attempt_history_is_immutable(queue_db) -> None:
    with Session(queue_db) as session:
        org = organization(session, "outbox")
        repo = repository(session, jitter=lambda _: 0)
        event = repo.append_outbox(org, "audit.created", "candidate", uuid.uuid4(), {"candidate_id": str(uuid.uuid4())}, max_attempts=2)
        job = repo.enqueue(org, "test.work", {})
        session.commit()
        claimed = repo.claim_outbox(org, "d1", lease_seconds=30)
        repo.fail_outbox(org, claimed.id, "d1", safe_code="temporary", retryable=True)
        session.flush()
        session.execute(text("UPDATE outbox_events SET available_at=now() WHERE id=:id"), {"id": event.id})
        claimed = repo.claim_outbox(org, "d2", lease_seconds=30)
        repo.publish_outbox(org, claimed.id, "d2")
        running = repo.claim(org, "w", lease_seconds=30)
        repo.succeed(org, running.id, "w")
        session.commit()
        with pytest.raises(LeaseRejected): repo.publish_outbox(org, event.id, "d2")
        with pytest.raises(Exception):
            session.execute(text("UPDATE job_attempts SET result='changed' WHERE job_id=:id"), {"id": job.id})


def test_concurrent_active_dedupe_returns_one_job_without_losing_caller_transactions(queue_db) -> None:
    with Session(queue_db) as session:
        org = organization(session, "dedupe-race"); session.commit()
    barrier = threading.Barrier(2); ids: list[uuid.UUID] = []; errors: list[Exception] = []
    def enqueue(worker: int) -> None:
        try:
            with Session(queue_db) as session:
                session.execute(text("SELECT :marker"), {"marker": worker})
                barrier.wait()
                job = repository(session).enqueue(org, "test.work", {}, dedupe_key="stable-key")
                session.commit(); ids.append(job.id)
        except Exception as error: errors.append(error)
    threads = [threading.Thread(target=enqueue, args=(index,)) for index in range(2)]
    [thread.start() for thread in threads]; [thread.join() for thread in threads]
    assert errors == [] and len(ids) == 2 and len(set(ids)) == 1


def test_retry_permanent_failure_and_cancellation_state_contract(queue_db) -> None:
    with Session(queue_db) as session:
        org = organization(session, "states"); repo = repository(session)
        retry = repo.enqueue(org, "test.work", {}, max_attempts=3); queued = repo.enqueue(org, "test.cancel", {}, priority=-1); session.commit()
        claimed = repo.claim(org, "w", lease_seconds=30); repo.fail(org, claimed.id, "w", safe_code="temporary_unavailable", retryable=True); session.commit()
        session.refresh(retry); assert retry.status == "queued" and retry.run_after > repo.database_now()
        session.execute(text("UPDATE background_jobs SET run_after=now() WHERE id=:id"), {"id": retry.id}); session.commit()
        claimed = repo.claim(org, "w", lease_seconds=30); repo.fail(org, claimed.id, "w", safe_code="invalid_payload", retryable=False); session.commit()
        session.refresh(retry); assert retry.status == "dead_letter" and retry.last_error_code == "invalid_payload"
        assert repo.cancel(org, queued.id); session.commit(); assert session.get(BackgroundJob, queued.id).status == "cancelled"
        running = repo.enqueue(org, "test.cancel", {}); session.commit(); running = repo.claim(org, "w", lease_seconds=30); assert repo.cancel(org, running.id); session.commit()
        assert session.get(BackgroundJob, running.id).status == "cancelled"
        done = repo.enqueue(org, "test.work", {}); session.commit(); done = repo.claim(org, "w", lease_seconds=30); repo.succeed(org, done.id, "w"); session.commit()
        assert not repo.cancel(org, done.id)


def test_outbox_concurrency_heartbeat_stale_replay_and_terminal_failure(queue_db) -> None:
    with Session(queue_db) as session:
        org = organization(session, "outbox-contract"); repo = repository(session)
        first = repo.append_outbox(org, "audit.created", "candidate", uuid.uuid4(), {"candidate_id": str(uuid.uuid4())}, max_attempts=3)
        second = repo.append_outbox(org, "audit.created", "candidate", uuid.uuid4(), {"candidate_id": str(uuid.uuid4())}, max_attempts=1); session.commit()
        first_id, second_id = first.id, second.id
    barrier = threading.Barrier(2); claimed_ids: list[uuid.UUID] = []
    def claim(worker: str) -> None:
        with Session(queue_db) as session:
            barrier.wait(); event = repository(session).claim_outbox(org, worker, lease_seconds=30); claimed_ids.append(event.id); session.commit()
    threads = [threading.Thread(target=claim, args=(f"d{index}",)) for index in range(2)]
    [thread.start() for thread in threads]; [thread.join() for thread in threads]
    assert set(claimed_ids) == {first_id, second_id}
    with Session(queue_db) as session:
        repo = repository(session); first = session.get(OutboxEvent, first_id); second = session.get(OutboxEvent, second_id)
        with pytest.raises(LeaseRejected): repo.heartbeat_outbox(org, first.id, "wrong", lease_seconds=30)
        owner = first.lease_owner; repo.heartbeat_outbox(org, first.id, owner, lease_seconds=30)
        repo.fail_outbox(org, second.id, second.lease_owner, safe_code="raw email person@example.test", retryable=True); session.commit()
        session.refresh(second); assert second.status == "failed" and second.failed_at is not None and second.safe_error_code == "internal_error"
        session.execute(text("UPDATE outbox_events SET lease_expires_at=now()-interval '1 second' WHERE id=:id"), {"id": first.id}); session.commit()
        replay = repo.claim_outbox(org, "replay", lease_seconds=30); assert replay.id == first_id
        external_idempotency_keys = [first_id, replay.id]
        assert external_idempotency_keys[0] == external_idempotency_keys[1]
        repo.publish_outbox(org, replay.id, "replay"); session.commit(); assert session.get(OutboxEvent, first_id).status == "published"


def test_policy_rejection_tenant_isolation_and_index_metadata(queue_db) -> None:
    with Session(queue_db) as session:
        org = organization(session, "policy"); other = organization(session, "policy-other"); repo = repository(session)
        with pytest.raises(UnsafePayload): repo.enqueue(org, "test.work", {"benign": "resume text"})
        with pytest.raises(UnsafePayload): repo.append_outbox(org, "unknown.topic", "candidate", uuid.uuid4(), {})
        job = repo.enqueue(org, "test.work", {}); event = repo.append_outbox(org, "audit.created", "candidate", uuid.uuid4(), {"candidate_id": str(uuid.uuid4())}); session.commit()
        assert repo.claim(other, "w", lease_seconds=30) is None and repo.claim_outbox(other, "d", lease_seconds=30) is None
        with pytest.raises(LeaseRejected): repo.succeed(other, job.id, "w")
        with pytest.raises(LeaseRejected): repo.publish_outbox(other, event.id, "d")
    inspector = inspect(queue_db)
    job_indexes = {item["name"]: item["column_names"] for item in inspector.get_indexes("background_jobs")}
    outbox_indexes = {item["name"]: item["column_names"] for item in inspector.get_indexes("outbox_events")}
    assert job_indexes["ix_background_jobs_claim"][:2] == ["organization_id", "priority"]
    assert job_indexes["ix_background_jobs_stale_lease"] == ["organization_id", "lease_expires_at"]
    assert outbox_indexes["ix_outbox_events_claim"][:2] == ["organization_id", "available_at"]
    assert outbox_indexes["ix_outbox_events_stale_lease"] == ["organization_id", "lease_expires_at"]
    assert {item["name"] for item in inspector.get_check_constraints("background_jobs")} >= {"ck_background_jobs_status", "ck_background_jobs_attempts"}
    assert {item["name"] for item in inspector.get_check_constraints("outbox_events")} >= {"ck_outbox_events_status", "ck_outbox_events_attempts"}


def test_expired_outbox_at_max_attempts_is_reaped_to_failed(queue_db) -> None:
    with Session(queue_db) as session:
        org = organization(session, "outbox-abandoned"); repo = repository(session)
        event = repo.append_outbox(org, "audit.created", "candidate", uuid.uuid4(), {"candidate_id": str(uuid.uuid4())}, max_attempts=1); session.commit()
        claimed = repo.claim_outbox(org, "crashed", lease_seconds=30); assert claimed.id == event.id
        session.execute(text("UPDATE outbox_events SET lease_expires_at=now()-interval '1 second' WHERE id=:id"), {"id": event.id}); session.commit()
        assert repo.claim_outbox(org, "next", lease_seconds=30) is None
        session.refresh(event)
        assert event.status == "failed" and event.failed_at is not None
        assert event.safe_error_code == "delivery_abandoned" and event.lease_owner is None and event.lease_expires_at is None


def test_persisted_unknown_job_type_is_dead_lettered_without_execution(queue_db) -> None:
    with Session(queue_db) as session:
        org = organization(session, "unknown-job"); repo = repository(session)
        job = repo.enqueue(org, "test.unknown", {}); session.commit(); job_id = job.id
    gateway = DatabaseQueueGateway(os.environ["POSTGRES_SMOKE_URL"], policies())
    class Probe:
        async def check(self) -> None: pass
    worker = Worker(Probe(), Probe(), interval_seconds=0, queue=gateway, handlers={}, outbox_handlers={}, worker_id="unknown-worker", lease_seconds=30, heartbeat_seconds=10)
    asyncio.run(worker._poll_once())
    with Session(queue_db) as session:
        stored = session.get(BackgroundJob, job_id)
        assert stored.status == "dead_letter" and stored.last_error_code == "unknown_job_type"
        assert session.scalar(select(JobAttempt).where(JobAttempt.job_id == job_id)).safe_error_code == "unknown_job_type"
