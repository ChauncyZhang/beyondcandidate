"""Actual calendar-create/termination lock interleaving on a disposable database.

Normal gate: TERMINATION_CALENDAR_POSTGRES_URL points at an empty dedicated DB.
The test migrates it, uses only a controlled provider, and retains DB evidence.
It does not validate provider timeouts or real Feishu cancellation.
"""

import asyncio
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from sqlalchemy import create_engine, inspect, select, text

from server.app.core.settings import Settings
from server.app.integrations.feishu.models import FeishuInterviewSync
from server.app.integrations.feishu.provider import FakeFeishuProvider
from server.app.integrations.feishu.sync import schedule_interview_sync
from server.app.integrations.feishu.worker import FeishuCalendarOutboxHandler
from server.app.interviews.models import Interview
from server.app.main import create_app
from server.app.queue.models import OutboxEvent
from server.app.recruiting import termination
from server.app.recruiting.models import Application
from server.app.recruiting.termination_models import ApplicationTermination
from server.tests.test_feishu_notifications import _feedback_event
from server.tests.test_interview_api import Probe, seed_application


@pytest.mark.skipif(not os.getenv("TERMINATION_CALENDAR_POSTGRES_URL"), reason="Dedicated calendar PostgreSQL URL not configured")
def test_calendar_create_serializes_termination_and_queues_real_cancel():
    url = os.environ["TERMINATION_CALENDAR_POSTGRES_URL"]
    engine = create_engine(url.replace("+asyncpg", "+psycopg"))
    assert inspect(engine).get_table_names() == [], "Use an isolated empty calendar smoke database"
    subprocess.run([sys.executable, "-m", "alembic", "-c", "server/alembic.ini", "upgrade", "head"],
        env={**os.environ, "DATABASE_URL": url}, check=True, timeout=120, capture_output=True, text=True)
    app = create_app(settings=Settings(environment="test", database_url=url,
        cors_origins=["https://hr.example.test"]), database_probe=Probe(), storage_probe=Probe())
    sessions = app.state.identity_store.sync_session
    seed = seed_application(app)
    _, interview_id = _feedback_event(app, seed)
    with sessions.begin() as db:
        interview = db.get(Interview, interview_id)
        sync = schedule_interview_sync(db, interview, "create")
        organization_id = interview.organization_id
    with sessions() as db:
        event = db.scalar(select(OutboxEvent).where(OutboxEvent.topic == "feishu.calendar.create"))
        db.expunge(event)

    entered, release, termination_started = Event(), Event(), Event()
    termination_pid = []

    class ControlledProvider(FakeFeishuProvider):
        def create_event(self, credentials, request, *, idempotency_key):
            entered.set()
            assert release.wait(15), "Test did not release the controlled calendar call"
            return super().create_event(credentials, request, idempotency_key=idempotency_key)

    provider = ControlledProvider()
    handler = FeishuCalendarOutboxHandler(sessions, provider, app.state.feishu_secret_cipher)

    def stop():
        with sessions.begin() as db:
            db.execute(text("SET LOCAL statement_timeout = '15000ms'"))
            termination_pid.append(db.scalar(text("SELECT pg_backend_pid()")))
            termination_started.set()
            application = termination.lock_application(db, organization_id, seed["application_id"])
            resources = termination.dependent_resources(db, application)
            termination.terminate_application(db, application, expected_version=application.version,
                expected_preview=termination.preview_version(application, resources), reason_text="Candidate withdrew",
                confirm_not_onboarded=False, actor_user_id=seed["admin_id"], trace_id="calendar-race")

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            creating = pool.submit(asyncio.run, handler(event, event.id))
            assert entered.wait(10)
            stopping = pool.submit(stop)
            assert termination_started.wait(10)
            try:
                deadline = time.monotonic() + 10
                waiting = False
                while time.monotonic() < deadline:
                    with engine.connect() as connection:
                        waiting = connection.scalar(text("SELECT wait_event_type = 'Lock' FROM pg_stat_activity WHERE pid=:pid"),
                            {"pid": termination_pid[0]})
                    if waiting:
                        break
                    time.sleep(0.02)
                assert waiting, "Termination must wait for the calendar Candidate lock"
                assert not stopping.done()
            finally:
                release.set()
            creating.result(timeout=20)
            stopping.result(timeout=20)
        assert len(provider.events) == 1
        remote_id = next(iter(provider.events))
        with sessions() as db:
            application = db.get(Application, seed["application_id"])
            interview = db.get(Interview, interview_id)
            sync = db.scalar(select(FeishuInterviewSync).where(FeishuInterviewSync.interview_id == interview_id))
            stopped = db.scalar(select(ApplicationTermination))
            cancel = db.scalar(select(OutboxEvent).where(OutboxEvent.topic == "feishu.calendar.cancel"))
            db.expunge(cancel)
            assert application.stage == "withdrawn" and interview.status == "cancelled"
            assert sync.external_event_id == remote_id and sync.desired_action == "cancel"
            assert sync.sync_status == "pending" and cancel is not None
            assert any(item["kind"] == "calendar" and item["reference"] == remote_id
                and item["state"] == "needs_action" for item in stopped.external_items)
        # The existing cancellation path consumes the retained remote ID, and a
        # delayed replay of the old forward event cannot recreate the event.
        asyncio.run(handler(cancel, cancel.id))
        asyncio.run(handler(event, event.id))
        assert set(provider.events) == {remote_id}
        assert provider.events[remote_id].cancelled is True
        with sessions() as db:
            assert db.get(Application, seed["application_id"]).stage == "withdrawn"
            assert db.get(Interview, interview_id).status == "cancelled"
            sync = db.scalar(select(FeishuInterviewSync).where(FeishuInterviewSync.interview_id == interview_id))
            assert sync.sync_status == "cancelled" and sync.external_event_id == remote_id
            stopped = db.scalar(select(ApplicationTermination))
            assert stopped.external_status == "resolved"
            assert all(item["state"] == "resolved" for item in stopped.external_items)
    finally:
        release.set()
        app.state.identity_store.engine.dispose()
        engine.dispose()
