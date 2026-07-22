import asyncio
from datetime import datetime, timezone

from fastapi.testclient import TestClient
from sqlalchemy import select
from uuid import UUID

from server.app.identity.models import User
from server.app.integrations.feishu.models import FeishuIdentityBinding, FeishuInterviewSync
from server.app.integrations.feishu.provider import FakeFeishuProvider
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
        assert sync.sync_status == "pending"
        assert sync.desired_action == "create"
        assert event.topic == "feishu.calendar.create"
        assert event.payload == {
            "organization_id": str(sync.organization_id),
            "interview_id": str(sync.interview_id),
            "sync_id": str(sync.id),
        }
        assert str(event.id)


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
        assert event.attendee_emails == ("interview-admin@example.test",)


def test_verified_provider_change_only_marks_pending_confirmation(tmp_path) -> None:
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
            db.commit()

        response = client.post(
            "/api/v1/integrations/feishu/events",
            json={
                "organization_id": str(sync.organization_id),
                "verification_token": "verified-event-token",
                "external_event_id": "evt_provider_changed",
                "provider_revision": "rev-2",
            },
        )
        assert response.status_code == 202

    with app.state.identity_store.sync_session() as db:
        interview = db.get(Interview, interview_id)
        sync = db.scalar(select(FeishuInterviewSync).where(FeishuInterviewSync.interview_id == interview_id))
        assert (interview.starts_at, interview.ends_at) == original
        assert sync.sync_status == "pending_confirmation"
        assert sync.provider_revision == "rev-2"
