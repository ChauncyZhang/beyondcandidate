from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Protocol
from urllib.parse import urlencode
from uuid import UUID, uuid4

import httpx


AUTHORIZE_URL = "https://accounts.feishu.cn/open-apis/authen/v1/authorize"
OPEN_API_BASE = "https://open.feishu.cn/open-apis"
MAX_FREEBUSY_USERS = 10
MAX_FREEBUSY_RANGE = timedelta(days=14)


class FeishuProviderError(RuntimeError):
    def __init__(self, safe_code: str = "feishu_unavailable", *, retryable: bool = True):
        self.safe_code = safe_code
        self.retryable = retryable
        super().__init__(safe_code)


@dataclass(frozen=True)
class FeishuCredentials:
    app_id: str
    app_secret: str
    redirect_uri: str
    calendar_id: str = "primary"


@dataclass(frozen=True)
class OAuthIdentity:
    union_id: str | None
    open_id: str | None
    email: str | None
    tenant_key: str | None


@dataclass(frozen=True)
class ConnectionResult:
    ok: bool
    latency_ms: int = 0
    safe_error_code: str | None = None


@dataclass(frozen=True)
class FreeBusyRequest:
    user_ids: tuple[str, ...]
    time_min: datetime
    time_max: datetime


@dataclass(frozen=True)
class BusyWindow:
    user_id: str
    starts_at: datetime
    ends_at: datetime


@dataclass(frozen=True)
class CalendarEventRequest:
    interview_id: UUID
    summary: str
    starts_at: datetime
    ends_at: datetime
    timezone: str
    description: str
    location: str
    attendee_open_ids: tuple[str, ...]
    attendee_emails: tuple[str, ...]


@dataclass(frozen=True)
class CalendarEvent:
    event_id: str
    attendee_open_ids: tuple[str, ...]
    attendee_emails: tuple[str, ...]
    cancelled: bool = False


def chunk_freebusy_requests(
    user_ids: list[str] | tuple[str, ...], time_min: datetime, time_max: datetime
) -> list[FreeBusyRequest]:
    if not user_ids or time_min >= time_max:
        raise ValueError("freebusy requires users and a positive time range")
    chunks: list[FreeBusyRequest] = []
    range_start = time_min
    while range_start < time_max:
        range_end = min(range_start + MAX_FREEBUSY_RANGE, time_max)
        for offset in range(0, len(user_ids), MAX_FREEBUSY_USERS):
            chunks.append(
                FreeBusyRequest(tuple(user_ids[offset : offset + MAX_FREEBUSY_USERS]), range_start, range_end)
            )
        range_start = range_end
    return chunks


class FeishuProvider(Protocol):
    def authorization_url(self, credentials: FeishuCredentials, state: str) -> str: ...
    def test_connection(self, credentials: FeishuCredentials) -> ConnectionResult: ...
    def exchange_code(self, credentials: FeishuCredentials, code: str) -> OAuthIdentity: ...
    def batch_freebusy(self, credentials: FeishuCredentials, request: FreeBusyRequest) -> tuple[BusyWindow, ...]: ...
    def create_event(self, credentials: FeishuCredentials, request: CalendarEventRequest, *, idempotency_key: str) -> CalendarEvent: ...
    def update_event(self, credentials: FeishuCredentials, event_id: str, request: CalendarEventRequest, *, idempotency_key: str) -> CalendarEvent: ...
    def cancel_event(self, credentials: FeishuCredentials, event_id: str, *, idempotency_key: str) -> None: ...


class FakeFeishuProvider:
    def __init__(self, *, identity: OAuthIdentity | None = None) -> None:
        self.identity = identity or OAuthIdentity("on_fake", "ou_fake", None, "tenant_fake")
        self.events: dict[str, CalendarEvent] = {}
        self._idempotency: dict[str, object] = {}
        self.exchanged_codes: list[str] = []
        self.busy_windows: tuple[BusyWindow, ...] = ()
        self.freebusy_requests: list[FreeBusyRequest] = []
        self.failure: FeishuProviderError | None = None

    def _check(self) -> None:
        if self.failure:
            raise self.failure

    def authorization_url(self, credentials: FeishuCredentials, state: str) -> str:
        return f"{AUTHORIZE_URL}?{urlencode({'client_id': credentials.app_id, 'response_type': 'code', 'redirect_uri': credentials.redirect_uri, 'state': state})}"

    def test_connection(self, credentials: FeishuCredentials) -> ConnectionResult:
        self._check()
        return ConnectionResult(True, 1)

    def exchange_code(self, credentials: FeishuCredentials, code: str) -> OAuthIdentity:
        self._check()
        self.exchanged_codes.append(code)
        return self.identity

    def batch_freebusy(self, credentials: FeishuCredentials, request: FreeBusyRequest) -> tuple[BusyWindow, ...]:
        self._check()
        if len(request.user_ids) > MAX_FREEBUSY_USERS or request.time_max - request.time_min > MAX_FREEBUSY_RANGE:
            raise ValueError("freebusy provider request exceeds Feishu limits")
        self.freebusy_requests.append(request)
        return tuple(window for window in self.busy_windows if window.user_id in request.user_ids)

    def create_event(self, credentials: FeishuCredentials, request: CalendarEventRequest, *, idempotency_key: str) -> CalendarEvent:
        self._check()
        if idempotency_key in self._idempotency:
            return self._idempotency[idempotency_key]  # type: ignore[return-value]
        event = CalendarEvent(
            f"evt_{uuid4().hex}",
            request.attendee_open_ids,
            request.attendee_emails,
        )
        self.events[event.event_id] = event
        self._idempotency[idempotency_key] = event
        return event

    def update_event(self, credentials: FeishuCredentials, event_id: str, request: CalendarEventRequest, *, idempotency_key: str) -> CalendarEvent:
        self._check()
        if idempotency_key in self._idempotency:
            return self._idempotency[idempotency_key]  # type: ignore[return-value]
        if event_id not in self.events:
            raise FeishuProviderError("feishu_event_not_found", retryable=False)
        event = CalendarEvent(
            event_id,
            request.attendee_open_ids,
            request.attendee_emails,
        )
        self.events[event_id] = event
        self._idempotency[idempotency_key] = event
        return event

    def cancel_event(self, credentials: FeishuCredentials, event_id: str, *, idempotency_key: str) -> None:
        self._check()
        if idempotency_key in self._idempotency:
            return
        if event_id not in self.events:
            raise FeishuProviderError("feishu_event_not_found", retryable=False)
        self.events[event_id] = replace(self.events[event_id], cancelled=True)
        self._idempotency[idempotency_key] = True


class HttpFeishuProvider:
    """Small synchronous adapter; callers run it outside database transactions."""

    def __init__(self, client: httpx.Client | None = None, *, timeout_seconds: float = 10) -> None:
        self._client = client or httpx.Client(timeout=timeout_seconds, follow_redirects=False)

    def authorization_url(self, credentials: FeishuCredentials, state: str) -> str:
        return f"{AUTHORIZE_URL}?{urlencode({'client_id': credentials.app_id, 'response_type': 'code', 'redirect_uri': credentials.redirect_uri, 'state': state})}"

    def _json(self, method: str, url: str, **kwargs) -> dict:
        try:
            response = self._client.request(method, url, **kwargs)
            response.raise_for_status()
            payload = response.json()
        except (httpx.TimeoutException, httpx.NetworkError):
            raise FeishuProviderError() from None
        except (httpx.HTTPStatusError, ValueError, TypeError):
            raise FeishuProviderError("feishu_response_invalid", retryable=False) from None
        if not isinstance(payload, dict) or payload.get("code", 0) != 0:
            code = payload.get("code") if isinstance(payload, dict) else None
            retryable = isinstance(code, int) and (code >= 50000 or code in {20007, 20050})
            raise FeishuProviderError("feishu_request_failed", retryable=retryable)
        return payload

    def _tenant_token(self, credentials: FeishuCredentials) -> str:
        payload = self._json(
            "POST",
            f"{OPEN_API_BASE}/auth/v3/tenant_access_token/internal",
            json={"app_id": credentials.app_id, "app_secret": credentials.app_secret},
        )
        token = payload.get("tenant_access_token")
        if not isinstance(token, str) or not token:
            raise FeishuProviderError("feishu_response_invalid", retryable=False)
        return token

    def test_connection(self, credentials: FeishuCredentials) -> ConnectionResult:
        from time import perf_counter
        started = perf_counter()
        try:
            self._tenant_token(credentials)
        except FeishuProviderError as error:
            return ConnectionResult(False, int((perf_counter() - started) * 1000), error.safe_code)
        return ConnectionResult(True, int((perf_counter() - started) * 1000))

    def exchange_code(self, credentials: FeishuCredentials, code: str) -> OAuthIdentity:
        token_payload = self._json(
            "POST",
            f"{OPEN_API_BASE}/authen/v2/oauth/token",
            json={"grant_type": "authorization_code", "client_id": credentials.app_id, "client_secret": credentials.app_secret, "code": code, "redirect_uri": credentials.redirect_uri},
        )
        access_token = token_payload.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            raise FeishuProviderError("feishu_response_invalid", retryable=False)
        info = self._json("GET", f"{OPEN_API_BASE}/authen/v1/user_info", headers={"Authorization": f"Bearer {access_token}"})
        data = info.get("data")
        if not isinstance(data, dict):
            raise FeishuProviderError("feishu_response_invalid", retryable=False)
        return OAuthIdentity(data.get("union_id"), data.get("open_id"), data.get("enterprise_email") or data.get("email"), data.get("tenant_key"))

    def _headers(self, credentials: FeishuCredentials) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._tenant_token(credentials)}", "Content-Type": "application/json; charset=utf-8"}

    @staticmethod
    def _event_body(request: CalendarEventRequest) -> dict:
        return {
            "summary": request.summary[:255],
            "description": request.description,
            "start_time": {"timestamp": str(int(request.starts_at.timestamp())), "timezone": request.timezone},
            "end_time": {"timestamp": str(int(request.ends_at.timestamp())), "timezone": request.timezone},
            "location": {"name": request.location[:512]},
        }

    def _add_attendees(
        self,
        credentials: FeishuCredentials,
        event_id: str,
        open_ids: tuple[str, ...],
        emails: tuple[str, ...],
    ) -> None:
        attendees = [
            {"type": "user", "user_id": open_id}
            for open_id in open_ids
        ]
        attendees.extend(
            {"type": "third_party", "third_party_email": email}
            for email in emails
        )
        if not attendees:
            return
        self._json(
            "POST",
            f"{OPEN_API_BASE}/calendar/v4/calendars/{credentials.calendar_id}/events/{event_id}/attendees",
            params={"user_id_type": "open_id"},
            headers=self._headers(credentials),
            json={"attendees": attendees, "need_notification": True},
        )

    def batch_freebusy(self, credentials: FeishuCredentials, request: FreeBusyRequest) -> tuple[BusyWindow, ...]:
        if len(request.user_ids) > MAX_FREEBUSY_USERS or request.time_max - request.time_min > MAX_FREEBUSY_RANGE:
            raise ValueError("freebusy provider request exceeds Feishu limits")
        payload = self._json(
            "POST",
            f"{OPEN_API_BASE}/calendar/v4/freebusy/batch?user_id_type=open_id",
            headers=self._headers(credentials),
            json={
                "time_min": request.time_min.isoformat(),
                "time_max": request.time_max.isoformat(),
                "user_ids": list(request.user_ids),
                "include_external_calendar": True,
                "only_busy": True,
                "need_rsvp_status": True,
            },
        )
        data = payload.get("data")
        if not isinstance(data, dict):
            raise FeishuProviderError("feishu_response_invalid", retryable=False)
        if not data:
            return ()
        if "freebusy_lists" in data:
            rows = data["freebusy_lists"]
            items_key = "freebusy_items"
        elif "freebusy_list" in data:
            rows = data["freebusy_list"]
            items_key = "freebusy"
        else:
            raise FeishuProviderError("feishu_response_invalid", retryable=False)
        if not isinstance(rows, list):
            raise FeishuProviderError("feishu_response_invalid", retryable=False)
        windows: list[BusyWindow] = []
        try:
            for row in rows:
                if not isinstance(row, dict) or not isinstance(row.get("user_id"), str):
                    raise ValueError
                items = row.get(items_key)
                if not isinstance(items, list):
                    raise ValueError
                for item in items:
                    if not isinstance(item, dict):
                        raise ValueError
                    if item.get("rsvp_status") in {"decline", "declined"}:
                        continue
                    starts_at = datetime.fromisoformat(str(item["start_time"]).replace("Z", "+00:00"))
                    ends_at = datetime.fromisoformat(str(item["end_time"]).replace("Z", "+00:00"))
                    if starts_at.tzinfo is None or ends_at.tzinfo is None or ends_at <= starts_at:
                        raise ValueError
                    windows.append(BusyWindow(row["user_id"], starts_at, ends_at))
        except (KeyError, TypeError, ValueError):
            raise FeishuProviderError("feishu_response_invalid", retryable=False) from None
        return tuple(windows)

    def create_event(self, credentials: FeishuCredentials, request: CalendarEventRequest, *, idempotency_key: str) -> CalendarEvent:
        payload = self._json("POST", f"{OPEN_API_BASE}/calendar/v4/calendars/{credentials.calendar_id}/events", params={"idempotency_key": idempotency_key}, headers=self._headers(credentials), json=self._event_body(request))
        event_id = payload.get("data", {}).get("event", {}).get("event_id")
        if not isinstance(event_id, str) or not event_id:
            raise FeishuProviderError("feishu_response_invalid", retryable=False)
        self._add_attendees(
            credentials,
            event_id,
            request.attendee_open_ids,
            request.attendee_emails,
        )
        return CalendarEvent(
            event_id,
            request.attendee_open_ids,
            request.attendee_emails,
        )

    def update_event(self, credentials: FeishuCredentials, event_id: str, request: CalendarEventRequest, *, idempotency_key: str) -> CalendarEvent:
        self._json("PATCH", f"{OPEN_API_BASE}/calendar/v4/calendars/{credentials.calendar_id}/events/{event_id}", headers=self._headers(credentials), json=self._event_body(request))
        # Attendee reconciliation is deliberately additive in the skeleton; ATS remains authoritative and retries are idempotent at the outbox boundary.
        self._add_attendees(
            credentials,
            event_id,
            request.attendee_open_ids,
            request.attendee_emails,
        )
        return CalendarEvent(
            event_id,
            request.attendee_open_ids,
            request.attendee_emails,
        )

    def cancel_event(self, credentials: FeishuCredentials, event_id: str, *, idempotency_key: str) -> None:
        self._json("DELETE", f"{OPEN_API_BASE}/calendar/v4/calendars/{credentials.calendar_id}/events/{event_id}", headers=self._headers(credentials))
