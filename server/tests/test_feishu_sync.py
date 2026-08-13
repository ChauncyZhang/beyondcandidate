import asyncio
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import select
from uuid import UUID

from server.app.identity.models import User
from server.app.integrations.feishu.models import (
    FeishuIdentityBinding,
    FeishuInterviewSync,
    FeishuOrganizationConfig,
)
from server.app.integrations.feishu.provider import FakeFeishuProvider
from server.app.integrations.feishu.sync import schedule_interview_sync
from server.app.integrations.feishu.worker import FeishuCalendarOutboxHandler
from server.app.interviews.models import Interview
from server.app.queue.models import OutboxEvent
from server.tests.test_interview_api import (
    create_interview,
    interview_payload,
    make_app,
    seed_application,
)
from server.tests.test_recruiting_api import login


def _future_payload(seed):
    return interview_payload(
        seed,
        starts_at=datetime(2030, 7, 20, 8, 0, tzinfo=timezone.utc),
    )


class RecordingCalendarProvider(FakeFeishuProvider):
    def __init__(self) -> None:
        super().__init__()
        self.calendar_calls = []

    def create_event(self, credentials, request, *, idempotency_key):
        self.calendar_calls.append(("create", credentials.calendar_id))
        return super().create_event(
            credentials, request, idempotency_key=idempotency_key
        )

    def update_event(self, credentials, event_id, request, *, idempotency_key):
        self.calendar_calls.append(("update", credentials.calendar_id))
        return super().update_event(
            credentials, event_id, request, idempotency_key=idempotency_key
        )

    def cancel_event(self, credentials, event_id, *, idempotency_key):
        self.calendar_calls.append(("cancel", credentials.calendar_id))
        return super().cancel_event(
            credentials, event_id, idempotency_key=idempotency_key
        )


def test_interview_create_queues_enabled_feishu_sync_with_durable_idempotency(tmp_path) -> None:
    app = make_app(tmp_path)
    seed = seed_application(app)
    with TestClient(app) as client:
        headers = login(client, "interview-admin@example.test")
        configured = client.put(
            "/api/v1/settings/integrations/feishu",
            headers=headers,
            json={
                "app_id": "cli_test",
                "app_secret": "app-secret-value",
                "redirect_uri": "https://hr.example.test/api/v1/auth/feishu/callback",
                "calendar_id": "primary",
                "enabled": True,
            },
        )
        assert configured.status_code == 200
        created, _ = create_interview(
            client,
            seed,
            key="feishu-sync-create",
            payload=_future_payload(seed),
        )
        assert created.status_code == 201
        interview_id = UUID(created.json()["data"]["id"])

    with app.state.identity_store.sync_session() as db:
        sync = db.scalar(select(FeishuInterviewSync).where(FeishuInterviewSync.interview_id == interview_id))
        event = db.scalar(select(OutboxEvent).where(OutboxEvent.aggregate_id == interview_id))
        notification = db.scalar(
            select(OutboxEvent).where(
                OutboxEvent.topic == "feishu.notification.send",
                OutboxEvent.aggregate_id == seed["interviewer_id"],
            )
        )
        assert sync.sync_status == "pending"
        assert sync.desired_action == "create"
        assert event.topic == "feishu.calendar.create"
        assert event.payload == {
            "organization_id": str(sync.organization_id),
            "interview_id": str(sync.interview_id),
            "sync_id": str(sync.id),
        }
        assert str(event.id)
        assert notification.payload == {
            "organization_id": str(sync.organization_id),
            "recipient_user_id": str(seed["interviewer_id"]),
            "event_type": "interview_scheduled",
            "application_id": str(seed["application_id"]),
            "interview_id": str(interview_id),
        }


def test_interview_create_degrades_to_disabled_sync_without_outbox(tmp_path) -> None:
    app = make_app(tmp_path)
    seed = seed_application(app)
    with TestClient(app) as client:
        created, _ = create_interview(
            client,
            seed,
            key="feishu-disabled-create",
            payload=_future_payload(seed),
        )
        interview_id = UUID(created.json()["data"]["id"])

    with app.state.identity_store.sync_session() as db:
        sync = db.scalar(select(FeishuInterviewSync).where(FeishuInterviewSync.interview_id == interview_id))
        assert sync.sync_status == "disabled"
        assert db.scalar(select(OutboxEvent).where(OutboxEvent.aggregate_id == interview_id)) is None


def test_reschedule_notifies_new_and_removed_interviewers_separately(tmp_path) -> None:
    app = make_app(tmp_path)
    seed = seed_application(app)
    with TestClient(app) as client:
        headers = login(client, "interview-admin@example.test")
        assert client.put(
            "/api/v1/settings/integrations/feishu",
            headers=headers,
            json={
                "app_id": "cli_test",
                "app_secret": "app-secret-value",
                "redirect_uri": "https://hr.example.test/api/v1/auth/feishu/callback",
                "calendar_id": "primary",
                "enabled": True,
            },
        ).status_code == 200
        created, headers = create_interview(
            client,
            seed,
            key="feishu-participant-create",
            payload=_future_payload(seed),
        )
        interview_id = created.json()["data"]["id"]
        start = datetime(2030, 7, 21, 8, 0, tzinfo=timezone.utc)
        updated = client.patch(
            f"/api/v1/interviews/{interview_id}",
            headers={**headers, "If-Match": '"1"'},
            json={
                "starts_at": start.isoformat(),
                "ends_at": (start + timedelta(minutes=45)).isoformat(),
                "participants": [
                    {
                        "user_id": str(seed["other_interviewer_id"]),
                        "role": "interviewer",
                        "required_feedback": True,
                    }
                ],
            },
        )
        assert updated.status_code == 200

    with app.state.identity_store.sync_session() as db:
        events = list(
            db.scalars(
                select(OutboxEvent).where(
                    OutboxEvent.topic == "feishu.notification.send"
                )
            )
        )
        events = [event for event in events if event.payload.get("interview_id") == interview_id]
        recipients = {
            (event.payload["event_type"], event.aggregate_id) for event in events
        }
        assert ("interview_rescheduled", seed["other_interviewer_id"]) in recipients
        assert ("interview_assignment_removed", seed["interviewer_id"]) in recipients


def test_outbox_handler_creates_event_and_persists_retry_safe_sync_status(tmp_path) -> None:
    app = make_app(tmp_path)
    app.state.feishu_provider = FakeFeishuProvider()
    seed = seed_application(app)
    with app.state.identity_store.sync_session() as db:
        interviewer = db.get(User, seed["interviewer_id"])
        db.add(
            FeishuIdentityBinding(
                organization_id=interviewer.organization_id,
                user_id=interviewer.id,
                union_id="on_interviewer",
                open_id="ou_interviewer",
                tenant_key="tenant",
            )
        )
        db.commit()
    with TestClient(app) as client:
        headers = login(client, "interview-admin@example.test")
        client.put(
            "/api/v1/settings/integrations/feishu",
            headers=headers,
            json={
                "app_id": "cli_test",
                "app_secret": "app-secret-value",
                "redirect_uri": "https://hr.example.test/api/v1/auth/feishu/callback",
                "calendar_id": "primary",
                "enabled": True,
            },
        )
        created, _ = create_interview(
            client,
            seed,
            key="feishu-handler-create",
            payload=_future_payload(seed),
        )
        interview_id = UUID(created.json()["data"]["id"])
    with app.state.identity_store.sync_session() as db:
        event = db.scalar(select(OutboxEvent).where(OutboxEvent.aggregate_id == interview_id))
        db.expunge(event)

    handler = FeishuCalendarOutboxHandler(
        app.state.identity_store.sync_session,
        app.state.feishu_provider,
        app.state.feishu_secret_cipher,
    )
    asyncio.run(handler(event, event.id))

    with app.state.identity_store.sync_session() as db:
        sync = db.scalar(select(FeishuInterviewSync).where(FeishuInterviewSync.interview_id == interview_id))
        assert sync.sync_status == "synced"
        assert sync.attempts == 1
        assert sync.external_event_id in app.state.feishu_provider.events
        event = app.state.feishu_provider.events[sync.external_event_id]
        assert event.attendee_open_ids == ("ou_interviewer",)
        assert event.attendee_emails == ("interview-admin@example.com",)


@pytest.mark.parametrize("action", ["update", "cancel"])
def test_existing_event_uses_persisted_calendar_after_config_change(
    tmp_path, action
) -> None:
    app = make_app(tmp_path)
    provider = RecordingCalendarProvider()
    seed = seed_application(app)
    with TestClient(app) as client:
        headers = login(client, "interview-admin@example.test")
        assert client.put(
            "/api/v1/settings/integrations/feishu",
            headers=headers,
            json={
                "app_id": "cli_test",
                "app_secret": "app-secret-value",
                "redirect_uri": "https://hr.example.test/api/v1/auth/feishu/callback",
                "calendar_id": "legacy-calendar",
                "enabled": True,
            },
        ).status_code == 200
        created, _ = create_interview(
            client,
            seed,
            key=f"feishu-calendar-change-{action}",
            payload=_future_payload(seed),
        )
        interview_id = UUID(created.json()["data"]["id"])

    handler = FeishuCalendarOutboxHandler(
        app.state.identity_store.sync_session,
        provider,
        app.state.feishu_secret_cipher,
    )
    with app.state.identity_store.sync_session() as db:
        create_event = db.scalar(
            select(OutboxEvent).where(
                OutboxEvent.aggregate_id == interview_id,
                OutboxEvent.topic == "feishu.calendar.create",
            )
        )
        db.expunge(create_event)
    asyncio.run(handler(create_event, create_event.id))

    with app.state.identity_store.sync_session() as db:
        config = db.scalar(
            select(FeishuOrganizationConfig).where(
                FeishuOrganizationConfig.organization_id == create_event.organization_id
            )
        )
        config.calendar_id = "current-calendar"
        interview = db.get(Interview, interview_id)
        schedule_interview_sync(db, interview, action)
        db.commit()
        changed_event = db.scalar(
            select(OutboxEvent).where(
                OutboxEvent.aggregate_id == interview_id,
                OutboxEvent.topic == f"feishu.calendar.{action}",
            )
        )
        db.expunge(changed_event)

    asyncio.run(handler(changed_event, changed_event.id))

    assert provider.calendar_calls == [
        ("create", "legacy-calendar"),
        (action, "legacy-calendar"),
    ]


def test_verified_provider_change_does_not_guess_a_specific_interview(tmp_path) -> None:
    app = make_app(tmp_path)
    seed = seed_application(app)
    with TestClient(app) as client:
        headers = login(client, "interview-admin@example.test")
        client.put(
            "/api/v1/settings/integrations/feishu",
            headers=headers,
            json={
                "app_id": "cli_test",
                "app_secret": "app-secret-value",
                "redirect_uri": "https://hr.example.test/api/v1/auth/feishu/callback",
                "calendar_id": "primary",
                "verification_token": "verified-event-token",
                "enabled": True,
            },
        )
        created, _ = create_interview(
            client,
            seed,
            key="feishu-provider-change",
            payload=_future_payload(seed),
        )
        interview_id = UUID(created.json()["data"]["id"])
        with app.state.identity_store.sync_session() as db:
            interview = db.get(Interview, interview_id)
            original = (interview.starts_at, interview.ends_at)
            sync = db.scalar(select(FeishuInterviewSync).where(FeishuInterviewSync.interview_id == interview_id))
            sync.external_event_id = "evt_provider_changed"
            sync.sync_status = "synced"
            db.add(
                FeishuIdentityBinding(
                    organization_id=sync.organization_id,
                    user_id=seed["admin_id"],
                    union_id="on_provider_change_admin",
                    open_id="ou_provider_change_admin",
                    tenant_key="tenant-provider-change",
                )
            )
            db.commit()

        response = client.post(
            "/api/v1/integrations/feishu/events",
            json={
                "schema": "2.0",
                "header": {
                    "event_id": "evt-provider-change-delivery",
                    "event_type": "calendar.calendar.event.changed_v4",
                    "token": "verified-event-token",
                    "app_id": "cli_test",
                    "tenant_key": "tenant-provider-change",
                },
                "event": {
                    "calendar_id": "primary",
                    "user_id_list": [],
                },
            },
        )
        assert response.status_code == 200

    with app.state.identity_store.sync_session() as db:
        interview = db.get(Interview, interview_id)
        sync = db.scalar(select(FeishuInterviewSync).where(FeishuInterviewSync.interview_id == interview_id))
        assert (interview.starts_at, interview.ends_at) == original
        assert sync.sync_status == "synced"
        assert sync.provider_revision is None
