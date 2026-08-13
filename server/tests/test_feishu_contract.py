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
    FeishuProviderError,
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
        if request.method == "GET" and request.url.path.endswith("/attendees"):
            return httpx.Response(200, json={"code": 0, "data": {"items": [], "has_more": False}})
        if request.method == "POST" and request.url.path.endswith("/attendees"):
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


def test_http_provider_create_retry_does_not_add_duplicate_attendees() -> None:
    attendee_adds: list[httpx.Request] = []
    current_attendees: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/tenant_access_token/internal"):
            return httpx.Response(200, json={"code": 0, "tenant_access_token": "tenant-token"})
        if request.method == "POST" and request.url.path.endswith("/events"):
            return httpx.Response(200, json={"code": 0, "data": {"event": {"event_id": "evt_1"}}})
        if request.method == "GET" and request.url.path.endswith("/events/evt_1/attendees"):
            return httpx.Response(200, json={
                "code": 0,
                "data": {"items": current_attendees, "has_more": False},
            })
        if request.method == "POST" and request.url.path.endswith("/events/evt_1/attendees"):
            attendee_adds.append(request)
            for index, attendee in enumerate(json.loads(request.content)["attendees"]):
                current_attendees.append({
                    **attendee,
                    "attendee_id": f"attendee_{index}",
                    "is_organizer": False,
                })
            return httpx.Response(200, json={"code": 0, "data": {"attendees": []}})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    provider = HttpFeishuProvider(httpx.Client(transport=httpx.MockTransport(handler)))
    credentials = FeishuCredentials("cli", "secret", "https://example.test/callback")
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

    provider.create_event(credentials, request, idempotency_key="event-key")
    provider.create_event(credentials, request, idempotency_key="event-key")

    assert len(attendee_adds) == 1


def test_http_provider_update_deduplicates_external_emails_case_insensitively() -> None:
    attendee_adds: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/tenant_access_token/internal"):
            return httpx.Response(200, json={"code": 0, "tenant_access_token": "tenant-token"})
        if request.method == "PATCH" and request.url.path.endswith("/events/evt_1"):
            return httpx.Response(200, json={"code": 0, "data": {}})
        if request.method == "GET" and request.url.path.endswith("/events/evt_1/attendees"):
            return httpx.Response(200, json={"code": 0, "data": {"items": [], "has_more": False}})
        if request.method == "POST" and request.url.path.endswith("/events/evt_1/attendees"):
            attendee_adds.append(request)
            return httpx.Response(200, json={"code": 0, "data": {"attendees": []}})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    provider = HttpFeishuProvider(httpx.Client(transport=httpx.MockTransport(handler)))
    provider.update_event(
        FeishuCredentials("cli", "secret", "https://example.test/callback"),
        "evt_1",
        _calendar_event_request(
            attendee_open_ids=(),
            attendee_emails=("User@example.test", "user@example.test"),
        ),
        idempotency_key="update-key",
    )

    assert len(attendee_adds) == 1
    assert json.loads(attendee_adds[0].content)["attendees"] == [
        {"type": "third_party", "third_party_email": "User@example.test"},
    ]


def test_http_provider_update_retry_recovers_after_delete_succeeds_and_add_fails() -> None:
    current_attendees = [
        {"type": "user", "attendee_id": "remove", "user_id": "ou_remove"},
    ]
    delete_calls = 0
    add_attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal delete_calls, add_attempts
        if request.url.path.endswith("/tenant_access_token/internal"):
            return httpx.Response(200, json={"code": 0, "tenant_access_token": "tenant-token"})
        if request.method == "PATCH" and request.url.path.endswith("/events/evt_1"):
            return httpx.Response(200, json={"code": 0, "data": {}})
        if request.method == "GET" and request.url.path.endswith("/events/evt_1/attendees"):
            return httpx.Response(200, json={
                "code": 0,
                "data": {"items": current_attendees, "has_more": False},
            })
        if request.method == "POST" and request.url.path.endswith("/attendees/batch_delete"):
            delete_calls += 1
            current_attendees.clear()
            return httpx.Response(200, json={"code": 0, "data": {}})
        if request.method == "POST" and request.url.path.endswith("/events/evt_1/attendees"):
            add_attempts += 1
            if add_attempts == 1:
                raise httpx.ReadTimeout("attendee add timed out", request=request)
            attendee = json.loads(request.content)["attendees"][0]
            current_attendees.append({**attendee, "attendee_id": "added"})
            return httpx.Response(200, json={"code": 0, "data": {"attendees": []}})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    provider = HttpFeishuProvider(httpx.Client(transport=httpx.MockTransport(handler)))
    credentials = FeishuCredentials("cli", "secret", "https://example.test/callback")
    request = _calendar_event_request(attendee_open_ids=("ou_add",), attendee_emails=())

    with pytest.raises(FeishuProviderError):
        provider.update_event(credentials, "evt_1", request, idempotency_key="update-key")
    provider.update_event(credentials, "evt_1", request, idempotency_key="update-key")

    assert delete_calls == 1
    assert add_attempts == 2
    assert current_attendees[0]["user_id"] == "ou_add"


def test_http_provider_update_retry_does_not_readd_after_add_response_is_lost() -> None:
    current_attendees: list[dict] = []
    add_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal add_calls
        if request.url.path.endswith("/tenant_access_token/internal"):
            return httpx.Response(200, json={"code": 0, "tenant_access_token": "tenant-token"})
        if request.method == "PATCH" and request.url.path.endswith("/events/evt_1"):
            return httpx.Response(200, json={"code": 0, "data": {}})
        if request.method == "GET" and request.url.path.endswith("/events/evt_1/attendees"):
            return httpx.Response(200, json={
                "code": 0,
                "data": {"items": current_attendees, "has_more": False},
            })
        if request.method == "POST" and request.url.path.endswith("/events/evt_1/attendees"):
            add_calls += 1
            attendee = json.loads(request.content)["attendees"][0]
            current_attendees.append({**attendee, "attendee_id": "added"})
            raise httpx.ReadTimeout("attendee add response lost", request=request)
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    provider = HttpFeishuProvider(httpx.Client(transport=httpx.MockTransport(handler)))
    credentials = FeishuCredentials("cli", "secret", "https://example.test/callback")
    request = _calendar_event_request(attendee_open_ids=("ou_add",), attendee_emails=())

    with pytest.raises(FeishuProviderError):
        provider.update_event(credentials, "evt_1", request, idempotency_key="update-key")
    provider.update_event(credentials, "evt_1", request, idempotency_key="update-key")

    assert add_calls == 1


def test_http_provider_update_reconciles_internal_and_external_attendees_idempotently() -> None:
    attendee_requests: list[httpx.Request] = []
    current_attendees = [
        {
            "attendee_id": "attendee_organizer",
            "type": "user",
            "user_id": "ou_organizer",
            "is_organizer": True,
            "rsvp_status": "accept",
        },
        {
            "attendee_id": "attendee_keep_user",
            "type": "user",
            "user_id": "ou_keep",
            "is_organizer": False,
            "rsvp_status": "accept",
        },
        {
            "attendee_id": "attendee_remove_user",
            "type": "user",
            "user_id": "ou_remove",
            "is_organizer": False,
            "rsvp_status": "accept",
        },
        {
            "attendee_id": "attendee_keep_email",
            "type": "third_party",
            "third_party_email": "KEEP@example.test",
            "is_organizer": False,
            "rsvp_status": "needs_action",
        },
        {
            "attendee_id": "attendee_remove_email",
            "type": "third_party",
            "third_party_email": "remove@example.test",
            "is_organizer": False,
            "rsvp_status": "needs_action",
        },
        {
            "attendee_id": "attendee_chat",
            "type": "chat",
            "chat_id": "oc_existing",
            "is_organizer": False,
            "rsvp_status": "accept",
        },
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/tenant_access_token/internal"):
            return httpx.Response(200, json={"code": 0, "tenant_access_token": "tenant-token"})
        if request.method == "PATCH" and request.url.path.endswith("/events/evt_1"):
            return httpx.Response(200, json={"code": 0, "data": {"event": {"event_id": "evt_1"}}})
        if request.method == "GET" and request.url.path.endswith("/events/evt_1/attendees"):
            attendee_requests.append(request)
            offset = int(request.url.params.get("page_token", "0"))
            page = current_attendees[offset : offset + 3]
            next_offset = offset + len(page)
            has_more = next_offset < len(current_attendees)
            return httpx.Response(200, json={
                "code": 0,
                "data": {
                    "items": page,
                    "has_more": has_more,
                    "page_token": str(next_offset) if has_more else "",
                },
            })
        if request.method == "POST" and request.url.path.endswith("/attendees/batch_delete"):
            attendee_requests.append(request)
            attendee_ids = set(json.loads(request.content)["attendee_ids"])
            current_attendees[:] = [
                attendee for attendee in current_attendees
                if attendee["attendee_id"] not in attendee_ids
            ]
            return httpx.Response(200, json={"code": 0, "data": {}})
        if request.method == "POST" and request.url.path.endswith("/attendees"):
            attendee_requests.append(request)
            for index, attendee in enumerate(json.loads(request.content)["attendees"]):
                current_attendees.append({
                    "attendee_id": f"attendee_added_{index}",
                    "type": attendee["type"],
                    "user_id": attendee.get("user_id"),
                    "third_party_email": attendee.get("third_party_email"),
                    "is_organizer": False,
                    "rsvp_status": "needs_action",
                })
            return httpx.Response(200, json={"code": 0, "data": {"attendees": []}})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    provider = HttpFeishuProvider(httpx.Client(transport=httpx.MockTransport(handler)))
    credentials = FeishuCredentials("cli", "secret", "https://example.test/callback")
    request = CalendarEventRequest(
        interview_id=uuid4(),
        summary="Backend interview",
        starts_at=datetime(2026, 7, 20, 1, tzinfo=timezone.utc),
        ends_at=datetime(2026, 7, 20, 2, tzinfo=timezone.utc),
        timezone="Asia/Shanghai",
        description="Interview",
        location="Room 1",
        attendee_open_ids=("ou_keep", "ou_add"),
        attendee_emails=("keep@example.test", "add@example.test"),
    )

    provider.update_event(credentials, "evt_1", request, idempotency_key="update-key")
    provider.update_event(credentials, "evt_1", request, idempotency_key="update-key")

    delete_requests = [
        item for item in attendee_requests
        if item.url.path.endswith("/attendees/batch_delete")
    ]
    add_requests = [
        item for item in attendee_requests
        if item.method == "POST" and item.url.path.endswith("/attendees")
    ]
    assert len(delete_requests) == 1
    assert delete_requests[0].url.params["user_id_type"] == "open_id"
    assert json.loads(delete_requests[0].content) == {
        "attendee_ids": ["attendee_remove_user", "attendee_remove_email"],
        "need_notification": True,
    }
    assert len(add_requests) == 1
    assert json.loads(add_requests[0].content) == {
        "attendees": [
            {"type": "user", "user_id": "ou_add"},
            {"type": "third_party", "third_party_email": "add@example.test"},
        ],
        "need_notification": True,
    }
    list_requests = [item for item in attendee_requests if item.method == "GET"]
    assert len(list_requests) == 4
    assert all(item.url.params["user_id_type"] == "open_id" for item in list_requests)
    assert all(item.url.params["page_size"] == "100" for item in list_requests)


def _calendar_event_request(
    *,
    attendee_open_ids: tuple[str, ...],
    attendee_emails: tuple[str, ...],
) -> CalendarEventRequest:
    return CalendarEventRequest(
        interview_id=uuid4(),
        summary="Backend interview",
        starts_at=datetime(2026, 7, 20, 1, tzinfo=timezone.utc),
        ends_at=datetime(2026, 7, 20, 2, tzinfo=timezone.utc),
        timezone="Asia/Shanghai",
        description="Interview",
        location="Room 1",
        attendee_open_ids=attendee_open_ids,
        attendee_emails=attendee_emails,
    )


def test_http_provider_update_adds_only_missing_internal_and_external_attendees() -> None:
    attendee_writes: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/tenant_access_token/internal"):
            return httpx.Response(200, json={"code": 0, "tenant_access_token": "tenant-token"})
        if request.method == "PATCH" and request.url.path.endswith("/events/evt_1"):
            return httpx.Response(200, json={"code": 0, "data": {}})
        if request.method == "GET" and request.url.path.endswith("/events/evt_1/attendees"):
            return httpx.Response(200, json={
                "code": 0,
                "data": {
                    "items": [
                        {"type": "user", "attendee_id": "attendee_existing", "user_id": "ou_existing"},
                        {"type": "third_party", "attendee_id": "attendee_email", "third_party_email": "existing@example.test"},
                    ],
                    "has_more": False,
                },
            })
        if request.method == "POST" and request.url.path.endswith("/events/evt_1/attendees"):
            attendee_writes.append(request)
            return httpx.Response(200, json={"code": 0, "data": {"attendees": []}})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    provider = HttpFeishuProvider(httpx.Client(transport=httpx.MockTransport(handler)))
    provider.update_event(
        FeishuCredentials("cli", "secret", "https://example.test/callback"),
        "evt_1",
        _calendar_event_request(
            attendee_open_ids=("ou_existing", "ou_missing"),
            attendee_emails=("existing@example.test", "missing@example.test"),
        ),
        idempotency_key="update-key",
    )

    assert len(attendee_writes) == 1
    assert json.loads(attendee_writes[0].content) == {
        "attendees": [
            {"type": "user", "user_id": "ou_missing"},
            {"type": "third_party", "third_party_email": "missing@example.test"},
        ],
        "need_notification": True,
    }


def test_http_provider_update_deletes_removed_internal_and_external_attendees() -> None:
    attendee_deletes: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/tenant_access_token/internal"):
            return httpx.Response(200, json={"code": 0, "tenant_access_token": "tenant-token"})
        if request.method == "PATCH" and request.url.path.endswith("/events/evt_1"):
            return httpx.Response(200, json={"code": 0, "data": {}})
        if request.method == "GET" and request.url.path.endswith("/events/evt_1/attendees"):
            return httpx.Response(200, json={
                "code": 0,
                "data": {
                    "items": [
                        {"type": "user", "attendee_id": "organizer", "user_id": "ou_organizer", "is_organizer": True},
                        {"type": "user", "attendee_id": "keep", "user_id": "ou_keep"},
                        {"type": "user", "attendee_id": "remove_user", "user_id": "ou_remove"},
                        {"type": "third_party", "attendee_id": "remove_email", "third_party_email": "remove@example.test"},
                    ],
                    "has_more": False,
                },
            })
        if request.method == "POST" and request.url.path.endswith("/events/evt_1/attendees/batch_delete"):
            attendee_deletes.append(request)
            return httpx.Response(200, json={"code": 0, "data": {}})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    provider = HttpFeishuProvider(httpx.Client(transport=httpx.MockTransport(handler)))
    provider.update_event(
        FeishuCredentials("cli", "secret", "https://example.test/callback"),
        "evt_1",
        _calendar_event_request(attendee_open_ids=("ou_keep",), attendee_emails=()),
        idempotency_key="update-key",
    )

    assert len(attendee_deletes) == 1
    assert attendee_deletes[0].url.params["user_id_type"] == "open_id"
    assert json.loads(attendee_deletes[0].content) == {
        "attendee_ids": ["remove_user", "remove_email"],
        "need_notification": True,
    }


def test_http_provider_update_with_unchanged_paginated_attendees_has_no_attendee_writes() -> None:
    attendee_writes: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/tenant_access_token/internal"):
            return httpx.Response(200, json={"code": 0, "tenant_access_token": "tenant-token"})
        if request.method == "PATCH" and request.url.path.endswith("/events/evt_1"):
            return httpx.Response(200, json={"code": 0, "data": {}})
        if request.method == "GET" and request.url.path.endswith("/events/evt_1/attendees"):
            if request.url.params.get("page_token") is None:
                return httpx.Response(200, json={
                    "code": 0,
                    "data": {
                        "items": [{"type": "user", "attendee_id": "user", "user_id": "ou_keep"}],
                        "has_more": True,
                        "page_token": "next-page",
                    },
                })
            assert request.url.params["page_token"] == "next-page"
            return httpx.Response(200, json={
                "code": 0,
                "data": {
                    "items": [{"type": "third_party", "attendee_id": "email", "third_party_email": "keep@example.test"}],
                    "has_more": False,
                },
            })
        if "/attendees" in request.url.path:
            attendee_writes.append(request)
            return httpx.Response(200, json={"code": 0, "data": {}})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    provider = HttpFeishuProvider(httpx.Client(transport=httpx.MockTransport(handler)))
    provider.update_event(
        FeishuCredentials("cli", "secret", "https://example.test/callback"),
        "evt_1",
        _calendar_event_request(
            attendee_open_ids=("ou_keep",),
            attendee_emails=("keep@example.test",),
        ),
        idempotency_key="update-key",
    )

    assert attendee_writes == []


def test_http_provider_rejects_cyclic_attendee_page_tokens() -> None:
    page_tokens = iter(("page-a", "page-b", "page-a"))

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/tenant_access_token/internal"):
            return httpx.Response(200, json={"code": 0, "tenant_access_token": "tenant-token"})
        if request.method == "PATCH" and request.url.path.endswith("/events/evt_1"):
            return httpx.Response(200, json={"code": 0, "data": {}})
        if request.method == "GET" and request.url.path.endswith("/events/evt_1/attendees"):
            return httpx.Response(200, json={
                "code": 0,
                "data": {
                    "items": [],
                    "has_more": True,
                    "page_token": next(page_tokens),
                },
            })
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    provider = HttpFeishuProvider(httpx.Client(transport=httpx.MockTransport(handler)))

    with pytest.raises(FeishuProviderError) as raised:
        provider.update_event(
            FeishuCredentials("cli", "secret", "https://example.test/callback"),
            "evt_1",
            _calendar_event_request(attendee_open_ids=(), attendee_emails=()),
            idempotency_key="update-key",
        )

    assert raised.value.safe_code == "feishu_response_invalid"
    assert raised.value.retryable is False


def test_http_provider_update_ignores_attendees_already_marked_removed() -> None:
    attendee_deletes: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/tenant_access_token/internal"):
            return httpx.Response(200, json={"code": 0, "tenant_access_token": "tenant-token"})
        if request.method == "PATCH" and request.url.path.endswith("/events/evt_1"):
            return httpx.Response(200, json={"code": 0, "data": {}})
        if request.method == "GET" and request.url.path.endswith("/events/evt_1/attendees"):
            return httpx.Response(200, json={
                "code": 0,
                "data": {
                    "items": [{
                        "type": "user",
                        "attendee_id": "already_removed",
                        "user_id": "ou_removed",
                        "rsvp_status": "removed",
                    }],
                    "has_more": False,
                },
            })
        if request.method == "POST" and request.url.path.endswith("/attendees/batch_delete"):
            attendee_deletes.append(request)
            return httpx.Response(200, json={"code": 0, "data": {}})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    provider = HttpFeishuProvider(httpx.Client(transport=httpx.MockTransport(handler)))
    credentials = FeishuCredentials("cli", "secret", "https://example.test/callback")
    request = _calendar_event_request(attendee_open_ids=(), attendee_emails=())

    provider.update_event(credentials, "evt_1", request, idempotency_key="update-key")
    provider.update_event(credentials, "evt_1", request, idempotency_key="update-key")

    assert attendee_deletes == []


def test_http_provider_update_batches_attendee_deletes_at_official_limit() -> None:
    attendee_deletes: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/tenant_access_token/internal"):
            return httpx.Response(200, json={"code": 0, "tenant_access_token": "tenant-token"})
        if request.method == "PATCH" and request.url.path.endswith("/events/evt_1"):
            return httpx.Response(200, json={"code": 0, "data": {}})
        if request.method == "GET" and request.url.path.endswith("/events/evt_1/attendees"):
            return httpx.Response(200, json={
                "code": 0,
                "data": {
                    "items": [
                        {
                            "type": "user",
                            "attendee_id": f"attendee_{index}",
                            "user_id": f"ou_{index}",
                        }
                        for index in range(501)
                    ],
                    "has_more": False,
                },
            })
        if request.method == "POST" and request.url.path.endswith("/attendees/batch_delete"):
            attendee_deletes.append(request)
            return httpx.Response(200, json={"code": 0, "data": {}})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    provider = HttpFeishuProvider(httpx.Client(transport=httpx.MockTransport(handler)))
    provider.update_event(
        FeishuCredentials("cli", "secret", "https://example.test/callback"),
        "evt_1",
        _calendar_event_request(attendee_open_ids=(), attendee_emails=()),
        idempotency_key="update-key",
    )

    assert [
        len(json.loads(request.content)["attendee_ids"])
        for request in attendee_deletes
    ] == [500, 1]


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


def test_http_provider_reuses_tenant_token_until_its_refresh_window() -> None:
    token_requests = 0
    now = [100.0]

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal token_requests
        if request.url.path.endswith("/tenant_access_token/internal"):
            token_requests += 1
            return httpx.Response(200, json={
                "code": 0,
                "tenant_access_token": f"tenant-token-{token_requests}",
                "expire": 100,
            })
        if request.url.path.endswith("/freebusy/batch"):
            return httpx.Response(200, json={"code": 0, "data": {}})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    provider = HttpFeishuProvider(
        httpx.Client(transport=httpx.MockTransport(handler)),
        clock=lambda: now[0],
    )
    credentials = FeishuCredentials("cli", "secret", "https://example.test/callback")
    start = datetime(2026, 7, 21, 6, tzinfo=timezone.utc)
    request = FreeBusyRequest(("ou_interviewer",), start, start + timedelta(hours=1))

    provider.batch_freebusy(credentials, request)
    provider.batch_freebusy(credentials, request)
    assert token_requests == 1

    now[0] = 191.0
    provider.batch_freebusy(credentials, request)
    assert token_requests == 2


def test_http_provider_does_not_reuse_token_after_secret_changes() -> None:
    token_requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal token_requests
        if request.url.path.endswith("/tenant_access_token/internal"):
            token_requests += 1
            return httpx.Response(200, json={
                "code": 0,
                "tenant_access_token": f"tenant-token-{token_requests}",
                "expire": 7200,
            })
        if request.url.path.endswith("/freebusy/batch"):
            return httpx.Response(200, json={"code": 0, "data": {}})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    provider = HttpFeishuProvider(httpx.Client(transport=httpx.MockTransport(handler)))
    start = datetime(2026, 7, 21, 6, tzinfo=timezone.utc)
    request = FreeBusyRequest(("ou_interviewer",), start, start + timedelta(hours=1))

    provider.batch_freebusy(FeishuCredentials("cli", "secret-one", "https://example.test/callback"), request)
    provider.batch_freebusy(FeishuCredentials("cli", "secret-two", "https://example.test/callback"), request)

    assert token_requests == 2


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
