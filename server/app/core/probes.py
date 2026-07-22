import asyncio
from typing import Protocol


class ReadinessProbe(Protocol):
    async def check(self) -> None: ...


async def check_readiness(*probes: ReadinessProbe) -> None:
    async with asyncio.TaskGroup() as group:
        for probe in probes:
            group.create_task(probe.check())
