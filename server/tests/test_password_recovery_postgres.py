"""Bounded concurrency checks against an initially empty disposable PostgreSQL DB."""

import asyncio
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, inspect, select, text

from server.app.communications.models import EmailDelivery
from server.app.communications.worker import EmailDeliveryJobHandler
from server.app.core.settings import Settings
from server.app.identity.models import AuditLog, PasswordInvitation, PasswordRecoveryRequest, User
from server.app.identity.password_recovery import PasswordRecoveryJobHandler, RECOVERY_JOB_TYPE, REQUEST_EVENT, request_password_reset
from server.app.main import create_app
from server.app.queue.models import BackgroundJob
from server.tests.test_identity_management import Probe, seed_user
from server.tests.test_password_recovery import configure, FakeSmtp, request_reset


@pytest.mark.skipif(not os.getenv("PASSWORD_RECOVERY_POSTGRES_URL"), reason="Disposable recovery PostgreSQL URL not configured")
def test_recovery_serializes_network_user_and_worker_lifecycle():
    settings = Settings(environment="test", database_url=os.environ["PASSWORD_RECOVERY_POSTGRES_URL"],
        offer_public_base_url="https://trusted.example.com", cors_origins=["https://hr.example.test"])
    app = create_app(settings, database_probe=Probe(), storage_probe=Probe())
    store = app.state.identity_store
    engine = store.engine
    assert inspect(engine).get_table_names() == [], "Use an isolated empty recovery database"
    # The ORM's SQLite-only JSON checks are not a PostgreSQL schema authority.
    subprocess.run([sys.executable, "-m", "alembic", "-c", "server/alembic.ini", "upgrade", "0043_password_reset"],
        env={**os.environ, "DATABASE_URL": settings.database_url}, check=True, timeout=120, capture_output=True, text=True)
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE ROLE recovery_smoke_app NOLOGIN")
        connection.exec_driver_sql("GRANT SELECT, INSERT, UPDATE, DELETE ON password_invitations TO recovery_smoke_app")
    subprocess.run([sys.executable, "-m", "alembic", "-c", "server/alembic.ini", "upgrade", "head"],
        env={**os.environ, "DATABASE_URL": settings.database_url}, check=True, timeout=120, capture_output=True, text=True)
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "0044_password_recovery"
        for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
            assert connection.scalar(text("SELECT has_table_privilege('recovery_smoke_app', 'password_recovery_requests', :privilege)"), {"privilege": privilege})

    def bounded_queries(connection):
        connection.exec_driver_sql("SET statement_timeout = '15s'")
        connection.exec_driver_sql("SET lock_timeout = '10s'")
        connection.commit()

    event.listen(engine, "engine_connect", bounded_queries)
    try:
        target = seed_user(app, role="recruiter", email="target@example.com")
        configure(app, target)

        def issue(email, network):
            request_password_reset(app.state.identity_service, app.state.email_secret_cipher, settings,
                "acme", email, trace_id="recovery-postgres-concurrency", network=network)

        def concurrent_requests(count, email, networks):
            barrier = Barrier(count)

            def run(index):
                barrier.wait(timeout=10)
                issue(email, networks[index])

            with ThreadPoolExecutor(max_workers=count) as pool:
                futures = [pool.submit(run, index) for index in range(count)]
                for future in futures:
                    future.result(timeout=25)

        concurrent_requests(6, "missing@example.com", ["192.0.2.1"] * 6)
        network_id = app.state.email_secret_cipher.fingerprint("password-reset-network", "192.0.2.1")
        with store.sync_session() as db:
            audits = db.scalars(select(AuditLog).where(AuditLog.event_type == REQUEST_EVENT, AuditLog.ip_hash == network_id)).all()
            assert len(audits) == 5
            assert db.query(PasswordInvitation).count() == db.query(EmailDelivery).count() == 0
        concurrent_requests(6, "target@example.com", [f"192.0.2.{index + 10}" for index in range(6)])
        with store.sync_session() as db:
            assert db.query(PasswordInvitation).count() == db.query(EmailDelivery).count() == 0
            recovery_jobs = db.scalars(select(BackgroundJob).where(BackgroundJob.type == RECOVERY_JOB_TYPE)).all()
            assert len(recovery_jobs) == 11
        recovery_handler = PasswordRecoveryJobHandler(store.sync_session, app.state.email_secret_cipher, settings)
        with ThreadPoolExecutor(max_workers=6) as pool:
            futures = [pool.submit(lambda job=job: asyncio.run(recovery_handler(job))) for job in recovery_jobs]
            for future in futures:
                future.result(timeout=25)
        with store.sync_session() as db:
            assert db.query(PasswordInvitation).count() == db.query(EmailDelivery).count() == 1
            assert all(row.processed_at is not None and row.email_ciphertext is None for row in db.scalars(select(PasswordRecoveryRequest)))
            reset_id = db.scalar(select(PasswordInvitation.id))
            job = db.scalar(select(BackgroundJob).where(BackgroundJob.type == "communications.send_email"))

        # Model admin reissue's user-first lock and expiry update, deliberately
        # completing between the worker locating the user and acquiring its lock.
        worker_waiting = Event()
        worker_pid = []

        def before_user_lock(connection, cursor, statement, parameters, context, many):
            if "FROM users" in statement and "FOR UPDATE" in statement:
                worker_pid.append(connection.exec_driver_sql("SELECT pg_backend_pid()").scalar())
                worker_waiting.set()

        provider = FakeSmtp()
        handler = EmailDeliveryJobHandler(store.sync_session, provider, app.state.email_secret_cipher,
            offer_public_base_url=settings.offer_public_base_url)
        with ThreadPoolExecutor(max_workers=1) as pool:
            with store.sync_session.begin() as admin_db:
                admin_db.scalar(select(User).where(User.id == target.user_id).with_for_update())
                event.listen(engine, "before_cursor_execute", before_user_lock)
                try:
                    future = pool.submit(lambda: asyncio.run(handler(job)))
                    assert worker_waiting.wait(timeout=10)
                    reset = admin_db.get(PasswordInvitation, reset_id)
                    reset.expires_at = app.state.identity_service.clock.current_time()
                    admin_db.flush()
                    deadline = time.monotonic() + 10
                    while True:
                        with engine.connect() as connection:
                            blocked = connection.scalar(text("SELECT cardinality(pg_blocking_pids(:pid)) > 0"), {"pid": worker_pid[0]})
                        if blocked or time.monotonic() >= deadline:
                            break
                        time.sleep(0.02)
                    assert blocked, "Worker must wait for lifecycle's user lock"
                finally:
                    event.remove(engine, "before_cursor_execute", before_user_lock)
            future.result(timeout=20)
        assert provider.messages == []
        with store.sync_session() as db:
            assert db.scalar(select(EmailDelivery)).status == "cancelled"

        # A real locked user must not delay public acknowledgement. Only the
        # durable worker waits, then captures the latest committed user version.
        held_target = seed_user(app, role="recruiter", email="held@example.com")
        worker_waiting.clear()
        worker_pid.clear()
        with TestClient(app) as client, ThreadPoolExecutor(max_workers=2) as pool:
            with store.sync_session.begin() as holder:
                held_user = holder.scalar(select(User).where(User.id == held_target.user_id).with_for_update())
                response_future = pool.submit(request_reset, client, "held@example.com", "acme", "192.0.2.90")
                assert response_future.result(timeout=3).status_code == 202
                with store.sync_session() as db:
                    held_request = db.scalar(select(PasswordRecoveryRequest).where(PasswordRecoveryRequest.processed_at.is_(None)))
                    held_job = db.scalar(select(BackgroundJob).where(BackgroundJob.type == RECOVERY_JOB_TYPE,
                        BackgroundJob.payload["request_id"].as_string() == str(held_request.id)))
                event.listen(engine, "before_cursor_execute", before_user_lock)
                try:
                    worker_future = pool.submit(lambda: asyncio.run(recovery_handler(held_job)))
                    assert worker_waiting.wait(timeout=10)
                    held_user.authorization_version += 1
                finally:
                    event.remove(engine, "before_cursor_execute", before_user_lock)
            worker_future.result(timeout=20)
        with store.sync_session() as db:
            token = db.scalar(select(PasswordInvitation).where(PasswordInvitation.user_id == held_target.user_id))
            assert token is not None and token.reset_authorization_version == 2
            assert db.get(PasswordRecoveryRequest, held_request.id).email_ciphertext is None
    finally:
        event.remove(engine, "engine_connect", bounded_queries)
        engine.dispose()
