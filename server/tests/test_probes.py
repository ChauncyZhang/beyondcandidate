import asyncio

import pytest

from server.app.core.probes import check_readiness


class FailingProbe:
    async def check(self) -> None:
        raise RuntimeError("unavailable")


class CancellableProbe:
    def __init__(self) -> None:
        self.cancelled = False

    async def check(self) -> None:
        try:
            await asyncio.Event().wait()
        finally:
            self.cancelled = True


def test_failing_probe_cancels_sibling() -> None:
    sibling = CancellableProbe()

    async def exercise() -> None:
        with pytest.raises(ExceptionGroup):
            await check_readiness(FailingProbe(), sibling)

    asyncio.run(exercise())

    assert sibling.cancelled is True
