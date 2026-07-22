from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from threading import Barrier, Event
from datetime import datetime, timedelta, timezone
import os
import subprocess
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, text
from sqlalchemy.exc import IntegrityError, OperationalError

from server.app.core.settings import Settings
from server.app.identity.models import Job, Organization, User, UserRole, UserStatus
from server.app.identity.security import PasswordService
from server.app.identity.service import AuthenticationFailed, Clock, IdentityService, TokenSource
from server.app.identity.store import IdentityStore
from server.app.recruiting.models import CandidateEvent, IdempotencyRecord, JobJdVersion, ScreeningRuleVersion
from server.app.recruiting.service import (IdempotencyConflict, ResourceVersionConflict, TicketInvalid, consume_download_ticket_record, issue_download_ticket_record, persisted_idempotent, transition_application_record, transition_job_record, patch_application_record, patch_candidate_record, patch_job_record)
from server.app.main import create_app


pytestmark = pytest.mark.skipif(not os.getenv("POSTGRES_SMOKE_URL"), reason="PostgreSQL smoke URL not configured")


@pytest.fixture
def pg_store():
    url = os.environ["POSTGRES_SMOKE_URL"]
    env = {**os.environ, "DATABASE_URL": url}
    subprocess.run(["python", "-m", "alembic", "-c", "server/alembic.ini", "upgrade", "head"], check=True, env=env)
    store = IdentityStore(url)
    yield store
    subprocess.run(["python", "-m", "alembic", "-c", "server/alembic.ini", "downgrade", "base"], check=True, env=env)


def seed_pg_user(store, slug="concurrent"):
    with store.sync_session() as db:
        organization = Organization(slug=slug, name="Concurrent", status="active")
        user = User(organization=organization, email="user@example.test", normalized_email="user@example.test", display_name="User", password_hash=PasswordService().hash("correct"), status=UserStatus.ACTIVE)
        db.add(user)
        db.commit()
        return user.id, organization.id


def seed_recruiting_graph(store, slug="concurrent"):
    user_id, organization_id = seed_pg_user(store, slug)
    ids = {name: uuid4() for name in ("candidate", "job", "file", "resume", "application")}
    with store.engine.begin() as connection:
        connection.execute(text("insert into candidates(id,organization_id,display_name,owner_id) values (:candidate,:org,'Candidate',:user)"), {**ids, "org": organization_id, "user": user_id})
        connection.execute(text("insert into jobs(id,organization_id,title,owner_id,status,created_at,updated_at) values (:job,:org,'Role',:user,'draft',now(),now())"), {**ids, "org": organization_id, "user": user_id})
        connection.execute(text("insert into file_objects(id,organization_id,storage_key,original_filename,mime_type,size_bytes,sha256,uploaded_by) values (:file,:org,:key,'resume.pdf','application/pdf',1,:sha,:user)"), {**ids, "org": organization_id, "user": user_id, "key": str(ids["file"]), "sha": "1" * 64})
        connection.execute(text("insert into resumes(id,organization_id,candidate_id,file_object_id,version_number) values (:resume,:org,:candidate,:file,1)"), {**ids, "org": organization_id})
        connection.execute(text("insert into applications(id,organization_id,candidate_id,job_id,resume_id,owner_id,stage) values (:application,:org,:candidate,:job,:resume,:user,'new')"), {**ids, "org": organization_id, "user": user_id})
    return organization_id, user_id, ids


class FixedRecruitingClock:
    def __init__(self): self.now = datetime(2026, 7, 12, tzinfo=timezone.utc)
    def current_time(self): return self.now


class FixedToken:
    def __init__(self, value): self.value = value
    def new_token(self): return self.value


class Probe:
    async def check(self):
        pass


def job_definition_payload(**changes):
    payload = {"title": "Concurrent role", "department_id": None, "headcount": 1, "priority": "normal", "hiring_owner_id": None, "description": "Concurrent JD", "location": "Remote", "process_template": "standard", "llm_enabled": False, "must_have": ["Python"], "nice_to_have": [], "publish": False}
    payload.update(changes)
    return payload


def seed_pg_admin(store, slug):
    user_id, organization_id = seed_pg_user(store, slug)
    with store.sync_session() as db:
        db.add(UserRole(user_id=user_id, role="recruiting_admin")); db.commit()
    return user_id, organization_id


def pg_app_and_headers(pg_store, slug):
    seed_pg_admin(pg_store, slug)
    app = create_app(settings=Settings(environment="test", database_url=os.environ["POSTGRES_SMOKE_URL"], cors_origins=["https://hr.example.test"]), database_probe=Probe(), storage_probe=Probe())
    client = TestClient(app, raise_server_exceptions=False)
    login = client.post("/api/v1/auth/login", json={"organization_slug": slug, "email": "user@example.test", "password": "correct"}, headers={"Origin": "https://hr.example.test"})
    assert login.status_code == 200
    return app, client, {"Origin": "https://hr.example.test", "X-CSRF-Token": login.headers["X-CSRF-Token"]}


def test_postgres_two_puts_with_same_version_return_success_and_conflict_without_500(pg_store) -> None:
    app, client, headers = pg_app_and_headers(pg_store, "job-definition-two-put")
    created = client.post("/api/v1/job-definitions", json=job_definition_payload(), headers={**headers, "Idempotency-Key": "create-two-put"})
    job_id = created.json()["data"]["job"]["id"]
    barrier = Barrier(2, timeout=5)

    def replace(index):
        barrier.wait()
        return client.put(f"/api/v1/job-definitions/{job_id}", json=job_definition_payload(title=f"Concurrent {index}"), headers={**headers, "If-Match": '"1"', "Idempotency-Key": f"replace-{index}"})

    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(replace, (1, 2)))
    assert sorted(response.status_code for response in responses) == [200, 409]
    assert all(response.status_code != 500 for response in responses)
    assert next(response for response in responses if response.status_code == 409).json()["code"] == "resource_version_conflict"
    job_uuid = UUID(job_id)
    with pg_store.sync_session() as db:
        assert db.get(Job, job_uuid).version == 2
        assert db.query(JobJdVersion).filter_by(job_id=job_uuid).count() == 2
        assert db.query(ScreeningRuleVersion).filter_by(job_id=job_uuid).count() == 2
    client.close()


@pytest.mark.parametrize(("legacy_path", "table_name"), [("jd-versions", "job_jd_versions"), ("rule-versions", "screening_rule_versions")])
def test_postgres_put_and_legacy_version_write_share_job_lock_without_500(pg_store, legacy_path, table_name) -> None:
    app, client, headers = pg_app_and_headers(pg_store, f"job-definition-mixed-{legacy_path}")
    created = client.post("/api/v1/job-definitions", json=job_definition_payload(), headers={**headers, "Idempotency-Key": "create-mixed"})
    job_id = created.json()["data"]["job"]["id"]
    old_read_max = Event()
    release_old = Event()
    observed_legacy_sql = []

    def pause_legacy_after_max(connection, cursor, statement, parameters, context, executemany):
        normalized = " ".join(statement.lower().split())
        observed_legacy_sql.append(normalized)
        if not old_read_max.is_set() and "max(" in normalized and f"{table_name}.version_number" in normalized:
            old_read_max.set()
            assert release_old.wait(timeout=5)

    event.listen(app.state.identity_store.engine, "after_cursor_execute", pause_legacy_after_max)
    try:
        def legacy_write():
            return client.post(f"/api/v1/jobs/{job_id}/{legacy_path}", json={"content": {"legacy": "concurrent"}}, headers=headers)

        def replacement():
            return client.put(f"/api/v1/job-definitions/{job_id}", json=job_definition_payload(description="replacement concurrent"), headers={**headers, "If-Match": '"1"', "Idempotency-Key": "mixed-replace"})

        with ThreadPoolExecutor(max_workers=2) as executor:
            old_future = executor.submit(legacy_write)
            assert old_read_max.wait(timeout=5), observed_legacy_sql
            put_future = executor.submit(replacement)
            try:
                put_future.result(timeout=0.5)
            except FutureTimeout:
                pass
            finally:
                release_old.set()
            responses = [old_future.result(timeout=5), put_future.result(timeout=5)]
    finally:
        release_old.set()
        event.remove(app.state.identity_store.engine, "after_cursor_execute", pause_legacy_after_max)
        client.close()
    assert sorted(response.status_code for response in responses) == [200, 201]
    assert all(response.status_code != 500 for response in responses)
    job_uuid = UUID(job_id)
    with pg_store.sync_session() as db:
        jd_versions = [row.version_number for row in db.query(JobJdVersion).filter_by(job_id=job_uuid).order_by(JobJdVersion.version_number)]
        rule_versions = [row.version_number for row in db.query(ScreeningRuleVersion).filter_by(job_id=job_uuid).order_by(ScreeningRuleVersion.version_number)]
        assert (jd_versions, rule_versions) == (([1, 2, 3], [1, 2]) if legacy_path == "jd-versions" else ([1, 2], [1, 2, 3]))


def test_postgres_first_same_idempotency_key_concurrency_replays_one_create(pg_store) -> None:
    app, client, headers = pg_app_and_headers(pg_store, "job-definition-idempotency")
    barrier = Barrier(2, timeout=5)

    def create(_):
        barrier.wait()
        return client.post("/api/v1/job-definitions", json=job_definition_payload(), headers={**headers, "Idempotency-Key": "same-first-key"})

    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(create, (1, 2)))
    assert [response.status_code for response in responses] == [201, 201]
    assert responses[0].json() == responses[1].json()
    assert all(response.status_code != 500 for response in responses)
    with pg_store.sync_session() as db:
        assert db.query(Job).count() == 1
        assert db.query(JobJdVersion).count() == 1
        assert db.query(ScreeningRuleVersion).count() == 1
        assert db.query(IdempotencyRecord).filter_by(idempotency_key="same-first-key").count() == 1
    client.close()


def test_postgres_api_applies_recruiter_scope_inside_job_candidate_and_funnel_queries(pg_store) -> None:
    with pg_store.sync_session() as db:
        organization = Organization(slug="api-scope", name="API Scope", status="active")
        recruiter = User(organization=organization, email="recruiter@example.test", normalized_email="recruiter@example.test", display_name="Recruiter", password_hash=PasswordService().hash("correct"), status=UserStatus.ACTIVE)
        recruiter.roles.append(UserRole(role="recruiter"))
        outsider = User(organization=organization, email="other@example.test", normalized_email="other@example.test", display_name="Other", password_hash=PasswordService().hash("correct"), status=UserStatus.ACTIVE)
        outsider.roles.append(UserRole(role="recruiter"))
        db.add_all([recruiter, outsider]); db.commit()
        organization_id, outsider_id = organization.id, outsider.id

    app = create_app(
        settings=Settings(environment="test", database_url=os.environ["POSTGRES_SMOKE_URL"], cors_origins=["https://hr.example.test"]),
        database_probe=Probe(), storage_probe=Probe(),
    )
    with TestClient(app) as client:
        login = client.post("/api/v1/auth/login", json={"organization_slug": "api-scope", "email": "recruiter@example.test", "password": "correct"}, headers={"Origin": "https://hr.example.test"})
        headers = {"Origin": "https://hr.example.test", "X-CSRF-Token": login.headers["X-CSRF-Token"]}
        owned_job = client.post("/api/v1/jobs", json={"title": "Owned"}, headers=headers).json()["data"]
        owned_candidate = client.post("/api/v1/candidates", json={"display_name": "Owned Candidate"}, headers=headers).json()["data"]

        with pg_store.engine.begin() as connection:
            unauthorized_job, unauthorized_candidate, unauthorized_file, unauthorized_resume = uuid4(), uuid4(), uuid4(), uuid4()
            connection.execute(text("insert into jobs(id,organization_id,title,owner_id,status,created_at,updated_at) values (:id,:org,'Hidden',:owner,'open',now(),now())"), {"id": unauthorized_job, "org": organization_id, "owner": outsider_id})
            connection.execute(text("insert into candidates(id,organization_id,display_name,owner_id) values (:id,:org,'Hidden Candidate',:owner)"), {"id": unauthorized_candidate, "org": organization_id, "owner": outsider_id})
            connection.execute(text("insert into file_objects(id,organization_id,storage_key,original_filename,mime_type,size_bytes,sha256,uploaded_by) values (:id,:org,:key,'hidden.pdf','application/pdf',1,:sha,:owner)"), {"id": unauthorized_file, "org": organization_id, "key": str(unauthorized_file), "sha": "4" * 64, "owner": outsider_id})
            connection.execute(text("insert into resumes(id,organization_id,candidate_id,file_object_id,version_number) values (:id,:org,:candidate,:file,1)"), {"id": unauthorized_resume, "org": organization_id, "candidate": unauthorized_candidate, "file": unauthorized_file})

        jobs = client.get("/api/v1/jobs").json()["data"]
        candidates = client.get("/api/v1/candidates?q=Candidate").json()["data"]
        assert [row["id"] for row in jobs] == [owned_job["id"]]
        assert [row["id"] for row in candidates] == [owned_candidate["id"]]
        assert client.get(f"/api/v1/jobs/{unauthorized_job}/funnel").status_code == 404

        before = pg_store.engine.connect().execute(text("select count(*) from applications")).scalar_one()
        attack = client.post(f"/api/v1/jobs/{owned_job['id']}/applications", json={"candidate_id": str(unauthorized_candidate), "resume_id": str(unauthorized_resume)}, headers={**headers, "Idempotency-Key": "takeover"})
        assert attack.status_code == 404
        with pg_store.engine.connect() as connection:
            assert connection.execute(text("select count(*) from applications")).scalar_one() == before
            assert connection.execute(text("select count(*) from idempotency_records where idempotency_key='takeover'")).scalar_one() == 0
            assert connection.execute(text("select count(*) from candidate_events where candidate_id=:candidate"), {"candidate": unauthorized_candidate}).scalar_one() == 0

        with pg_store.engine.begin() as connection:
            owned_file, owned_resume = uuid4(), uuid4()
            connection.execute(text("insert into file_objects(id,organization_id,storage_key,original_filename,mime_type,size_bytes,sha256,uploaded_by) values (:id,:org,:key,'owned.pdf','application/pdf',1,:sha,:owner)"), {"id": owned_file, "org": organization_id, "key": str(owned_file), "sha": "5" * 64, "owner": recruiter.id})
            connection.execute(text("insert into resumes(id,organization_id,candidate_id,file_object_id,version_number) values (:id,:org,:candidate,:file,1)"), {"id": owned_resume, "org": organization_id, "candidate": owned_candidate["id"], "file": owned_file})
            connection.execute(text("insert into applications(id,organization_id,candidate_id,job_id,resume_id,owner_id,stage,source) values (:id,:org,:candidate,:job,:resume,:owner,'new','visible')"), {"id": uuid4(), "org": organization_id, "candidate": owned_candidate["id"], "job": owned_job["id"], "resume": owned_resume, "owner": recruiter.id})
            connection.execute(text("insert into applications(id,organization_id,candidate_id,job_id,resume_id,owner_id,stage,source) values (:id,:org,:candidate,:job,:resume,:owner,'rejected','hidden-source')"), {"id": uuid4(), "org": organization_id, "candidate": owned_candidate["id"], "job": unauthorized_job, "resume": owned_resume, "owner": outsider_id})
        assert client.get("/api/v1/candidates?stage=rejected").json()["data"] == []
        assert client.get("/api/v1/candidates?source=hidden-source").json()["data"] == []
        assert client.get(f"/api/v1/candidates?job_id={unauthorized_job}").json()["data"] == []

        other_org, other_user, other_ids = seed_recruiting_graph(pg_store, "api-cross-org")
        raw_cross_org_ticket = "cross-org-ticket-value-long-enough"
        with pg_store.sync_session() as db:
            issue_download_ticket_record(db, other_org, other_user, other_ids["resume"], FixedRecruitingClock(), FixedToken(raw_cross_org_ticket))
            db.commit()
        cross_org_denials = [
            client.get(f"/api/v1/jobs/{other_ids['job']}"),
            client.get(f"/api/v1/jobs/{other_ids['job']}/funnel"),
            client.get(f"/api/v1/candidates/{other_ids['candidate']}"),
            client.get(f"/api/v1/candidates/{other_ids['candidate']}/applications"),
            client.get(f"/api/v1/resumes/{other_ids['resume']}/preview"),
            client.post(f"/api/v1/resumes/{other_ids['resume']}/download-tickets", headers=headers),
            client.patch(f"/api/v1/applications/{other_ids['application']}", json={"human_conclusion": "takeover"}, headers={**headers, "If-Match": '"1"'}),
            client.post("/api/v1/download-tickets/consume", json={"token": raw_cross_org_ticket}, headers=headers),
        ]
        assert all(response.status_code == 404 and response.json()["code"] == "resource_not_found" for response in cross_org_denials)


def test_five_concurrent_failures_reliably_lock_account(pg_store) -> None:
    user_id, _ = seed_pg_user(pg_store)

    def fail_login(_):
        service = IdentityService(pg_store, Clock(), TokenSource())
        with pytest.raises(AuthenticationFailed):
            service.login("concurrent", "user@example.test", "wrong", trace_id=str(uuid4()), network="proxy")

    with ThreadPoolExecutor(max_workers=5) as executor:
        list(executor.map(fail_login, range(5)))
    with pg_store.sync_session() as db:
        user = db.get(User, user_id)
        assert user.failed_login_count == 5
        assert user.locked_until > datetime.now(timezone.utc)


def test_concurrent_success_cannot_overwrite_failed_attempt(pg_store) -> None:
    user_id, _ = seed_pg_user(pg_store)

    def login(password):
        service = IdentityService(pg_store, Clock(), TokenSource())
        try:
            service.login("concurrent", "user@example.test", password, trace_id=str(uuid4()), network="proxy")
        except AuthenticationFailed:
            pass

    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(login, ["correct", "wrong"]))
    with pg_store.sync_session() as db:
        user = db.get(User, user_id)
        assert user.failed_login_count in {0, 1}
        assert db.execute(text("select count(*) from audit_logs where event_type='authentication.login'")).scalar_one() == 2


def test_independent_users_in_one_organization_do_not_share_login_lock(pg_store) -> None:
    first_id, organization_id = seed_pg_user(pg_store)
    with pg_store.sync_session() as db:
        second = User(organization_id=organization_id, email="second@example.test", normalized_email="second@example.test", display_name="Second", password_hash=PasswordService().hash("correct"), status=UserStatus.ACTIVE)
        db.add(second)
        db.commit()
        assert second.id != first_id

    barrier = Barrier(2, timeout=5)

    class BarrierPasswords:
        def verify(self, encoded, password):
            barrier.wait()
            return False

    def fail(email):
        service = IdentityService(pg_store, Clock(), TokenSource())
        service.passwords = BarrierPasswords()
        with pytest.raises(AuthenticationFailed):
            service.login("concurrent", email, "wrong", trace_id=str(uuid4()), network="proxy")

    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(fail, ["user@example.test", "second@example.test"]))


def test_cross_organization_relationships_are_rejected(pg_store) -> None:
    with pg_store.engine.begin() as connection:
        org1, org2, dep1, user1, user2, job1 = [uuid4() for _ in range(6)]
        now = datetime.now(timezone.utc)
        connection.execute(text("insert into organizations(id,slug,name,status,created_at,updated_at) values (:id,'o1','O1','active',:n,:n),(:id2,'o2','O2','active',:n,:n)"), {"id": org1, "id2": org2, "n": now})
        connection.execute(text("insert into departments(id,organization_id,name,created_at,updated_at) values (:id,:org,'D',:n,:n)"), {"id": dep1, "org": org1, "n": now})
        password = PasswordService().hash("x")
        connection.execute(text("insert into users(id,organization_id,email,normalized_email,display_name,password_hash,status,authorization_version,failed_login_count,created_at,updated_at) values (:u1,:o1,'a@x','a@x','A',:p,'ACTIVE',1,0,:n,:n),(:u2,:o2,'b@x','b@x','B',:p,'ACTIVE',1,0,:n,:n)"), {"u1": user1, "u2": user2, "o1": org1, "o2": org2, "p": password, "n": now})
        connection.execute(text("insert into jobs(id,organization_id,title,owner_id,status,created_at,updated_at) values (:j,:o1,'J',:u1,'draft',:n,:n)"), {"j": job1, "o1": org1, "u1": user1, "n": now})
    invalid = [
        ("insert into users(id,organization_id,department_id,email,normalized_email,display_name,password_hash,status,authorization_version,failed_login_count,created_at,updated_at) values (:id,:o2,:d,'c@x','c@x','C','x','ACTIVE',1,0,now(),now())", {"id": uuid4(), "o2": org2, "d": dep1}),
        ("insert into jobs(id,organization_id,title,owner_id,status,created_at,updated_at) values (:id,:o2,'bad',:u1,'draft',now(),now())", {"id": uuid4(), "o2": org2, "u1": user1}),
        ("insert into job_collaborators(id,organization_id,job_id,user_id,access_role,created_at,updated_at) values (:id,:o2,:j,:u2,'job_recruiter',now(),now())", {"id": uuid4(), "o2": org2, "j": job1, "u2": user2}),
        ("insert into job_collaborators(id,organization_id,job_id,user_id,access_role,created_at,updated_at) values (:id,:o1,:j,:u2,'job_recruiter',now(),now())", {"id": uuid4(), "o1": org1, "j": job1, "u2": user2}),
        ("insert into departments(id,organization_id,parent_id,name,created_at,updated_at) values (:id,:o2,:d,'bad',now(),now())", {"id": uuid4(), "o2": org2, "d": dep1}),
    ]
    for statement, params in invalid:
        with pytest.raises(IntegrityError):
            with pg_store.engine.begin() as connection:
                connection.execute(text(statement), params)


def test_audit_logs_reject_update_and_delete(pg_store) -> None:
    _, organization_id = seed_pg_user(pg_store)
    with pg_store.engine.begin() as connection:
        audit_id = uuid4()
        connection.execute(text("insert into audit_logs(id,organization_id,category,event_type,outcome,metadata_json,created_at) values (:id,:org,'system','test','success','{}',now())"), {"id": audit_id, "org": organization_id})
    for statement in ("update audit_logs set outcome='changed' where id=:id", "delete from audit_logs where id=:id"):
        with pytest.raises(OperationalError):
            with pg_store.engine.begin() as connection:
                connection.execute(text(statement), {"id": audit_id})


def test_active_application_partial_index_and_recruiting_tenant_constraints(pg_store) -> None:
    user_id, organization_id = seed_pg_user(pg_store)
    with pg_store.engine.begin() as connection:
        candidate, job, file_id, resume = [uuid4() for _ in range(4)]
        connection.execute(text("insert into candidates(id,organization_id,display_name,owner_id) values (:id,:org,'Masked Candidate',:user)"), {"id": candidate, "org": organization_id, "user": user_id})
        connection.execute(text("insert into jobs(id,organization_id,title,owner_id,status,created_at,updated_at) values (:id,:org,'Role',:user,'open',now(),now())"), {"id": job, "org": organization_id, "user": user_id})
        connection.execute(text("insert into file_objects(id,organization_id,storage_key,original_filename,mime_type,size_bytes,sha256,uploaded_by) values (:id,:org,'private/key','resume.pdf','application/pdf',1,:sha,:user)"), {"id": file_id, "org": organization_id, "sha": "0" * 64, "user": user_id})
        connection.execute(text("insert into resumes(id,organization_id,candidate_id,file_object_id,version_number) values (:id,:org,:candidate,:file,1)"), {"id": resume, "org": organization_id, "candidate": candidate, "file": file_id})
        values = {"id": uuid4(), "org": organization_id, "candidate": candidate, "job": job, "resume": resume, "user": user_id}
        connection.execute(text("insert into applications(id,organization_id,candidate_id,job_id,resume_id,owner_id,stage) values (:id,:org,:candidate,:job,:resume,:user,'new')"), values)
    with pytest.raises(IntegrityError):
        with pg_store.engine.begin() as connection:
            connection.execute(text("insert into applications(id,organization_id,candidate_id,job_id,resume_id,owner_id,stage) values (:id,:org,:candidate,:job,:resume,:user,'review')"), {**values, "id": uuid4()})
    with pg_store.engine.begin() as connection:
        connection.execute(text("update applications set stage='rejected' where id=:id"), values)
        connection.execute(text("insert into applications(id,organization_id,candidate_id,job_id,resume_id,owner_id,stage,source_application_id) values (:new,:org,:candidate,:job,:resume,:user,'new',:id)"), {**values, "new": uuid4()})


def test_recruiting_immutable_rows_reject_update_and_delete(pg_store) -> None:
    user_id, organization_id = seed_pg_user(pg_store)
    with pg_store.engine.begin() as connection:
        job_id = uuid4()
        version_id = uuid4()
        connection.execute(text("insert into jobs(id,organization_id,title,owner_id,status,created_at,updated_at) values (:id,:org,'Role',:user,'draft',now(),now())"), {"id": job_id, "org": organization_id, "user": user_id})
        connection.execute(text("insert into job_jd_versions(id,organization_id,job_id,version_number,content,created_by) values (:id,:org,:job,1,'{}',:user)"), {"id": version_id, "org": organization_id, "job": job_id, "user": user_id})
    for statement in ("update job_jd_versions set version_number=2 where id=:id", "delete from job_jd_versions where id=:id"):
        with pytest.raises(OperationalError):
            with pg_store.engine.begin() as connection:
                connection.execute(text(statement), {"id": version_id})


def test_concurrent_stale_application_and_job_writers_only_emit_one_success(pg_store) -> None:
    organization_id, user_id, ids = seed_recruiting_graph(pg_store)

    def race(kind):
        barrier = Barrier(2, timeout=10)
        def worker(_):
            with pg_store.sync_session() as db:
                barrier.wait()
                try:
                    if kind == "application":
                        transition_application_record(db, organization_id, ids["application"], "review", expected_version=1, actor_user_id=user_id, trace_id="race")
                    else:
                        transition_job_record(db, ids["job"], "open", expected_version=1, actor_user_id=user_id, trace_id="race")
                    db.commit()
                    return "success"
                except ResourceVersionConflict:
                    db.rollback()
                    return "conflict"
        with ThreadPoolExecutor(max_workers=2) as executor:
            assert sorted(executor.map(worker, range(2))) == ["conflict", "success"]

    race("application")
    race("job")
    with pg_store.engine.begin() as connection:
        assert connection.execute(text("select version from applications where id=:id"), {"id": ids["application"]}).scalar_one() == 2
        assert connection.execute(text("select count(*) from application_stage_events where application_id=:id"), {"id": ids["application"]}).scalar_one() == 1
        assert connection.execute(text("select count(*) from audit_logs where trace_id='race'" )).scalar_one() == 2


def test_concurrent_patch_compare_and_swap_has_one_success_and_one_atomic_audit_timeline(pg_store) -> None:
    organization_id, user_id, ids = seed_recruiting_graph(pg_store)

    cases = [
        ("job", ids["job"], lambda db: patch_job_record(db, organization_id, ids["job"], {"title": "Changed"}, expected_version=1, actor_user_id=user_id, trace_id="patch-job")),
        ("candidate", ids["candidate"], lambda db: patch_candidate_record(db, organization_id, ids["candidate"], {"display_name": "Changed"}, expected_version=1, actor_user_id=user_id, trace_id="patch-candidate")),
        ("application", ids["application"], lambda db: patch_application_record(db, organization_id, ids["application"], {"human_conclusion": "recommend"}, expected_version=1, actor_user_id=user_id, trace_id="patch-application")),
    ]
    for _, _, operation in cases:
        barrier = Barrier(2, timeout=10)
        def worker(_):
            with pg_store.sync_session() as db:
                barrier.wait()
                try:
                    operation(db); db.commit(); return "success"
                except ResourceVersionConflict:
                    db.rollback(); return "conflict"
        with ThreadPoolExecutor(max_workers=2) as executor:
            assert sorted(executor.map(worker, range(2))) == ["conflict", "success"]

    with pg_store.engine.connect() as connection:
        assert connection.execute(text("select count(*) from audit_logs where trace_id like 'patch-%'" )).scalar_one() == 3
        assert connection.execute(text("select count(*) from candidate_events where event_type in ('candidate.corrected','application.updated')" )).scalar_one() == 2


def test_concurrent_first_use_idempotency_serializes_winner_replay_and_conflict(pg_store) -> None:
    organization_id, user_id, ids = seed_recruiting_graph(pg_store)

    def run_pair(key, bodies):
        barrier = Barrier(2, timeout=10)
        def worker(body):
            with pg_store.sync_session() as db:
                barrier.wait()
                try:
                    def action():
                        db.add(CandidateEvent(organization_id=organization_id, candidate_id=ids["candidate"], actor_user_id=user_id, event_type="idempotent.side_effect", payload={"body": body}))
                        return 201, {"winner": body}
                    result = persisted_idempotent(db, organization_id, user_id, "test", key, {"body": body}, action)
                    db.commit()
                    return "success", result
                except IdempotencyConflict:
                    db.rollback()
                    return "conflict", None
        with ThreadPoolExecutor(max_workers=2) as executor:
            return list(executor.map(worker, bodies))

    same = run_pair("same", ["one", "one"])
    assert [status for status, _ in same] == ["success", "success"]
    assert same[0][1] == same[1][1]
    different = run_pair("different", ["one", "two"])
    assert sorted(status for status, _ in different) == ["conflict", "success"]
    with pg_store.engine.begin() as connection:
        assert connection.execute(text("select count(*) from candidate_events where event_type='idempotent.side_effect'" )).scalar_one() == 2
        assert connection.execute(text("select count(*) from idempotency_records" )).scalar_one() == 2


def test_persisted_download_ticket_binding_expiry_and_concurrent_single_use(pg_store) -> None:
    organization_id, user_id, ids = seed_recruiting_graph(pg_store)
    clock = FixedRecruitingClock()
    with pg_store.sync_session() as db:
        raw = issue_download_ticket_record(db, organization_id, user_id, ids["resume"], clock, FixedToken("race-ticket"))
        expired = issue_download_ticket_record(db, organization_id, user_id, ids["resume"], clock, FixedToken("expired-ticket"))
        db.commit()
    for binding in ((uuid4(), user_id, ids["resume"]), (organization_id, uuid4(), ids["resume"]), (organization_id, user_id, uuid4())):
        with pg_store.sync_session() as db, pytest.raises(TicketInvalid):
            consume_download_ticket_record(db, raw, *binding, clock)
    clock.now += timedelta(seconds=61)
    with pg_store.sync_session() as db, pytest.raises(TicketInvalid):
        consume_download_ticket_record(db, expired, organization_id, user_id, ids["resume"], clock)
    clock.now -= timedelta(seconds=61)
    barrier = Barrier(2, timeout=10)
    def consume(_):
        with pg_store.sync_session() as db:
            barrier.wait()
            try:
                consume_download_ticket_record(db, raw, organization_id, user_id, ids["resume"], clock)
                db.commit()
                return "success"
            except TicketInvalid:
                db.rollback()
                return "invalid"
    with ThreadPoolExecutor(max_workers=2) as executor:
        assert sorted(executor.map(consume, range(2))) == ["invalid", "success"]


def test_application_relationships_cross_tenant_and_all_immutable_tables_are_enforced(pg_store) -> None:
    org1, user1, ids1 = seed_recruiting_graph(pg_store)
    org2, user2, ids2 = seed_recruiting_graph(pg_store, "concurrent-two")
    same_org_candidate, same_org_file, same_org_resume = uuid4(), uuid4(), uuid4()
    with pg_store.engine.begin() as connection:
        connection.execute(text("insert into candidates(id,organization_id,display_name,owner_id) values (:id,:org,'Second',:user)"), {"id": same_org_candidate, "org": org1, "user": user1})
        connection.execute(text("insert into file_objects(id,organization_id,storage_key,original_filename,mime_type,size_bytes,sha256,uploaded_by) values (:id,:org,:key,'second.pdf','application/pdf',1,:sha,:user)"), {"id": same_org_file, "org": org1, "key": str(same_org_file), "sha": "3" * 64, "user": user1})
        connection.execute(text("insert into resumes(id,organization_id,candidate_id,file_object_id,version_number) values (:id,:org,:candidate,:file,1)"), {"id": same_org_resume, "org": org1, "candidate": same_org_candidate, "file": same_org_file})
    invalid = [
        ("insert into candidates(id,organization_id,display_name,owner_id) values (:id,:org,'bad',:owner)", {"id": uuid4(), "org": org1, "owner": user2}),
        ("insert into resumes(id,organization_id,candidate_id,file_object_id,version_number) values (:id,:org,:candidate,:file,2)", {"id": uuid4(), "org": org1, "candidate": ids1["candidate"], "file": ids2["file"]}),
        ("insert into applications(id,organization_id,candidate_id,job_id,resume_id,owner_id,stage) values (:id,:org,:candidate,:job,:resume,:user,'new')", {"id": uuid4(), "org": org1, "candidate": ids1["candidate"], "job": ids1["job"], "resume": ids2["resume"], "user": user1}),
        ("insert into applications(id,organization_id,candidate_id,job_id,resume_id,owner_id,stage) values (:id,:org,:candidate,:job,:resume,:user,'rejected')", {"id": uuid4(), "org": org1, "candidate": same_org_candidate, "job": ids1["job"], "resume": ids1["resume"], "user": user1}),
        ("insert into candidate_events(id,organization_id,candidate_id,actor_user_id,event_type,payload) values (:id,:org,:candidate,:actor,'bad','{}')", {"id": uuid4(), "org": org1, "candidate": ids2["candidate"], "actor": user1}),
        ("insert into candidate_notes(id,organization_id,candidate_id,actor_user_id,event_type,payload) values (:id,:org,:candidate,:actor,'bad','{}')", {"id": uuid4(), "org": org1, "candidate": ids2["candidate"], "actor": user1}),
        ("insert into application_stage_events(id,organization_id,application_id,actor_user_id,event_type,payload) values (:id,:org,:application,:actor,'bad','{}')", {"id": uuid4(), "org": org1, "application": ids2["application"], "actor": user1}),
        ("insert into download_tickets(id,organization_id,token_hash,user_id,resume_id,expires_at) values (:id,:org,:hash,:user,:resume,now())", {"id": uuid4(), "org": org1, "hash": "2" * 64, "user": user1, "resume": ids2["resume"]}),
    ]
    for statement, params in invalid:
        with pytest.raises(IntegrityError):
            with pg_store.engine.begin() as connection: connection.execute(text(statement), params)
    with pytest.raises(IntegrityError):
        with pg_store.engine.begin() as connection:
            connection.execute(text("insert into applications(id,organization_id,candidate_id,job_id,resume_id,owner_id,stage,source_application_id) values (:id,:org,:candidate,:job,:resume,:user,'rejected',:source)"), {"id": uuid4(), "org": org1, "candidate": ids1["candidate"], "job": ids1["job"], "resume": ids1["resume"], "user": user1, "source": ids1["application"]})
    with pg_store.engine.begin() as connection:
        connection.execute(text("update applications set stage='rejected' where id=:id"), {"id": ids1["application"]})
    with pytest.raises(IntegrityError):
        with pg_store.engine.begin() as connection:
            connection.execute(text("insert into applications(id,organization_id,candidate_id,job_id,resume_id,owner_id,stage,source_application_id) values (:id,:org,:candidate,:job,:resume,:user,'rejected',:source)"), {"id": uuid4(), "org": org1, "candidate": same_org_candidate, "job": ids1["job"], "resume": same_org_resume, "user": user1, "source": ids1["application"]})
    with pytest.raises(IntegrityError):
        with pg_store.engine.begin() as connection:
            connection.execute(text("insert into applications(id,organization_id,candidate_id,job_id,resume_id,owner_id,stage,source_application_id) values (:id,:org,:candidate,:job,:resume,:user,'new',:source)"), {"id": uuid4(), "org": org1, "candidate": ids2["candidate"], "job": ids1["job"], "resume": ids1["resume"], "user": user1, "source": ids1["application"]})
    with pg_store.engine.begin() as connection:
        immutable_ids = {name: uuid4() for name in ("job_jd_versions", "screening_rule_versions", "application_stage_events", "candidate_notes", "candidate_events")}
        for table in ("job_jd_versions", "screening_rule_versions"):
            connection.execute(text(f"insert into {table}(id,organization_id,job_id,version_number,content,created_by) values (:id,:org,:job,1,'{{}}',:user)"), {"id": immutable_ids[table], "org": org1, "job": ids1["job"], "user": user1})
        connection.execute(text("insert into application_stage_events(id,organization_id,application_id,actor_user_id,event_type,payload) values (:id,:org,:app,:user,'test','{}')"), {"id": immutable_ids["application_stage_events"], "org": org1, "app": ids1["application"], "user": user1})
        for table in ("candidate_notes", "candidate_events"):
            connection.execute(text(f"insert into {table}(id,organization_id,candidate_id,actor_user_id,event_type,payload) values (:id,:org,:candidate,:user,'test','{{}}')"), {"id": immutable_ids[table], "org": org1, "candidate": ids1["candidate"], "user": user1})
        immutable_ids["resumes"] = ids1["resume"]
    for table, row_id in immutable_ids.items():
        for statement in (f"update {table} set created_at=created_at where id=:id", f"delete from {table} where id=:id"):
            with pytest.raises(OperationalError):
                with pg_store.engine.begin() as connection:
                    connection.execute(text(statement), {"id": row_id})
