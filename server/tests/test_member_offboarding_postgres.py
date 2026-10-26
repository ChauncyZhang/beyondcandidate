"""Run from the repo root against an EMPTY disposable MEMBER_OFFBOARDING_POSTGRES_URL.

Uses Alembic, not ORM schema creation. Synthetic data is intentionally retained;
the caller owns database/container disposal. No SMTP, storage or remote providers
are called. Run: python -m pytest server/tests/test_member_offboarding_postgres.py -q
"""

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
import subprocess
import sys
from threading import Event, Lock
import time
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select, text
from sqlalchemy.exc import IntegrityError

from server.app.core.settings import Settings
from server.app.identity.models import (
    AuditLog, Job, PasswordInvitation, User, UserRole, UserSession, UserStatus,
)
from server.app.identity.offboarding_models import MemberOffboarding
from server.app.identity.security import hash_token
from server.app.main import create_app
from server.app.recruiting.models import Application, Candidate
from server.app.screening.models import ScreeningRun
from server.app.talent.models import TalentPool, TalentPoolMembership
from server.tests.test_identity_management import Probe, login, seed_user


pytestmark = pytest.mark.skipif(
    not os.getenv("MEMBER_OFFBOARDING_POSTGRES_URL"),
    reason="Initially empty disposable member-offboarding PostgreSQL URL not configured",
)
ROOT = Path(__file__).resolve().parents[2]


def _bounded_queries(connection):
    connection.exec_driver_sql("SET statement_timeout = '15s'")
    connection.exec_driver_sql("SET lock_timeout = '10s'")
    connection.commit()


def _upgrade(url, revision):
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "-c", "server/alembic.ini", "upgrade", revision],
        cwd=ROOT, env={**os.environ, "DATABASE_URL": url},
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, f"Migration {revision} failed:\n{result.stdout}\n{result.stderr}"


@pytest.fixture(scope="module")
def postgres_app():
    url = os.environ["MEMBER_OFFBOARDING_POSTGRES_URL"]
    engine = create_engine(url.replace("+asyncpg", "+psycopg"))
    assert engine.dialect.name == "postgresql", "This smoke requires PostgreSQL"
    role = f"offboarding_smoke_{uuid4().hex}"
    role_created = False
    app = None
    reservation = engine.connect()
    try:
        # Prevent parallel invocations from both passing the empty-database gate.
        assert reservation.scalar(text("SELECT pg_try_advisory_lock(19500440045)")), "Smoke database already in use"
        objects = reservation.execute(text("""
            SELECT n.nspname, c.relname FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
              AND n.nspname NOT LIKE 'pg_toast%'
              AND n.nspname NOT LIKE 'pg_temp%'
        """)).all()
        assert not objects, "Refusing nonempty database; provide an isolated EMPTY disposable database"
        reservation.commit()
        _upgrade(url, "0044_password_recovery")
        app = create_app(
            Settings(environment="test", database_url=url, cors_origins=["https://hr.example.test"]),
            database_probe=Probe(), storage_probe=Probe(),
        )
        event.listen(app.state.identity_store.engine, "engine_connect", _bounded_queries)
        actor = seed_user(app, role="system_admin", email="legacy-admin@example.test", organization_slug="legacy-smoke")
        source = seed_user(app, role="recruiter", email="legacy-source@example.test", organization_slug="legacy-smoke")
        foreign = seed_user(app, role="recruiter", email="foreign@example.test", organization_slug="foreign-smoke")
        now = app.state.identity_service.clock.current_time()
        with app.state.identity_store.sync_session.begin() as db:
            db.add(UserSession(organization_id=source.organization_id, user_id=source.user_id,
                token_hash=hash_token(f"legacy-session-{uuid4()}"), csrf_token_hash=hash_token("csrf"),
                authorization_version=1, idle_expires_at=now + timedelta(hours=1),
                absolute_expires_at=now + timedelta(hours=2)))
            db.add(PasswordInvitation(organization_id=source.organization_id, user_id=source.user_id,
                token_hash=hash_token(f"legacy-grant-{uuid4()}"), purpose="password_reset",
                reset_authorization_version=1, expires_at=now + timedelta(hours=1)))
            db.add(AuditLog(organization_id=source.organization_id, actor_user_id=source.user_id,
                category="system", event_type="identity.smoke_history", outcome="success",
                resource_type="user", resource_id=source.user_id, trace_id="offboarding-migration-history",
                metadata_json={"historical": True}))
            db.add(Job(organization_id=source.organization_id, owner_id=source.user_id,
                title="Closed historical job", status="closed"))
        preserved_tables = ("users", "user_roles", "user_sessions", "password_invitations", "audit_logs", "jobs")
        with engine.begin() as connection:
            connection.exec_driver_sql(f'CREATE ROLE "{role}" NOLOGIN')
            role_created = True
            connection.exec_driver_sql(f'GRANT USAGE ON SCHEMA public TO "{role}"')
            connection.exec_driver_sql(f'GRANT SELECT, INSERT, UPDATE, DELETE ON users TO "{role}"')
            before = {table: connection.execute(text(f"SELECT * FROM {table} ORDER BY id")).all() for table in preserved_tables}
        _upgrade(url, "0045_member_offboarding")
        yield app, engine, role, before, actor, source, foreign
    finally:
        if app is not None:
            app.state.identity_store.engine.dispose()
        try:
            if role_created:
                with engine.begin() as connection:
                    connection.exec_driver_sql(f'DROP OWNED BY "{role}"')
                    connection.exec_driver_sql(f'DROP ROLE "{role}"')
        finally:
            reservation.close()
            engine.dispose()


def test_0044_to_0045_preserves_history_grants_and_tenant_constraints(postgres_app):
    app, engine, role, before, actor, source, foreign = postgres_app
    with engine.begin() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "0045_member_offboarding"
        width = connection.scalar(text("""SELECT character_maximum_length FROM information_schema.columns
            WHERE table_schema='public' AND table_name='alembic_version' AND column_name='version_num'"""))
        assert width >= len("0045_member_offboarding")
        for table, rows in before.items():
            assert connection.execute(text(f"SELECT * FROM {table} ORDER BY id")).all() == rows, table
        for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
            assert connection.scalar(text("SELECT has_table_privilege(:role, 'public.member_offboardings', :privilege)"),
                {"role": role, "privilege": privilege}), privilege
        # Execute DML as the inherited app role, not merely as the schema owner.
        connection.exec_driver_sql(f'SET LOCAL ROLE "{role}"')
        values = {"id": uuid4(), "org": source.organization_id, "source": source.user_id,
            "successor": actor.user_id, "actor": actor.user_id}
        connection.execute(text("""INSERT INTO member_offboardings
            (id,organization_id,user_id,successor_user_id,removed_by)
            VALUES (:id,:org,:source,:successor,:actor)"""), values)
        assert connection.scalar(text("SELECT status FROM member_offboardings WHERE id=:id"), values) == "pending"
        connection.execute(text("UPDATE member_offboardings SET status='completed',version=2 WHERE id=:id"), values)
        assert connection.execute(text("SELECT status,version FROM member_offboardings WHERE id=:id"), values).one() == ("completed", 2)
        assert connection.execute(text("DELETE FROM member_offboardings WHERE id=:id"), values).rowcount == 1

    for successor, sqlstate in ((foreign.user_id, "23503"), (source.user_id, "23514")):
        with app.state.identity_store.sync_session() as db:
            db.add(MemberOffboarding(organization_id=source.organization_id, user_id=source.user_id,
                successor_user_id=successor, removed_by=actor.user_id))
            with pytest.raises(IntegrityError) as error:
                db.flush()
            assert error.value.orig.sqlstate == sqlstate
            db.rollback()
    with app.state.identity_store.sync_session() as db:
        assert db.scalar(select(MemberOffboarding).where(MemberOffboarding.user_id == source.user_id)) is None


def _preview(client, user_id, successor_id=None):
    response = client.get(f"/api/v1/settings/users/{user_id}/offboarding",
        params={"successor_id": str(successor_id)} if successor_id else {})
    assert response.status_code == 200, response.text
    return response.json()["data"]["preview_version"]


def _remove(client, user_id, headers, version, successor_id=None):
    return client.post(f"/api/v1/settings/users/{user_id}/offboarding", headers=headers,
        json={"preview_version": version, "successor_user_id": str(successor_id) if successor_id else None,
            "revoke_only": successor_id is None})


def _wait_for_blocked(engine, pid, blocker_pid):
    deadline = time.monotonic() + 8
    with engine.connect() as connection:
        while time.monotonic() < deadline:
            # Tuple-lock waiters may queue behind each other before reaching
            # the transaction holding the row; accept that real blocker chain.
            if connection.scalar(text("""WITH RECURSIVE blockers(pid) AS (
                    SELECT unnest(pg_blocking_pids(:pid))
                    UNION
                    SELECT unnest(pg_blocking_pids(blockers.pid)) FROM blockers
                ) SELECT EXISTS(SELECT 1 FROM blockers WHERE pid=:blocker)"""),
                {"blocker": blocker_pid, "pid": pid}):
                return
        state = connection.execute(text("SELECT pid,state,wait_event_type,wait_event FROM pg_stat_activity WHERE pid=:pid"), {"pid": pid}).all()
    pytest.fail(f"Backend {pid} never blocked on {blocker_pid}; activity={state}")


def test_two_admins_cannot_concurrently_remove_the_last_admin(postgres_app):
    app, engine, _, _, _, _, _ = postgres_app
    store = app.state.identity_store
    slug = f"admin-race-{uuid4().hex}"
    admins = [seed_user(app, role="system_admin", email=f"admin-{index}@example.test", organization_slug=slug) for index in range(2)]
    waiting = Event()
    mutex = Lock()
    pids = set()

    def observe_organization_lock(connection, cursor, statement, parameters, context, many):
        if "FROM organizations" in statement and "FOR UPDATE" in statement:
            pid = connection.exec_driver_sql("SELECT pg_backend_pid()").scalar()
            with mutex:
                pids.add(pid)
                if len(pids) == 2:
                    waiting.set()

    with TestClient(app) as first, TestClient(app) as second:
        clients = [first, second]
        headers = [login(client, f"admin-{index}@example.test", slug) for index, client in enumerate(clients)]
        versions = [_preview(client, admins[1-index].user_id) for index, client in enumerate(clients)]
        with ThreadPoolExecutor(max_workers=2) as pool:
            with store.sync_session.begin() as holder:
                holder.scalar(select(User).where(User.id == admins[0].user_id))
                holder.execute(text("SELECT id FROM organizations WHERE id=:id FOR UPDATE"), {"id": admins[0].organization_id})
                holder_pid = holder.scalar(text("SELECT pg_backend_pid()"))
                event.listen(store.engine, "before_cursor_execute", observe_organization_lock)
                try:
                    futures = [pool.submit(_remove, client, admins[1-index].user_id, headers[index], versions[index]) for index, client in enumerate(clients)]
                    assert waiting.wait(timeout=8), "Both authenticated removals must reach the organization lock"
                    for pid in tuple(pids):
                        _wait_for_blocked(engine, pid, holder_pid)
                finally:
                    event.remove(store.engine, "before_cursor_execute", observe_organization_lock)
            responses = [future.result(timeout=20) for future in futures]
        codes = [response.status_code for response in responses]
        assert codes.count(200) == 1 and next(code for code in codes if code != 200) in {401, 409}, [response.text for response in responses]
        winner = codes.index(200)
        removed_id = admins[1-winner].user_id
        same_origin = {"Sec-Fetch-Site": "same-origin"}
        assert clients[1-winner].get("/api/v1/me", headers=same_origin).status_code == 401
        assert clients[winner].get("/api/v1/me", headers=same_origin).status_code == 200
    with store.sync_session() as db:
        active_admins = db.scalars(select(User.id).where(User.organization_id == admins[0].organization_id,
            User.status == UserStatus.ACTIVE, User.roles.any(UserRole.role == "system_admin"))).all()
        assert active_admins == [admins[winner].user_id]
        assert db.get(User, removed_id).status == UserStatus.DISABLED
        assert len(db.scalars(select(MemberOffboarding).where(MemberOffboarding.organization_id == admins[0].organization_id)).all()) == 1
        assert all(session.revoked_at is not None for session in db.scalars(select(UserSession).where(UserSession.user_id == removed_id)))


def test_talent_producer_retries_user_lock_then_creates_only_active_ownership(postgres_app, monkeypatch):
    from server.app.identity import offboarding_handover
    from server.app.identity.offboarding_resolver import OperationalOwnerUnavailable
    from server.app.talent.service import ensure_deferred_membership
    from server.tests.test_screening_routing import seed_routing_case

    app, engine, _, _, _, _, _ = postgres_app
    store = app.state.identity_store
    case = seed_routing_case(app, suffix=f"offboard-{uuid4().hex}")
    actor = seed_user(app, role="system_admin", email="producer-actor@example.test")
    locked, release = Event(), Event()
    real_build = offboarding_handover.build_handover_plan

    def pause_after_user_locks(*args, **kwargs):
        if kwargs.get("lock"):
            locked.set()
            assert release.wait(timeout=10), "Test did not release offboarding User locks"
        return real_build(*args, **kwargs)

    def produce():
        with store.sync_session.begin() as db:
            membership = ensure_deferred_membership(db,
                application=db.get(Application, case.application_id), candidate=db.get(Candidate, case.candidate_id),
                job=db.get(Job, case.job_id), run=db.get(ScreeningRun, case.run_id), score=45)
            db.flush()
            return membership.id

    with TestClient(app) as client:
        headers = login(client, "producer-actor@example.test")
        version = _preview(client, case.creator_id, case.admin_id)
        monkeypatch.setattr(offboarding_handover, "build_handover_plan", pause_after_user_locks)
        with ThreadPoolExecutor(max_workers=2) as pool:
            future = pool.submit(_remove, client, case.creator_id, headers, version, case.admin_id)
            try:
                assert locked.wait(timeout=8), "Offboarding never reached locked planning"
                producer = pool.submit(produce)
                with pytest.raises(OperationalOwnerUnavailable) as error:
                    producer.result(timeout=5)
                # A real PostgreSQL NOWAIT conflict, not an artificial mock failure.
                assert error.value.__context__.orig.sqlstate == "55P03"
                with store.sync_session() as db:
                    assert db.scalar(select(TalentPoolMembership).where(TalentPoolMembership.candidate_id == case.candidate_id)) is None
            finally:
                release.set()
            result = future.result(timeout=20)
            assert result.status_code == 200, result.text
        membership_id = produce()
    with store.sync_session() as db:
        assert db.get(User, case.creator_id).status == UserStatus.DISABLED
        membership = db.get(TalentPoolMembership, membership_id)
        assert membership.owner_id == case.admin_id
        pool = db.get(TalentPool, membership.pool_id)
        assert db.get(User, pool.owner_id).status == UserStatus.ACTIVE
        assert db.get(User, membership.owner_id).status == UserStatus.ACTIVE
        assert db.get(ScreeningRun, case.run_id).created_by == case.creator_id
        assert db.get(MemberOffboarding, db.scalar(select(MemberOffboarding.id).where(MemberOffboarding.user_id == case.creator_id))).removed_by == actor.user_id
