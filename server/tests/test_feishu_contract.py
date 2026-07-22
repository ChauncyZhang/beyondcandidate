from datetime import datetime, timedelta, timezone
import json
from uuid import uuid4

import httpx
import pytest

from server.app.integrations.feishu.provider import (
    CalendarEvent,
    CalendarEventRequest,
    FakeFeishuProvider,
    FeishuCredentials,
    FreeBusyRequest,
    HttpFeishuProvider,
    OAuthIdentity,
    chunk_freebusy_requests,
)
from server.app.integrations.feishu.service import public_config, stable_identity_key


def test_public_config_never_serializes_secret_material() -> None:
    class Config:
        app_id = "cli_test"
        redirect_uri = "https://hr.example.test/api/v1/auth/feishu/callback"
        calendar_id = "primary"
        enabled = False
        encrypted_app_secret = b"encrypted-secret"
        encrypted_verification_token = b"encrypted-token"
        encrypted_encrypt_key = b"encrypted-key"
        version = 3
        last_test_status = "failed"
        last_tested_at = None
        last_test_error_code = "feishu_unavailable"

    view = public_config(Config())

    assert view == {
        "app_id": "cli_test",
        "redirect_uri": "https://hr.example.test/api/v1/auth/feishu/callback",
        "calendar_id": "primary",
        "enabled": False,
        "app_secret_configured": True,
        "verification_token_configured": True,
        "encrypt_key_configured": True,
        "version": 3,
        "last_test_status": "failed",
        "last_tested_at": None,
        "last_test_error_code": "feishu_unavailable",
    }
    assert "secret" not in repr(view).lower().replace("app_secret_configured", "")
    assert b"encrypted" not in repr(view).encode()


def test_stable_identity_prefers_union_id_and_requires_provider_id() -> None:
    assert stable_identity_key(OAuthIdentity("on_123", "ou_123", None, "tenant")) == (
        "union_id",
        "on_123",
    )
    assert stable_identity_key(OAuthIdentity(None, "ou_123", None, "tenant")) == (
        "open_id",
        "ou_123",
    )
    with pytest.raises(ValueError, match="stable identity"):
        stable_identity_key(OAuthIdentity(None, None, None, "tenant"))


def test_freebusy_is_split_by_ten_users_and_fourteen_days() -> None:
    start = datetime(2026, 7, 1, tzinfo=timezone.utc)
    requests = chunk_freebusy_requests(
        [f"ou_{index}" for index in range(21)], start, start + timedelta(days=29)
    )

    assert len(requests) == 9
    assert {len(item.user_ids) for item in requests} == {1, 10}
    assert all(item.time_max - item.time_min <= timedelta(days=14) for item in requests)
    assert requests[-1].time_max == start + timedelta(days=29)


def test_fake_provider_contract_is_idempotent_and_records_attendees() -> None:
    provider = FakeFeishuProvider(
        identity=OAuthIdentity("on_123", "ou_123", "invited@example.test", "tenant")
    )
    credentials = FeishuCredentials(
        app_id="cli_test",
        app_secret="app-secret",
        redirect_uri="https://hr.example.test/callback",
        calendar_id="primary",
    )
    request = CalendarEventRequest(
        interview_id=uuid4(),
        summary="Backend interview",
        starts_at=datetime(2026, 7, 20, 1, tzinfo=timezone.utc),
        ends_at=datetime(2026, 7, 20, 2, tzinfo=timezone.utc),
        timezone="Asia/Shanghai",
        description="Interview",
        location="Room 1",
        attendee_open_ids=("ou_one",),
        attendee_emails=("one@example.test", "two@example.test"),
    )

    first = provider.create_event(credentials, request, idempotency_key="event-key")
    second = provider.create_event(credentials, request, idempotency_key="event-key")
    assert first == second
    assert first.attendee_open_ids == request.attendee_open_ids
    assert first.attendee_emails == request.attendee_emails

    updated = provider.update_event(
        credentials,
        first.event_id,
        request,
        idempotency_key="update-key",
    )
    assert isinstance(updated, CalendarEvent)
    provider.cancel_event(credentials, first.event_id, idempotency_key="cancel-key")
    provider.cancel_event(credentials, first.event_id, idempotency_key="cancel-key")
    assert provider.events[first.event_id].cancelled is True


def test_http_provider_adds_bound_users_as_internal_attendees() -> None:
    attendee_requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/tenant_access_token/internal"):
            return httpx.Response(200, json={"code": 0, "tenant_access_token": "tenant-token"})
        if request.url.path.endswith("/events"):
            return httpx.Response(200, json={"code": 0, "data": {"event": {"event_id": "evt_1"}}})
        if request.url.path.endswith("/attendees"):
            attendee_requests.append(request)
            return httpx.Response(200, json={"code": 0, "data": {"attendees": []}})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    provider = HttpFeishuProvider(httpx.Client(transport=httpx.MockTransport(handler)))
    request = CalendarEventRequest(
        interview_id=uuid4(),
        summary="Backend interview",
        starts_at=datetime(2026, 7, 20, 1, tzinfo=timezone.utc),
        ends_at=datetime(2026, 7, 20, 2, tzinfo=timezone.utc),
        timezone="Asia/Shanghai",
        description="Interview",
        location="Room 1",
        attendee_open_ids=("ou_bound",),
        attendee_emails=("external@example.test",),
    )

    provider.create_event(
        FeishuCredentials("cli", "secret", "https://example.test/callback"),
        request,
        idempotency_key="event-key",
    )

    assert len(attendee_requests) == 1
    attendee_request = attendee_requests[0]
    assert attendee_request.url.params["user_id_type"] == "open_id"
    assert json.loads(attendee_request.content) == {
        "attendees": [
            {"type": "user", "user_id": "ou_bound"},
            {"type": "third_party", "third_party_email": "external@example.test"},
        ],
        "need_notification": True,
    }


def test_http_provider_parses_current_batch_freebusy_response_shape() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/tenant_access_token/internal"):
            return httpx.Response(200, json={"code": 0, "tenant_access_token": "tenant-token"})
        if request.url.path.endswith("/freebusy/batch"):
            requests.append(request)
            return httpx.Response(200, json={
                "code": 0,
                "data": {
                    "freebusy_lists": [{
                        "user_id": "ou_interviewer",
                        "freebusy_items": [
                            {
                                "start_time": "2026-07-21T06:30:00Z",
                                "end_time": "2026-07-21T07:30:00Z",
                                "rsvp_status": "accept",
                            },
                            {
                                "start_time": "2026-07-21T08:00:00Z",
                                "end_time": "2026-07-21T08:30:00Z",
                                "rsvp_status": "decline",
                            },
                        ],
                    }],
                },
            })
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    provider = HttpFeishuProvider(httpx.Client(transport=httpx.MockTransport(handler)))
    start = datetime(2026, 7, 21, 6, tzinfo=timezone.utc)
    windows = provider.batch_freebusy(
        FeishuCredentials("cli", "secret", "https://example.test/callback"),
        FreeBusyRequest(("ou_interviewer",), start, start + timedelta(hours=3)),
    )

    assert [(window.user_id, window.starts_at, window.ends_at) for window in windows] == [(
        "ou_interviewer",
        datetime(2026, 7, 21, 6, 30, tzinfo=timezone.utc),
        datetime(2026, 7, 21, 7, 30, tzinfo=timezone.utc),
    )]
    assert json.loads(requests[0].content) == {
        "time_min": "2026-07-21T06:00:00+00:00",
        "time_max": "2026-07-21T09:00:00+00:00",
        "user_ids": ["ou_interviewer"],
        "include_external_calendar": True,
        "only_busy": True,
        "need_rsvp_status": True,
    }


def test_http_provider_accepts_successful_empty_next_week_freebusy_data() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/tenant_access_token/internal"):
            return httpx.Response(200, json={"code": 0, "tenant_access_token": "tenant-token"})
        if request.url.path.endswith("/freebusy/batch"):
            requests.append(request)
            return httpx.Response(200, json={"code": 0, "data": {}, "msg": "success"})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    provider = HttpFeishuProvider(httpx.Client(transport=httpx.MockTransport(handler)))
    starts_at = datetime.fromisoformat("2026-07-27T00:00:00+08:00")
    ends_at = datetime.fromisoformat("2026-08-02T23:59:59+08:00")

    windows = provider.batch_freebusy(
        FeishuCredentials("cli", "secret", "https://example.test/callback"),
        FreeBusyRequest(("ou_bound",), starts_at, ends_at),
    )

    assert windows == ()
    assert json.loads(requests[0].content) == {
        "time_min": "2026-07-27T00:00:00+08:00",
        "time_max": "2026-08-02T23:59:59+08:00",
        "user_ids": ["ou_bound"],
        "include_external_calendar": True,
        "only_busy": True,
        "need_rsvp_status": True,
    }


def test_http_provider_keeps_legacy_batch_freebusy_response_compatible() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/tenant_access_token/internal"):
            return httpx.Response(200, json={"code": 0, "tenant_access_token": "tenant-token"})
        return httpx.Response(200, json={
            "code": 0,
            "data": {
                "freebusy_list": [{
                    "user_id": "ou_interviewer",
                    "freebusy": [{
                        "start_time": "2026-07-21T06:30:00+00:00",
                        "end_time": "2026-07-21T07:30:00+00:00",
                    }],
                }],
            },
        })

    provider = HttpFeishuProvider(httpx.Client(transport=httpx.MockTransport(handler)))
    start = datetime(2026, 7, 21, 6, tzinfo=timezone.utc)
    windows = provider.batch_freebusy(
        FeishuCredentials("cli", "secret", "https://example.test/callback"),
        FreeBusyRequest(("ou_interviewer",), start, start + timedelta(hours=3)),
    )

    assert len(windows) == 1
    assert windows[0].starts_at == datetime(2026, 7, 21, 6, 30, tzinfo=timezone.utc)


def test_fake_provider_does_not_make_network_calls_and_exposes_oauth_identity() -> None:
    identity = OAuthIdentity("on_123", "ou_123", "existing@example.test", "tenant")
    provider = FakeFeishuProvider(identity=identity)
    credentials = FeishuCredentials("cli", "secret", "https://example.test/callback")

    assert provider.test_connection(credentials).ok is True
    assert provider.exchange_code(credentials, "single-use-code") == identity
    assert provider.exchanged_codes == ["single-use-code"]
