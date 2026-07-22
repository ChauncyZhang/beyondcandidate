import asyncio
import logging
import uuid
from datetime import timedelta

import pytest

from server.app.queue.payloads import IntegerField, OpaqueIdField, PayloadPolicyRegistry, PayloadSchema, UnsafePayload
from server.app.queue.service import PermanentJobError, RetryableJobError, retry_delay
from server.app.queue.outbox import OutboxDispatcher
from server.app.worker.main import Worker


def test_payloads_are_schema_limited_and_reject_sensitive_fields() -> None:
    candidate_id = str(uuid.uuid4())
    policies = PayloadPolicyRegistry(); policies.register_job("test.work", PayloadSchema({"candidate_id": OpaqueIdField(), "count": IntegerField(0, 10)}))
    assert policies.validate_job("test.work", {"candidate_id": candidate_id, "count": 2}) == {
        "candidate_id": candidate_id,
        "count": 2,
    }
    for payload in ({"candidate_id": candidate_id, "count": 2, "resume_text": "secret"}, {"candidate_id": "person@example.test", "count": 2}, {"candidate_id": candidate_id, "count": RuntimeError("raw")}):
        with pytest.raises(UnsafePayload):
            policies.validate_job("test.work", payload)


def test_retry_delay_is_bounded_and_accepts_deterministic_jitter() -> None:
    assert retry_delay(1, base_seconds=10, maximum_seconds=25, jitter=lambda _: 0) == timedelta(seconds=10)
    assert retry_delay(3, base_seconds=10, maximum_seconds=25, jitter=lambda _: 2) == timedelta(seconds=25)


class Probe:
    async def check(self) -> None:
        return None


class Claimed:
    id = uuid.uuid4()
    type = "known"
    attempt_no = 1
    attempts = 1
    trace_id = "trace-1"
    topic = "test.event"


class FakeQueue:
    def __init__(self) -> None:
        self.claims = [Claimed(), Claimed()]
        self.successes = 0
        self.failures = 0

    async def claim_job(self, **_: object):
        return self.claims.pop(0) if self.claims else None
    async def claim_outbox(self, **_: object): return None

    async def succeed(self, *_: object, **__: object) -> None:
        self.successes += 1

    async def fail(self, *_: object, **__: object) -> None:
        self.failures += 1
    async def heartbeat(self, *_: object, **__: object) -> None: await asyncio.Event().wait()


def test_worker_survives_handler_failure_and_unknown_type_is_safe(caplog: pytest.LogCaptureFixture) -> None:
    queue = FakeQueue()

    async def broken(_: Claimed) -> None:
        raise RuntimeError("do-not-log-this")

    worker = Worker(
        Probe(), Probe(), interval_seconds=0, queue=queue,
        handlers={"known": broken}, worker_id="worker-1", lease_seconds=30, heartbeat_seconds=10,
    )

    async def exercise() -> None:
        task = asyncio.create_task(worker.run())
        while queue.failures < 2:
            await asyncio.sleep(0)
        worker.request_shutdown()
        await asyncio.wait_for(task, 1)

    with caplog.at_level(logging.INFO):
        asyncio.run(exercise())
    assert queue.failures == 2
    assert "do-not-log-this" not in caplog.text


def test_failure_types_expose_safe_codes_only() -> None:
    assert RetryableJobError("temporary_unavailable").safe_code == "temporary_unavailable"
    assert PermanentJobError("invalid_payload").safe_code == "invalid_payload"


def test_worker_shutdown_timeout_leaves_current_job_uncompleted() -> None:
    queue = FakeQueue()
    started = asyncio.Event()

    async def hanging(_: Claimed) -> None:
        started.set()
        await asyncio.Event().wait()

    worker = Worker(Probe(), Probe(), interval_seconds=0, queue=queue, handlers={"known": hanging}, worker_id="w", lease_seconds=3, heartbeat_seconds=1, shutdown_timeout_seconds=0.01, cancel_timeout_seconds=0.01, hard_stop=lambda: None)

    async def exercise() -> None:
        task = asyncio.create_task(worker.run())
        await started.wait()
        worker.request_shutdown()
        await asyncio.wait_for(task, 0.1)

    asyncio.run(exercise())
    assert queue.successes == 0


def test_outbox_dispatcher_rejects_unknown_topics_without_calling_code() -> None:
    calls: list[tuple[str, bool]] = []
    class Event:
        topic = "unknown.topic"
    class Outbox:
        async def fail_outbox(self, _event, _worker_id: str, *, safe_code: str, retryable: bool) -> None:
            calls.append((safe_code, retryable))
    asyncio.run(OutboxDispatcher(Outbox(), {}, "worker-1").dispatch(Event()))
    assert calls == [("unknown_outbox_topic", False)]
