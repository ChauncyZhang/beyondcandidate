import asyncio
import uuid

import pytest
import logging
from pydantic import ValidationError

from server.app.core.settings import Settings
from server.app.queue.payloads import (
    BooleanField,
    EnumField,
    IntegerField,
    ListField,
    MapField,
    OpaqueIdField,
    PayloadPolicyRegistry,
    PayloadSchema,
    UnsafePayload,
)
from server.app.queue.service import JobError, normalize_safe_code
from server.app.worker.main import Worker


def policies() -> PayloadPolicyRegistry:
    registry = PayloadPolicyRegistry()
    schema = PayloadSchema({
        "candidate_id": OpaqueIdField(),
        "mode": EnumField({"fast", "safe"}),
        "count": IntegerField(minimum=0, maximum=10),
        "enabled": BooleanField(),
        "ids": ListField(OpaqueIdField(), maximum_items=3),
        "flags": MapField(BooleanField(), allowed_keys={"notify", "audit"}),
    })
    registry.register_job("test.work", schema)
    registry.register_topic("test.event", schema)
    return registry


def valid_payload() -> dict[str, object]:
    return {"candidate_id": str(uuid.uuid4()), "mode": "safe", "count": 2, "enabled": True, "ids": [], "flags": {"audit": True}}


def test_typed_payload_policy_accepts_only_registered_schema_fields() -> None:
    registry = policies()
    assert registry.validate_job("test.work", valid_payload())["mode"] == "safe"
    assert registry.validate_topic("test.event", valid_payload())["count"] == 2
    with pytest.raises(UnsafePayload): registry.validate_job("unknown", {})
    with pytest.raises(UnsafePayload): registry.validate_job("unknown", {"dedupe_key": "existing"})
    with pytest.raises(UnsafePayload): registry.validate_topic("unknown", {})
    with pytest.raises(UnsafePayload): registry.validate_job("test.work", {**valid_payload(), "extra": 1})


@pytest.mark.parametrize("value", ["person@example.test", "resume body", "two words", "\n", "safe-but-unbounded"])
def test_benign_keys_cannot_hide_sensitive_or_arbitrary_text(value: str) -> None:
    with pytest.raises(UnsafePayload): policies().validate_job("test.work", {**valid_payload(), "mode": value})


@pytest.mark.parametrize("value", ["bad type", "UPPER", "x/../../", "", "a" * 101])
def test_type_topic_and_identifier_formats_are_bounded(value: str) -> None:
    registry = policies()
    with pytest.raises(UnsafePayload): registry.validate_type(value)
    if value != "UPPER":
        with pytest.raises(UnsafePayload): registry.validate_identifier(value, field="dedupe_key")


def test_safe_error_codes_are_normalized_before_persistence_or_logging() -> None:
    assert normalize_safe_code("temporary_unavailable") == "temporary_unavailable"
    assert normalize_safe_code("raw exception: person@example.test") == "internal_error"
    assert normalize_safe_code("A" * 101) == "internal_error"
    assert JobError("raw exception").safe_code == "internal_error"


@pytest.mark.parametrize("values", [
    {"worker_lease_seconds": 30, "worker_heartbeat_seconds": 11},
    {"worker_poll_interval_seconds": 0},
    {"worker_poll_interval_seconds": 61},
    {"worker_shutdown_timeout_seconds": 301},
    {"worker_cancel_timeout_seconds": 31},
])
def test_worker_timing_settings_are_bounded(values: dict[str, object]) -> None:
    with pytest.raises(ValidationError): Settings(**values)


class Probe:
    async def check(self) -> None: pass


class Item:
    def __init__(self, kind: str = "job") -> None:
        self.id = uuid.uuid4(); self.organization_id = uuid.uuid4(); self.type = "test.work"; self.topic = "test.event"; self.attempts = 1; self.trace_id = "trace-1"; self.kind = kind


class LifecycleGateway:
    def __init__(self) -> None:
        self.claim_order: list[str] = []
        self.jobs = [Item("job")]
        self.events = [Item("outbox")]
        self.successes = 0; self.published = 0; self.failures = 0
        self.heartbeat_error: Exception | None = None

    async def claim_job(self, **_: object): self.claim_order.append("job"); return self.jobs.pop(0) if self.jobs else None
    async def claim_outbox(self, **_: object): self.claim_order.append("outbox"); return self.events.pop(0) if self.events else None
    async def succeed(self, *_: object, **__: object): self.successes += 1
    async def fail(self, *_: object, **__: object): self.failures += 1
    async def publish_outbox(self, *_: object, **__: object): self.published += 1
    async def fail_outbox(self, *_: object, **__: object): self.failures += 1
    async def heartbeat(self, *_: object, **__: object):
        if self.heartbeat_error:
            error, self.heartbeat_error = self.heartbeat_error, None
            raise error
    async def heartbeat_outbox(self, *_: object, **__: object):
        if self.heartbeat_error: raise self.heartbeat_error


def make_worker(gateway: LifecycleGateway, **overrides: object) -> Worker:
    values = dict(interval_seconds=0, queue=gateway, handlers={"test.work": lambda _: asyncio.sleep(0)}, outbox_handlers={"test.event": lambda _, __: asyncio.sleep(0)}, worker_id="w", lease_seconds=3, heartbeat_seconds=.01, shutdown_timeout_seconds=.01, cancel_timeout_seconds=.01)
    values.update(overrides)
    return Worker(Probe(), Probe(), **values)


def test_worker_alternates_job_and_outbox_and_passes_stable_idempotency_key() -> None:
    gateway = LifecycleGateway(); keys: list[uuid.UUID] = []
    expected_event_id = gateway.events[0].id
    async def event_handler(event: Item, idempotency_key: uuid.UUID) -> None: keys.append(idempotency_key)
    worker = make_worker(gateway, outbox_handlers={"test.event": event_handler})
    async def exercise() -> None:
        task = asyncio.create_task(worker.run())
        while gateway.successes + gateway.published < 2: await asyncio.sleep(0)
        worker.request_shutdown(); await task
    asyncio.run(exercise())
    assert gateway.claim_order[:2] == ["job", "outbox"]
    assert keys == [expected_event_id]


def test_worker_does_not_pause_while_claimed_work_remains() -> None:
    gateway = LifecycleGateway()
    gateway.events.clear()
    gateway.jobs.append(Item("job"))
    worker = make_worker(gateway, interval_seconds=60)
    successes_when_paused: list[int] = []

    async def stop_at_first_pause() -> None:
        successes_when_paused.append(gateway.successes)
        worker.request_shutdown()

    worker._pause = stop_at_first_pause
    asyncio.run(worker.run())
    assert successes_when_paused == [2]


def test_worker_contains_claim_and_completion_gateway_failures() -> None:
    class Failing(LifecycleGateway):
        async def claim_job(self, **_: object): self.claim_order.append("job"); raise RuntimeError("db down")
        async def succeed(self, *_: object, **__: object): raise RuntimeError("lease lost")
    gateway = Failing(); worker = make_worker(gateway)
    async def exercise() -> None:
        task = asyncio.create_task(worker.run())
        while len(gateway.claim_order) < 3: await asyncio.sleep(0)
        worker.request_shutdown(); await asyncio.wait_for(task, 1)
    asyncio.run(exercise())

    class CompletionFailing(LifecycleGateway):
        async def succeed(self, *_: object, **__: object): raise RuntimeError("lease lost")
    completion = CompletionFailing(); completion.events.clear(); completion.jobs.append(Item("job")); worker = make_worker(completion)
    async def complete_twice() -> None:
        task = asyncio.create_task(worker.run())
        while completion.claim_order.count("job") < 2: await asyncio.sleep(0)
        worker.request_shutdown(); await asyncio.wait_for(task, 1)
    asyncio.run(complete_twice())


def test_worker_rechecks_shutdown_before_claim() -> None:
    gateway = LifecycleGateway(); worker = make_worker(gateway); worker.request_shutdown()
    asyncio.run(worker.run())
    assert gateway.claim_order == []


def test_heartbeat_lease_loss_cancels_handler_and_prevents_completion() -> None:
    gateway = LifecycleGateway(); gateway.events.clear(); gateway.heartbeat_error = RuntimeError("lease lost")
    cancelled = asyncio.Event()
    calls = 0
    async def handler(_: Item) -> None:
        nonlocal calls; calls += 1
        if calls == 1:
            try: await asyncio.Event().wait()
            finally: cancelled.set()
    worker = make_worker(gateway, handlers={"test.work": handler})
    async def exercise() -> None:
        task = asyncio.create_task(worker.run())
        await cancelled.wait(); gateway.jobs.append(Item("job"))
        while gateway.successes < 1: await asyncio.sleep(0)
        worker.request_shutdown(); await asyncio.wait_for(task, 1)
    asyncio.run(exercise())
    assert gateway.successes == 1 and calls == 2


def test_stubborn_handler_invokes_hard_stop_after_two_bounded_deadlines() -> None:
    gateway = LifecycleGateway(); gateway.events.clear(); hard_stops: list[bool] = []; release = asyncio.Event()
    async def stubborn(_: Item) -> None:
        while True:
            try: await release.wait(); return
            except asyncio.CancelledError: continue
    def hard_stop() -> None:
        hard_stops.append(True); release.set()
    worker = make_worker(gateway, handlers={"test.work": stubborn}, hard_stop=hard_stop)
    async def exercise() -> None:
        task = asyncio.create_task(worker.run()); await asyncio.sleep(.01); worker.request_shutdown(); await asyncio.wait_for(task, .2)
    asyncio.run(exercise())
    assert hard_stops == [True]


def test_stubborn_handler_on_lease_loss_hard_stops_and_never_claims_again() -> None:
    gateway = LifecycleGateway(); gateway.events.clear(); gateway.heartbeat_error = RuntimeError("lease lost")
    release = asyncio.Event(); hard_stops: list[bool] = []
    async def stubborn(_: Item) -> None:
        while True:
            try: await release.wait(); return
            except asyncio.CancelledError: continue
    def hard_stop() -> None: hard_stops.append(True); release.set()
    worker = make_worker(gateway, handlers={"test.work": stubborn}, hard_stop=hard_stop)
    async def exercise() -> None:
        task = asyncio.create_task(worker.run()); await asyncio.wait_for(task, .2)
    asyncio.run(exercise())
    assert hard_stops == [True]
    assert gateway.claim_order.count("job") == 1
    assert gateway.successes == 0


@pytest.mark.parametrize("kind", ["job", "outbox"])
def test_shutdown_during_blocking_claim_does_not_start_handler(kind: str) -> None:
    started = asyncio.Event(); release = asyncio.Event(); handled: list[str] = []
    class BlockingGateway(LifecycleGateway):
        async def claim_job(self, **_: object):
            self.claim_order.append("job")
            if kind != "job": return None
            started.set(); await release.wait(); return Item("job")
        async def claim_outbox(self, **_: object):
            self.claim_order.append("outbox")
            if kind != "outbox": return None
            started.set(); await release.wait(); return Item("outbox")
    gateway = BlockingGateway()
    async def job_handler(_: Item) -> None: handled.append("job")
    async def outbox_handler(_: Item, __: uuid.UUID) -> None: handled.append("outbox")
    worker = make_worker(gateway, handlers={"test.work": job_handler}, outbox_handlers={"test.event": outbox_handler}, shutdown_timeout_seconds=.1)
    async def exercise() -> None:
        task = asyncio.create_task(worker.run()); await started.wait(); worker.request_shutdown(); release.set(); await asyncio.wait_for(task, 1)
    asyncio.run(exercise())
    assert handled == []
    assert gateway.successes == 0 and gateway.published == 0


def test_structured_lifecycle_logs_include_safe_type_and_never_payload(caplog: pytest.LogCaptureFixture) -> None:
    gateway = LifecycleGateway(); gateway.events.clear()
    item = gateway.jobs[0]; item.payload = {"resume_text": "must-not-log"}
    worker = make_worker(gateway)
    async def exercise() -> None:
        task = asyncio.create_task(worker.run())
        while gateway.successes < 1: await asyncio.sleep(0)
        worker.request_shutdown(); await task
    with caplog.at_level(logging.INFO): asyncio.run(exercise())
    records = [record for record in caplog.records if record.getMessage() == "worker_item_succeeded"]
    assert len(records) == 1
    assert records[0].context == {"item_id": str(item.id), "item_type": "test.work", "kind": "job", "attempt": 1, "trace_id": "trace-1", "safe_error_code": "succeeded"}
    assert "must-not-log" not in caplog.text and "payload" not in caplog.text
