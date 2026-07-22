from collections.abc import Awaitable, Callable, Mapping
from server.app.queue.service import PermanentJobError, RetryableJobError


class OutboxDispatcher:
    """At-least-once dispatcher; handlers receive event UUID as idempotency key."""
    def __init__(self, gateway: object, handlers: Mapping[str, Callable[[object, object], Awaitable[None]]], worker_id: str) -> None:
        self._gateway = gateway
        self._handlers = dict(handlers)
        self._worker_id = worker_id

    async def dispatch(self, event: object) -> None:
        handler = self._handlers.get(event.topic)
        if handler is None:
            await self._gateway.fail_outbox(event, self._worker_id, safe_code="unknown_outbox_topic", retryable=False)
            return
        try:
            await handler(event, event.id)
        except RetryableJobError as error:
            await self._gateway.fail_outbox(event, self._worker_id, safe_code=error.safe_code, retryable=True)
        except PermanentJobError as error:
            await self._gateway.fail_outbox(event, self._worker_id, safe_code=error.safe_code, retryable=False)
        except Exception:
            await self._gateway.fail_outbox(event, self._worker_id, safe_code="outbox_handler_failed", retryable=True)
        else:
            await self._gateway.publish_outbox(event, self._worker_id)
