"""Forward Feishu work stops; cancellation remains executable after termination."""

import asyncio

import pytest
from sqlalchemy import select

from server.app.integrations.feishu import worker as workers
from server.app.integrations.feishu.models import FeishuInterviewSync
from server.app.integrations.feishu.provider import FakeFeishuProvider
from server.app.integrations.feishu.sync import schedule_interview_sync
from server.app.interviews.models import Interview
from server.app.queue.models import OutboxEvent
from server.tests.test_feishu_notifications import _feedback_event
from server.tests.test_interview_api import make_app, seed_application


def test_forward_notification_checks_termination_before_provider_call(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    seed = seed_application(app)
    event, _ = _feedback_event(app, seed)
    monkeypatch.setattr(workers, "application_is_terminated", lambda *args: True)
    provider = FakeFeishuProvider()
    handler = workers.FeishuNotificationOutboxHandler(app.state.identity_store.sync_session,
        provider, app.state.feishu_secret_cipher)
    asyncio.run(handler(event, event.id))
    assert provider.cards == []


@pytest.mark.parametrize("action", ["create", "update", "cancel"])
def test_calendar_only_allows_cleanup_after_termination(tmp_path, monkeypatch, action):
    app = make_app(tmp_path)
    seed = seed_application(app)
    _, interview_id = _feedback_event(app, seed)
    with app.state.identity_store.sync_session() as db:
        interview = db.get(Interview, interview_id)
        sync = schedule_interview_sync(db, interview, action)
        sync.external_event_id = "existing_event" if action != "create" else None
        sync.external_calendar_id = "primary" if action != "create" else None
        if action == "update":
            sync = schedule_interview_sync(db, interview, action)
        db.commit()
        event = db.scalar(select(OutboxEvent).where(OutboxEvent.topic == f"feishu.calendar.{action}"))
        db.expunge(event)
    calls = []
    facts = []
    provider = FakeFeishuProvider()
    monkeypatch.setattr(workers, "application_is_terminated", lambda *args: True)
    monkeypatch.setattr(workers, "record_external_fact", lambda *args, **kwargs: facts.append(kwargs))
    monkeypatch.setattr(provider, "create_event", lambda *args, **kwargs: calls.append("create"))
    monkeypatch.setattr(provider, "update_event", lambda *args, **kwargs: calls.append("update"))
    monkeypatch.setattr(provider, "cancel_event", lambda *args, **kwargs: calls.append("cancel"))
    handler = workers.FeishuCalendarOutboxHandler(app.state.identity_store.sync_session,
        provider, app.state.feishu_secret_cipher)
    asyncio.run(handler(event, event.id))
    assert calls == (["cancel"] if action == "cancel" else [])
    with app.state.identity_store.sync_session() as db:
        sync = db.scalar(select(FeishuInterviewSync).where(FeishuInterviewSync.interview_id == interview_id))
        assert sync.sync_status == ("cancelled" if action == "cancel" else "pending")
        assert sync.attempts == (1 if action == "cancel" else 0)
        assert facts == ([dict(kind="calendar", resource_id=interview_id, state="resolved",
            reference="existing_event", generation=sync.idempotency_key)] if action == "cancel" else [])
