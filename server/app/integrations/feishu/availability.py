from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from queue import Empty, Queue
from threading import BoundedSemaphore, Event, Thread
from time import perf_counter
from typing import Literal, cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from server.app.integrations.feishu.models import FeishuIdentityBinding, FeishuOrganizationConfig
from server.app.integrations.feishu.provider import (
    BusyWindow,
    FeishuCredentials,
    FeishuProvider,
    FeishuProviderError,
    chunk_freebusy_requests,
)
from server.app.integrations.feishu.service import FeishuSecretCipher
from server.app.interviews.availability import AvailabilityProvider


logger = logging.getLogger(__name__)


class FeishuAwareAvailabilityProvider:
    """Combines ATS interview blocks with Feishu busy windows without event details."""

    def __init__(
        self,
        internal_provider: AvailabilityProvider,
        feishu_provider: FeishuProvider,
        cipher: FeishuSecretCipher,
        *,
        external_timeout_seconds: float = 5.0,
        external_workers: int = 4,
    ) -> None:
        if external_timeout_seconds <= 0:
            raise ValueError("external timeout must be positive")
        if external_workers <= 0:
            raise ValueError("external workers must be positive")
        self._internal_provider = internal_provider
        self._feishu_provider = feishu_provider
        self._cipher = cipher
        self._external_timeout_seconds = external_timeout_seconds
        self._external_slots = BoundedSemaphore(external_workers)

    def _external_busy_windows(
        self,
        credentials: FeishuCredentials,
        open_ids: list[str],
        query_start: datetime,
        query_end: datetime,
    ) -> list[BusyWindow]:
        if not self._external_slots.acquire(blocking=False):
            raise FeishuProviderError("feishu_availability_busy")

        provider_requests = chunk_freebusy_requests(open_ids, query_start, query_end)
        started = perf_counter()
        result: Queue[tuple[Literal["ok", "error"], object]] = Queue(maxsize=1)
        cancelled = Event()

        def query() -> None:
            try:
                windows: list[BusyWindow] = []
                for provider_request in provider_requests:
                    if cancelled.is_set():
                        return
                    windows.extend(self._feishu_provider.batch_freebusy(credentials, provider_request))
                if not cancelled.is_set():
                    result.put(("ok", windows))
            except Exception as error:
                if not cancelled.is_set():
                    result.put(("error", error))
            finally:
                self._external_slots.release()

        thread = Thread(target=query, name="feishu-availability", daemon=True)
        try:
            thread.start()
        except RuntimeError:
            self._external_slots.release()
            raise FeishuProviderError("feishu_availability_unavailable") from None
        try:
            status, payload = result.get(timeout=self._external_timeout_seconds)
        except Empty:
            cancelled.set()
            raise FeishuProviderError("feishu_availability_timeout") from None
        if status == "error":
            if isinstance(payload, FeishuProviderError):
                raise payload
            raise FeishuProviderError("feishu_availability_unavailable") from None
        logger.info(
            "feishu_availability_query_complete",
            extra={"context": {
                "duration_ms": round((perf_counter() - started) * 1000),
                "participant_count": len(open_ids),
                "batch_count": len(provider_requests),
            }},
        )
        return cast(list[BusyWindow], payload)

    def availability(
        self,
        *,
        db: Session,
        organization_id: UUID,
        participant_ids: list[UUID],
        starts_at: datetime,
        ends_at: datetime,
        buffer_minutes: int,
        exclude_interview_id: UUID | None,
    ) -> list[dict]:
        internal_rows = self._internal_provider.availability(
            db=db,
            organization_id=organization_id,
            participant_ids=participant_ids,
            starts_at=starts_at,
            ends_at=ends_at,
            buffer_minutes=buffer_minutes,
            exclude_interview_id=exclude_interview_id,
        )
        config = db.scalar(
            select(FeishuOrganizationConfig).where(
                FeishuOrganizationConfig.organization_id == organization_id
            )
        )
        if config is None or not config.enabled:
            return internal_rows

        bindings = db.scalars(
            select(FeishuIdentityBinding).where(
                FeishuIdentityBinding.organization_id == organization_id,
                FeishuIdentityBinding.user_id.in_(participant_ids),
            )
        ).all()
        open_id_by_user = {binding.user_id: binding.open_id for binding in bindings if binding.open_id}
        external_busy: dict[str, list[dict]] = defaultdict(list)
        external_available = True
        excluded_window: tuple[datetime, datetime] | None = None
        if exclude_interview_id is not None:
            from server.app.interviews.models import Interview

            existing_window = db.execute(
                select(Interview.starts_at, Interview.ends_at).where(
                    Interview.organization_id == organization_id,
                    Interview.id == exclude_interview_id,
                )
            ).one_or_none()
            if existing_window is not None:
                excluded_window = tuple(
                    value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
                    for value in existing_window
                )
        open_ids = list(dict.fromkeys(open_id_by_user.values()))
        if open_ids:
            credentials = FeishuCredentials(
                config.app_id,
                self._cipher.decrypt(config.encrypted_app_secret),
                config.redirect_uri,
                config.calendar_id,
            )
            external_started = perf_counter()
            try:
                query_start = starts_at - timedelta(minutes=buffer_minutes)
                query_end = ends_at + timedelta(minutes=buffer_minutes)
                windows = self._external_busy_windows(credentials, open_ids, query_start, query_end)
                for window in windows:
                    window_start = window.starts_at if window.starts_at.tzinfo is not None else window.starts_at.replace(tzinfo=timezone.utc)
                    window_end = window.ends_at if window.ends_at.tzinfo is not None else window.ends_at.replace(tzinfo=timezone.utc)
                    if excluded_window == (window_start, window_end):
                        continue
                    external_busy[window.user_id].append(
                        {"starts_at": window.starts_at.isoformat(), "ends_at": window.ends_at.isoformat()}
                    )
            except FeishuProviderError as error:
                logger.warning(
                    "feishu_availability_degraded",
                    extra={
                        "context": {
                            "safe_code": error.safe_code,
                            "participant_count": len(open_ids),
                            "duration_ms": round((perf_counter() - external_started) * 1000),
                        }
                    },
                )
                external_available = False

        internal_by_user = {row["participant_id"]: row.get("busy", []) for row in internal_rows}
        rows: list[dict] = []
        for participant_id in participant_ids:
            open_id = open_id_by_user.get(participant_id)
            internal_busy = internal_by_user.get(str(participant_id), [])
            if not open_id or not external_available:
                rows.append({"participant_id": str(participant_id), "status": "unknown", "busy": internal_busy})
                continue
            combined = [*internal_busy, *external_busy.get(open_id, [])]
            busy = list({(block["starts_at"], block["ends_at"]): block for block in combined}.values())
            busy.sort(key=lambda block: (block["starts_at"], block["ends_at"]))
            rows.append({"participant_id": str(participant_id), "status": "confirmed", "busy": busy})
        return rows
