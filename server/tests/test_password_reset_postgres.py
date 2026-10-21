"""Run only against a disposable, initially empty POSTGRES_SMOKE_URL database."""

import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier, Event
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.exc import IntegrityError

from server.app.identity.models import PasswordInvitation, User, UserSession, UserStatus
from server.app.identity.security import PasswordService, hash_token
from server.app.identity.service import Clock, IdentityService, InvalidSession, PasswordResetInvalidOrExpired, TokenSource
from server.app.identity.store import IdentityStore


@pytest.mark.skipif(not os.getenv("POSTGRES_SMOKE_URL"), reason="Disposable PostgreSQL smoke URL not configured")
def test_password_reset_upgrade_preserves_invitations_grants_and_serializes_consumption(monkeypatch):
    url = os.environ["POSTGRES_SMOKE_URL"]
    engine = create_engine(url.replace("+asyncpg", "+psycopg"))
    # Fail closed rather than downgrading or truncating an existing environment.
    assert inspect(engine).get_table_names() == [], "Use an isolated empty smoke database"
    env = {**os.environ, "DATABASE_URL": url}

    def upgrade(revision):
        subprocess.run([sys.executable, "-m", "alembic", "-c", "server/alembic.ini", "upgrade", revision], env=env, check=True, timeout=120, capture_output=True, text=True)

    organization_id, user_id, invitation_id = uuid4(), uuid4(), uuid4()
    values = {"organization": organization_id, "user": user_id, "invitation": invitation_id, "digest": hash_token("legacy-invitation-token-value")}
    try:
        upgrade("0042_offer_attachments")
        with engine.begin() as connection:
            connection.execute(text("INSERT INTO organizations(id,slug,name,status,retention_policy_id,created_at,updated_at) VALUES(:organization,'reset-smoke','Reset Smoke','active',:organization,now(),now())"), values)
            connection.execute(text("INSERT INTO users(id,organization_id,email,normalized_email,display_name,password_hash,status,authorization_version,failed_login_count,created_at,updated_at) VALUES(:user,:organization,'reset@example.test','reset@example.test','Reset Smoke','placeholder','ACTIVE',1,0,now(),now())"), values)
            connection.execute(text("INSERT INTO password_invitations(id,organization_id,user_id,token_hash,expires_at,created_at) VALUES(:invitation,:organization,:user,:digest,now()+interval '48 hours',now())"), values)
            before = connection.execute(text("SELECT id,token_hash,expires_at,used_at FROM password_invitations WHERE id=:invitation"), values).one()
            grants = connection.scalar(text("SELECT relacl::text FROM pg_class WHERE oid='public.password_invitations'::regclass"))
        upgrade("head")
        with engine.begin() as connection:
            assert connection.execute(text("SELECT id,token_hash,expires_at,used_at FROM password_invitations WHERE id=:invitation"), values).one() == before
            assert connection.scalar(text("SELECT purpose FROM password_invitations WHERE id=:invitation"), values) == "invitation"
            assert connection.scalar(text("SELECT reset_authorization_version FROM password_invitations WHERE id=:invitation"), values) is None
            assert connection.scalar(text("SELECT relacl::text FROM pg_class WHERE oid='public.password_invitations'::regclass")) == grants
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "0043_password_reset"
            width = connection.scalar(text("SELECT character_maximum_length FROM information_schema.columns WHERE table_name='alembic_version' AND column_name='version_num'"))
            assert len("0043_password_reset") <= width
            values["new"] = uuid4()
            values["new_digest"] = hash_token("new-legacy-invitation-token")
            connection.execute(text("INSERT INTO password_invitations(id,organization_id,user_id,token_hash,expires_at,created_at) VALUES(:new,:organization,:user,:new_digest,now()+interval '48 hours',now())"), values)
            assert connection.scalar(text("SELECT purpose FROM password_invitations WHERE id=:new"), values) == "invitation"
        with engine.begin() as connection:
            with pytest.raises(IntegrityError):
                connection.execute(text("UPDATE password_invitations SET purpose='other' WHERE id=:invitation"), values)

        store = IdentityStore(url)
        service = IdentityService(store, Clock(), TokenSource())
        try:
            # Distinct valid siblings racing must not both replace the password.
            for same_token in (True, False):
                now = datetime.now(timezone.utc)
                tokens = [f"reset-smoke-{uuid4()}"]
                if not same_token:
                    tokens.append(f"reset-smoke-{uuid4()}")
                with store.sync_session() as db:
                    user = db.get(User, user_id)
                    user.password_hash = PasswordService().hash("previous secure password")
                    version = user.authorization_version
                    user.status = UserStatus.ACTIVE
                    for token in tokens:
                        db.add(PasswordInvitation(organization_id=organization_id, user_id=user_id, token_hash=hash_token(token), purpose="password_reset", reset_authorization_version=version, expires_at=now + timedelta(hours=1)))
                    db.add(UserSession(organization_id=organization_id, user_id=user_id, token_hash=hash_token(f"session-{uuid4()}"), csrf_token_hash=hash_token("csrf"), authorization_version=version, idle_expires_at=now + timedelta(hours=1), absolute_expires_at=now + timedelta(hours=2)))
                    db.commit()
                barrier = Barrier(2)

                def consume(token):
                    barrier.wait(timeout=10)
                    try:
                        service.accept_password_reset(token, "replacement secure password", trace_id="reset-concurrency-smoke")
                        return "accepted"
                    except PasswordResetInvalidOrExpired:
                        return "invalid"

                with ThreadPoolExecutor(max_workers=2) as pool:
                    futures = [pool.submit(consume, tokens[0]), pool.submit(consume, tokens[-1])]
                    assert sorted(f.result(timeout=30) for f in futures) == ["accepted", "invalid"]
                with store.sync_session() as db:
                    assert db.get(User, user_id).authorization_version == version + 1
                    assert all(s.revoked_at is not None for s in db.scalars(select(UserSession).where(UserSession.user_id == user_id)))
                    assert db.get(PasswordInvitation, invitation_id).used_at is None
                    assert db.get(PasswordInvitation, invitation_id).purpose == "invitation"

            # Hold the reset's user lock while change_password caches its old
            # session/user, then prove the change actually waits in PostgreSQL.
            now = datetime.now(timezone.utc)
            reset_token, session_token = f"reset-{uuid4()}", f"session-{uuid4()}"
            with store.sync_session() as db:
                user = db.get(User, user_id)
                user.password_hash = service.passwords.hash("previous secure password")
                version = user.authorization_version
                db.add(PasswordInvitation(organization_id=organization_id, user_id=user_id, token_hash=hash_token(reset_token), purpose="password_reset", reset_authorization_version=version, expires_at=now + timedelta(hours=1)))
                db.add(UserSession(organization_id=organization_id, user_id=user_id, token_hash=hash_token(session_token), csrf_token_hash=hash_token("csrf"), authorization_version=version, idle_expires_at=now + timedelta(hours=1), absolute_expires_at=now + timedelta(hours=2)))
                db.commit()
            reset_has_lock, finish_reset, change_resolved = Event(), Event(), Event()
            change_pid = []
            real_hash = service.passwords.hash
            real_resolve = service._resolve_session

            def hash_while_holding_reset_lock(password):
                if password == "reset winner secure password":
                    reset_has_lock.set()
                    assert finish_reset.wait(timeout=15)
                return real_hash(password)

            def resolve_and_record_backend(db, token, **kwargs):
                record = real_resolve(db, token, **kwargs)
                if not change_resolved.is_set():
                    change_pid.append(db.scalar(text("SELECT pg_backend_pid()")))
                    change_resolved.set()
                return record

            monkeypatch.setattr(service.passwords, "hash", hash_while_holding_reset_lock)
            monkeypatch.setattr(service, "_resolve_session", resolve_and_record_backend)
            with ThreadPoolExecutor(max_workers=2) as pool:
                reset_future = pool.submit(service.accept_password_reset, reset_token, "reset winner secure password", trace_id="postgres-reset-interleave")
                try:
                    assert reset_has_lock.wait(timeout=10)
                    change_future = pool.submit(service.change_password, session_token, "previous secure password", "stale session overwrite", trace_id="postgres-change-interleave", network=None)
                    assert change_resolved.wait(timeout=10)
                    deadline = time.monotonic() + 10
                    while True:
                        with engine.connect() as connection:
                            blocked = connection.scalar(text("SELECT cardinality(pg_blocking_pids(:pid)) > 0"), {"pid": change_pid[0]})
                        if blocked or time.monotonic() >= deadline:
                            break
                        time.sleep(0.02)
                    assert blocked, "Password change must wait on the reset's user lock"
                finally:
                    finish_reset.set()
                assert reset_future.result(timeout=20)["email"] == "reset@example.test"
                with pytest.raises(InvalidSession):
                    change_future.result(timeout=20)
            with store.sync_session() as db:
                user = db.get(User, user_id)
                assert user.authorization_version == version + 1
                assert service.passwords.verify(user.password_hash, "reset winner secure password")
                assert not service.passwords.verify(user.password_hash, "stale session overwrite")
                assert db.scalar(select(UserSession).where(UserSession.token_hash == hash_token(session_token))).revoked_at is not None
        finally:
            store.engine.dispose()
    finally:
        engine.dispose()
