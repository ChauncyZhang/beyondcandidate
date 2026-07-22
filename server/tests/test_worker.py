import asyncio

import pytest

from server.app.worker import main as worker_main
from server.app.worker.main import Worker


class CountingProbe:
    def __init__(self) -> None:
        self.calls = 0

    async def check(self) -> None:
        self.calls += 1


class FlakyProbe(CountingProbe):
    async def check(self) -> None:
        await super().check()
        if self.calls == 1:
            raise RuntimeError("dependency unavailable")


class HangingProbe(CountingProbe):
    async def check(self) -> None:
        await super().check()
        await asyncio.Event().wait()


def test_worker_entrypoint_configures_structured_logging(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    monkeypatch.setattr(worker_main, "configure_logging", lambda: calls.append("logging"))

    def fake_run(coroutine: object) -> None:
        calls.append("run")
        coroutine.close()  # type: ignore[attr-defined]

    monkeypatch.setattr(worker_main.asyncio, "run", fake_run)

    worker_main.main()

    assert calls == ["logging", "run"]


def test_governance_worker_job_types_are_explicit_and_complete() -> None:
    assert worker_main.GOVERNANCE_JOB_TYPES == {
        "governance.delete_candidate",
        "governance.retention_sweep",
        "governance.redelete_after_restore",
    }


def test_worker_stops_after_shutdown_request() -> None:
    database = CountingProbe()
    storage = CountingProbe()
    worker = Worker(database, storage, interval_seconds=0)

    async def exercise() -> None:
        task = asyncio.create_task(worker.run())
        await asyncio.sleep(0)
        worker.request_shutdown()
        await asyncio.wait_for(task, timeout=1)

    asyncio.run(exercise())

    assert database.calls >= 1
    assert storage.calls >= 1


def test_worker_keeps_running_when_a_readiness_check_fails() -> None:
    database = FlakyProbe()
    storage = CountingProbe()
    worker = Worker(database, storage, interval_seconds=0)

    async def exercise() -> None:
        task = asyncio.create_task(worker.run())
        while database.calls < 2:
            await asyncio.sleep(0)
        worker.request_shutdown()
        await asyncio.wait_for(task, timeout=1)

    asyncio.run(exercise())

    assert database.calls >= 2
    assert storage.calls >= 1


def test_worker_shutdown_is_bounded_when_probe_hangs() -> None:
    database = HangingProbe()
    storage = CountingProbe()
    worker = Worker(
        database,
        storage,
        interval_seconds=30,
        readiness_timeout_seconds=0.01,
    )

    async def exercise() -> None:
        task = asyncio.create_task(worker.run())
        while database.calls == 0:
            await asyncio.sleep(0)
        worker.request_shutdown()
        await asyncio.wait_for(task, timeout=0.1)

    asyncio.run(exercise())
