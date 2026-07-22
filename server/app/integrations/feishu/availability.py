from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from server.app.integrations.feishu.models import FeishuIdentityBinding, FeishuOrganizationConfig
from server.app.integrations.feishu.provider import (
    FeishuCredentials,
    FeishuProvider,
    FeishuProviderError,
    chunk_freebusy_requests,
)
from server.app.integrations.feishu.service import FeishuSecretCipher
from server.app.interviews.availability import AvailabilityProvider


class FeishuAwareAvailabilityProvider:
    """Combines ATS interview blocks with Feishu busy windows without event details."""

    def __init__(
        self,
        internal_provider: AvailabilityProvider,
        feishu_provider: FeishuProvider,
        cipher: FeishuSecretCipher,
    ) -> None:
        self._internal_provider = internal_provider
        self._feishu_provider = feishu_provider
        self._cipher = cipher

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
            try:
                query_start = starts_at - timedelta(minutes=buffer_minutes)
                query_end = ends_at + timedelta(minutes=buffer_minutes)
                for provider_request in chunk_freebusy_requests(open_ids, query_start, query_end):
                    for window in self._feishu_provider.batch_freebusy(credentials, provider_request):
                        window_start = window.starts_at if window.starts_at.tzinfo is not None else window.starts_at.replace(tzinfo=timezone.utc)
                        window_end = window.ends_at if window.ends_at.tzinfo is not None else window.ends_at.replace(tzinfo=timezone.utc)
                        if excluded_window == (window_start, window_end):
                            continue
                        external_busy[window.user_id].append(
                            {"starts_at": window.starts_at.isoformat(), "ends_at": window.ends_at.isoformat()}
                        )
            except FeishuProviderError:
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
