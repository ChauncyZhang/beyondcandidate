import os
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from server.app.core.settings import Settings
from server.app.governance import service as governance_service
from server.app.governance.models import RetentionPolicy
from server.app.governance.retention import lock_candidate_retention_facts, recalculate_candidate_retention
from server.app.governance.service import aware, candidate_due_dates
from server.app.identity.models import AuditLog, Job, Organization, User, UserRole
from server.app.identity.bootstrap import bootstrap_system_admin
from server.app.interviews import api as interviews_api
from server.app.interviews.models import Interview, InterviewFeedback, InterviewParticipant
from server.app.main import create_app
from server.app.recruiting.models import (
    Application,
    Candidate,
    FileObject,
    IdempotencyRecord,
    JobJdVersion,
    Resume,
    ScreeningRuleVersion,
)
from server.app.recruiting import service as recruiting_service
from server.app.recruiting.service import (
    ResourceVersionConflict,
    create_application_record,
    patch_application_record,
    persisted_idempotent,
    transition_application_record,
)
from server.app.screening import actions as screening_actions
from server.app.screening import api as screening_api
from server.app.screening.models import ScreeningItem, ScreeningResult, ScreeningRun
from server.app.talent import api as talent_api
from server.app.talent.models import TalentPool, TalentPoolMembership


pytestmark = pytest.mark.skipif(
    not os.getenv("POSTGRES_SMOKE_URL"), reason="PostgreSQL smoke URL not configured"
)


class Probe:
    async def check(self) -> None:
        pass


@pytest.fixture
def postgres_app():
    url = os.environ["POSTGRES_SMOKE_URL"]
    subprocess.run(
        ["python", "-m", "alembic", "-c", "server/alembic.ini", "upgrade", "head"],
        check=True,
        env={**os.environ, "DATABASE_URL": url},
    )
    engine = create_engine(url.replace("+asyncpg", "+psycopg"))
    with engine.begin() as connection:
        connection.execute(text("TRUNCATE organizations CASCADE"))
    app = create_app(
        settings=Settings(
            environment="test",
            database_url=url,
            cors_origins=["https://hr.example.test"],
        ),
        database_probe=Probe(),
        storage_probe=Probe(),
    )
    bootstrap_system_admin(
        app.state.identity_store,
        "governance-pg",
        "Governance PG",
        "governance-pg@test",
        "Governance admin",
        "correct horse battery staple",
    )
    yield app, engine
    engine.dispose()


def _login(client):
    response = client.post(
        "/api/v1/auth/login",
        json={
            "organization_slug": "governance-pg",
            "email": "governance-pg@test",
            "password": "correct horse battery staple",
        },
        headers={"Origin": "https://hr.example.test"},
    )
    assert response.status_code == 200
    return {
        "Origin": "https://hr.example.test",
        "X-CSRF-Token": response.headers["X-CSRF-Token"],
    }


def test_concurrent_patch_commits_one_version_and_one_audit(postgres_app) -> None:
    app, engine = postgres_app
    clients = [TestClient(app), TestClient(app)]
    headers = [_login(client) for client in clients]
    barrier = threading.Barrier(2)
    results = []

    def patch(index):
        barrier.wait()
        results.append(
            clients[index].patch(
                "/api/v1/settings/retention-policy",
                json={
                    "terminal_days": 400,
                    "talent_pool_days": 730,
                    "backup_window_days": 90,
                },
                headers={
                    **headers[index],
                    "If-Match": '"1"',
                    "Idempotency-Key": f"concurrent-{index}",
                },
            )
        )

    threads = [threading.Thread(target=patch, args=(index,)) for index in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    for client in clients:
        client.close()

    assert sorted(response.status_code for response in results) == [200, 409]
    assert next(response for response in results if response.status_code == 409).json()["code"] == "resource_version_conflict"
    with Session(engine) as db:
        assert db.scalar(select(RetentionPolicy.version)) == 2
        assert db.scalar(
            select(func.count()).select_from(AuditLog).where(
                AuditLog.event_type == "retention_policy.updated"
            )
        ) == 1


def _seed_application_facts(engine):
    with Session(engine) as db:
        user = db.scalar(select(User).where(User.email == "governance-pg@test"))
        candidate = Candidate(
            organization_id=user.organization_id,
            display_name="Concurrent candidate",
            owner_id=user.id,
            retention_due_at=datetime.now(timezone.utc) + timedelta(days=365),
        )
        job = Job(
            organization_id=user.organization_id,
            title="Concurrent job",
            owner_id=user.id,
            status="open",
        )
        file = FileObject(
            organization_id=user.organization_id,
            storage_key=f"private/{uuid4()}",
            original_filename="candidate.pdf",
            mime_type="application/pdf",
            size_bytes=1,
            sha256="b" * 64,
            uploaded_by=user.id,
        )
        db.add_all([candidate, job, file])
        db.flush()
        resume = Resume(
            organization_id=user.organization_id,
            candidate_id=candidate.id,
            file_object_id=file.id,
            version_number=1,
        )
        db.add(resume)
        db.commit()
        return user.id, user.organization_id, candidate.id, job.id, resume.id


def test_retention_patch_serializes_with_concurrent_active_application(postgres_app) -> None:
    app, engine = postgres_app
    user_id, organization_id, candidate_id, job_id, resume_id = _seed_application_facts(engine)
    application_locked = threading.Event()
    allow_application_commit = threading.Event()
    patch_started = threading.Event()
    results = {}

    def create_application():
        with Session(engine) as db:
            create_application_record(
                db,
                organization_id=organization_id,
                candidate_id=candidate_id,
                job_id=job_id,
                resume_id=resume_id,
                owner_id=user_id,
            )
            application_locked.set()
            assert allow_application_commit.wait(10)
            db.commit()

    with TestClient(app) as client:
        headers = _login(client)

        def patch_policy():
            patch_started.set()
            results["response"] = client.patch(
                "/api/v1/settings/retention-policy",
                json={
                    "terminal_days": 400,
                    "talent_pool_days": 730,
                    "backup_window_days": 90,
                },
                headers={
                    **headers,
                    "If-Match": '"1"',
                    "Idempotency-Key": "application-barrier",
                },
            )

        application_thread = threading.Thread(target=create_application)
        application_thread.start()
        assert application_locked.wait(10)
        patch_thread = threading.Thread(target=patch_policy)
        patch_thread.start()
        assert patch_started.wait(10)
        time.sleep(0.5)
        allow_application_commit.set()
        application_thread.join(10)
        patch_thread.join(10)

    assert not application_thread.is_alive() and not patch_thread.is_alive()
    assert results["response"].status_code == 200
    with Session(engine) as db:
        assert db.get(Candidate, candidate_id).retention_due_at is None


def test_expired_idempotency_key_concurrency_executes_replacement_once(postgres_app) -> None:
    _, engine = postgres_app
    with Session(engine) as db:
        user = db.scalar(select(User).where(User.email == "governance-pg@test"))
        db.add(
            IdempotencyRecord(
                organization_id=user.organization_id,
                user_id=user.id,
                operation="retention_policy.update",
                idempotency_key="expired-concurrent",
                request_hash="0" * 64,
                status_code=200,
                response_json={"stale": True},
                expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
            )
        )
        db.commit()
        organization_id, user_id = user.organization_id, user.id

    barrier = threading.Barrier(2)
    action_count = 0
    action_lock = threading.Lock()
    results = []

    def execute():
        nonlocal action_count
        with Session(engine) as db:
            barrier.wait()

            def action():
                nonlocal action_count
                with action_lock:
                    action_count += 1
                return 200, {"fresh": True}

            results.append(
                persisted_idempotent(
                    db,
                    organization_id,
                    user_id,
                    "retention_policy.update",
                    "expired-concurrent",
                    {"version": 2},
                    action,
                )
            )
            db.commit()

    threads = [threading.Thread(target=execute) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    assert all(not thread.is_alive() for thread in threads)
    assert action_count == 1
    assert results == [(200, {"fresh": True}), (200, {"fresh": True})]
    with Session(engine) as db:
        records = db.scalars(
            select(IdempotencyRecord).where(
                IdempotencyRecord.operation == "retention_policy.update",
                IdempotencyRecord.idempotency_key == "expired-concurrent",
            )
        ).all()
        assert len(records) == 1 and records[0].response_json == {"fresh": True}


def _seed_concurrent_retention_fact_writers(engine):
    old = datetime.now(timezone.utc) - timedelta(days=30)
    with Session(engine) as db:
        user = db.scalar(select(User).where(User.email == "governance-pg@test"))
        db.add_all([UserRole(user_id=user.id, role="recruiting_admin"), UserRole(user_id=user.id, role="interviewer")])
        candidate = Candidate(
            organization_id=user.organization_id,
            display_name="Fact writer candidate",
            owner_id=user.id,
            created_at=old,
            updated_at=old,
            retention_due_at=old + timedelta(days=365),
        )
        job = Job(organization_id=user.organization_id, title="Fact writer job", owner_id=user.id, status="open")
        file = FileObject(
            organization_id=user.organization_id,
            storage_key=f"private/{uuid4()}",
            original_filename="fact.pdf",
            mime_type="application/pdf",
            size_bytes=1,
            sha256="c" * 64,
            uploaded_by=user.id,
        )
        db.add_all([candidate, job, file])
        db.flush()
        resume = Resume(organization_id=user.organization_id, candidate_id=candidate.id, file_object_id=file.id, version_number=1)
        db.add(resume)
        db.flush()
        application = Application(
            organization_id=user.organization_id,
            candidate_id=candidate.id,
            job_id=job.id,
            resume_id=resume.id,
            owner_id=user.id,
            stage="hired",
            created_at=old,
            updated_at=old,
        )
        pool = TalentPool(
            organization_id=user.organization_id,
            name="Fact pool",
            purpose="Concurrent retention facts",
            owner_id=user.id,
            suitable_roles=["Engineer"],
        )
        db.add_all([application, pool])
        db.flush()
        interview = Interview(
            organization_id=user.organization_id,
            application_id=application.id,
            round_name="Final",
            method="video",
            timezone="UTC",
            starts_at=old,
            ends_at=old + timedelta(hours=1),
            meeting_url="https://meet.example.test/fact",
            status="feedback_completed",
            owner_id=user.id,
            created_by=user.id,
            created_at=old,
            updated_at=old,
        )
        db.add(interview)
        db.flush()
        participant = InterviewParticipant(
            organization_id=user.organization_id,
            interview_id=interview.id,
            user_id=user.id,
            role="interviewer",
            required_feedback=True,
            task_status="completed",
        )
        db.add(participant)
        db.flush()
        feedback = InterviewFeedback(
            organization_id=user.organization_id,
            interview_id=interview.id,
            author_id=user.id,
            status="submitted",
            ratings={"technical": 4, "communication": 4, "problem_solving": 4},
            strengths="Strong",
            risks="None",
            conclusion="recommend",
            notes="Original",
            version=1,
            submitted_at=old,
            created_at=old,
            updated_at=old,
        )
        db.add(feedback)
        db.commit()
        return {
            "organization_id": user.organization_id,
            "user_id": user.id,
            "candidate_id": candidate.id,
            "application_id": application.id,
            "pool_id": pool.id,
            "feedback_id": feedback.id,
        }


@pytest.mark.parametrize("writer_kind", ["talent", "feedback", "event"])
def test_retention_patch_serializes_with_every_retention_fact_writer(
    postgres_app, monkeypatch, writer_kind
) -> None:
    app, engine = postgres_app
    ids = _seed_concurrent_retention_fact_writers(engine)
    snapshot_ready = threading.Event()
    release_patch = threading.Event()
    writer_done = threading.Event()
    responses = {}
    original = governance_service.affected_candidate_ids

    def paused_snapshot(*args, **kwargs):
        result = original(*args, **kwargs)
        snapshot_ready.set()
        assert release_patch.wait(10)
        return result

    monkeypatch.setattr(governance_service, "affected_candidate_ids", paused_snapshot)

    def patch_policy():
        with TestClient(app) as client:
            headers = _login(client)
            responses["patch"] = client.patch(
                "/api/v1/settings/retention-policy",
                json={"terminal_days": 400, "talent_pool_days": 730, "backup_window_days": 90},
                headers={**headers, "If-Match": '"1"', "Idempotency-Key": f"fact-{writer_kind}"},
            )

    def write_fact():
        with TestClient(app) as client:
            headers = _login(client)
            if writer_kind == "talent":
                responses["writer"] = client.post(
                    f"/api/v1/talent-pools/{ids['pool_id']}/memberships",
                    json={
                        "candidate_id": str(ids["candidate_id"]),
                        "owner_id": str(ids["user_id"]),
                        "suitable_roles": ["Engineer"],
                        "tags": [],
                        "reason": "Concurrent fact",
                        "retention_until": (datetime.now(timezone.utc) + timedelta(days=900)).isoformat(),
                    },
                    headers={**headers, "Idempotency-Key": "fact-talent-writer"},
                )
            elif writer_kind == "feedback":
                responses["writer"] = client.post(
                    f"/api/v1/interview-feedback/{ids['feedback_id']}/amendments",
                    json={
                        "ratings": {
                            "professional_ability": 4,
                            "problem_solving": 4,
                            "communication": 4,
                            "role_fit": 4,
                        },
                        "strengths": "Stronger",
                        "risks": "Low",
                        "conclusion": "strong_recommend",
                        "notes": "Amended",
                        "reason": "New evidence",
                    },
                    headers={**headers, "If-Match": '"1"'},
                )
            else:
                responses["writer"] = client.post(
                    f"/api/v1/candidates/{ids['candidate_id']}/notes",
                    json={"application_id": str(ids["application_id"]), "body": "Concurrent event"},
                    headers=headers,
                )
        writer_done.set()

    patch_thread = threading.Thread(target=patch_policy)
    patch_thread.start()
    assert snapshot_ready.wait(10)
    writer_thread = threading.Thread(target=write_fact)
    writer_thread.start()
    writer_was_serialized = not writer_done.wait(0.75)
    release_patch.set()
    patch_thread.join(10)
    writer_thread.join(10)

    assert not patch_thread.is_alive() and not writer_thread.is_alive()
    assert writer_was_serialized
    assert responses["patch"].status_code == 200
    assert responses["writer"].status_code in {200, 201}
    with Session(engine) as db:
        candidate = db.get(Candidate, ids["candidate_id"])
        expected = candidate_due_dates(db, ids["organization_id"], 400)[ids["candidate_id"]]
        assert aware(candidate.retention_due_at) == aware(expected)


def test_single_candidate_recalculation_does_not_touch_or_wait_on_other_candidate(
    postgres_app,
) -> None:
    _, engine = postgres_app
    ids = _seed_concurrent_retention_fact_writers(engine)
    with Session(engine) as db:
        other = Candidate(
            organization_id=ids["organization_id"],
            display_name="Unrelated candidate",
            owner_id=ids["user_id"],
            retention_due_at=datetime.now(timezone.utc) + timedelta(days=700),
        )
        db.add(other)
        db.commit()
        other_id = other.id
    def row_identity(candidate_id):
        with engine.connect() as connection:
            return connection.execute(
                text(
                    "SELECT retention_due_at, updated_at, version, xmin::text "
                    "FROM candidates WHERE id = :id"
                ),
                {"id": candidate_id},
            ).one()

    def assert_isolated_recalculation(candidate_id, unrelated_id):
        before = row_identity(unrelated_id)
        blocker = Session(engine)
        blocker.execute(text("SET LOCAL statement_timeout = '5s'"))
        blocker.scalar(
            select(Candidate).where(Candidate.id == unrelated_id).with_for_update()
        )
        completed = threading.Event()
        errors = []

        def recalculate_target():
            try:
                with Session(engine) as db:
                    db.execute(text("SET LOCAL lock_timeout = '750ms'"))
                    db.execute(text("SET LOCAL statement_timeout = '3s'"))
                    lock_candidate_retention_facts(
                        db, ids["organization_id"], candidate_id
                    )
                    recalculate_candidate_retention(
                        db, ids["organization_id"], candidate_id
                    )
                    db.commit()
            except Exception as error:
                errors.append(error)
            finally:
                completed.set()

        thread = threading.Thread(target=recalculate_target)
        thread.start()
        assert completed.wait(3)
        blocker.rollback()
        blocker.close()
        thread.join(3)

        assert errors == []
        assert row_identity(unrelated_id) == before

    assert_isolated_recalculation(ids["candidate_id"], other_id)
    assert_isolated_recalculation(other_id, ids["candidate_id"])


def test_application_transition_and_patch_use_candidate_first_without_deadlock(
    postgres_app, monkeypatch
) -> None:
    _, engine = postgres_app
    ids = _seed_concurrent_retention_fact_writers(engine)
    with Session(engine) as db:
        application = db.get(Application, ids["application_id"])
        application.stage = "new"
        db.commit()

    transition_before_candidate = threading.Event()
    release_transition = threading.Event()
    patch_done = threading.Event()
    errors = {}
    original_lock = recruiting_service.lock_candidate_retention_facts

    def observed_lock(db, organization_id, candidate_id):
        if threading.current_thread().name == "transition-writer":
            transition_before_candidate.set()
            assert release_transition.wait(5)
        return original_lock(db, organization_id, candidate_id)

    monkeypatch.setattr(
        recruiting_service, "lock_candidate_retention_facts", observed_lock
    )

    def transition_writer():
        try:
            with Session(engine) as db:
                db.execute(text("SET LOCAL lock_timeout = '2s'"))
                db.execute(text("SET LOCAL statement_timeout = '5s'"))
                transition_application_record(
                    db,
                    ids["organization_id"],
                    ids["application_id"],
                    "review",
                    expected_version=1,
                    actor_user_id=ids["user_id"],
                    trace_id="transition-lock-order",
                )
                db.commit()
        except Exception as error:
            errors["transition"] = error

    def patch_writer():
        try:
            with Session(engine) as db:
                db.execute(text("SET LOCAL lock_timeout = '2s'"))
                db.execute(text("SET LOCAL statement_timeout = '5s'"))
                patch_application_record(
                    db,
                    ids["organization_id"],
                    ids["application_id"],
                    {"human_conclusion": "serialized"},
                    expected_version=1,
                    actor_user_id=ids["user_id"],
                    trace_id="patch-lock-order",
                )
                db.commit()
        except Exception as error:
            errors["patch"] = error
        finally:
            patch_done.set()

    transition_thread = threading.Thread(
        target=transition_writer, name="transition-writer"
    )
    transition_thread.start()
    assert transition_before_candidate.wait(5)
    patch_thread = threading.Thread(target=patch_writer, name="patch-writer")
    patch_thread.start()
    patch_finished_before_release = patch_done.wait(1)
    release_transition.set()
    transition_thread.join(6)
    patch_thread.join(6)

    assert patch_finished_before_release
    assert "patch" not in errors
    assert isinstance(errors.get("transition"), ResourceVersionConflict)
    assert not any(isinstance(error, OperationalError) for error in errors.values())
    with Session(engine) as db:
        candidate = db.get(Candidate, ids["candidate_id"])
        expected = candidate_due_dates(
            db, ids["organization_id"], 365
        )[ids["candidate_id"]]
        assert candidate.retention_due_at is expected is None


def test_screening_bulk_and_retention_patch_lock_candidates_in_global_order(
    postgres_app, monkeypatch
) -> None:
    app, engine = postgres_app
    low_candidate_id = UUID(int=10)
    high_candidate_id = UUID(int=20)
    first_item_id = UUID(int=100)
    second_item_id = UUID(int=200)
    with Session(engine) as db:
        user = db.scalar(select(User).where(User.email == "governance-pg@test"))
        db.add(UserRole(user_id=user.id, role="recruiting_admin"))
        job = Job(
            organization_id=user.organization_id,
            title="Ordered bulk",
            owner_id=user.id,
            status="open",
        )
        db.add(job)
        db.flush()
        jd = JobJdVersion(
            organization_id=user.organization_id,
            job_id=job.id,
            version_number=1,
            content={"text": "Python"},
            created_by=user.id,
        )
        rule = ScreeningRuleVersion(
            organization_id=user.organization_id,
            job_id=job.id,
            version_number=1,
            content={},
            created_by=user.id,
        )
        db.add_all([jd, rule])
        db.flush()
        run = ScreeningRun(
            organization_id=user.organization_id,
            job_id=job.id,
            jd_version_id=jd.id,
            rule_version_id=rule.id,
            source="upload",
            status="completed",
            total_count=2,
            processed_count=2,
            succeeded_count=2,
            failed_count=0,
            created_by=user.id,
        )
        db.add(run)
        db.flush()
        application_ids = {}
        for index, (candidate_id, item_id) in enumerate(
            ((high_candidate_id, first_item_id), (low_candidate_id, second_item_id)),
            start=1,
        ):
            candidate = Candidate(
                id=candidate_id,
                organization_id=user.organization_id,
                display_name=f"Ordered candidate {index}",
                owner_id=user.id,
            )
            stored_file = FileObject(
                organization_id=user.organization_id,
                storage_key=f"private/ordered-{index}",
                original_filename=f"ordered-{index}.pdf",
                mime_type="application/pdf",
                size_bytes=1,
                sha256=str(index) * 64,
                uploaded_by=user.id,
            )
            db.add_all([candidate, stored_file])
            db.flush()
            resume = Resume(
                organization_id=user.organization_id,
                candidate_id=candidate.id,
                file_object_id=stored_file.id,
                version_number=1,
            )
            db.add(resume)
            db.flush()
            application = Application(
                organization_id=user.organization_id,
                candidate_id=candidate.id,
                job_id=job.id,
                resume_id=resume.id,
                owner_id=user.id,
                stage="new",
                source="screening",
            )
            db.add(application)
            db.flush()
            item = ScreeningItem(
                id=item_id,
                organization_id=user.organization_id,
                run_id=run.id,
                file_object_id=stored_file.id,
                candidate_id=candidate.id,
                resume_id=resume.id,
                application_id=application.id,
                status="scored",
                attempts=1,
            )
            db.add(item)
            db.flush()
            db.add(
                ScreeningResult(
                    organization_id=user.organization_id,
                    item_id=item.id,
                    application_id=application.id,
                    resume_id=resume.id,
                    rule_engine_version="rule-v1",
                    rule_score=80,
                    recommendation="可沟通",
                    required_hits=["Python"],
                    required_missing=[],
                    bonus_hits=[],
                    estimated_years=0,
                    risks=[],
                    questions=[],
                )
            )
            application_ids[candidate.id] = application.id
        db.commit()
        organization_id = user.organization_id
        run_id = run.id

    patch_has_low_candidate = threading.Event()
    release_patch = threading.Event()
    bulk_about_to_lock_low_candidate = threading.Event()
    responses = {}
    original_lock_all = governance_service.lock_all_candidate_retention_facts
    original_transition = screening_actions.transition_application_record
    original_apply_bulk = screening_api.apply_bulk_action

    def ordered_patch_lock(db, locked_organization_id):
        if locked_organization_id != organization_id:
            return original_lock_all(db, locked_organization_id)
        db.execute(text("SET LOCAL lock_timeout = '3s'"))
        db.execute(text("SET LOCAL statement_timeout = '8s'"))
        locked = []
        for candidate_id in (low_candidate_id, high_candidate_id):
            locked.append(
                db.scalar(
                    select(Candidate.id)
                    .where(
                        Candidate.organization_id == organization_id,
                        Candidate.id == candidate_id,
                    )
                    .with_for_update()
                )
            )
            if candidate_id == low_candidate_id:
                patch_has_low_candidate.set()
                assert release_patch.wait(10)
        return locked

    def observed_transition(db, locked_organization_id, application_id, *args, **kwargs):
        if application_id == application_ids[low_candidate_id]:
            bulk_about_to_lock_low_candidate.set()
        return original_transition(
            db, locked_organization_id, application_id, *args, **kwargs
        )

    def timed_apply_bulk(db, *args, **kwargs):
        db.execute(text("SET LOCAL lock_timeout = '3s'"))
        db.execute(text("SET LOCAL statement_timeout = '8s'"))
        return original_apply_bulk(db, *args, **kwargs)

    monkeypatch.setattr(
        governance_service, "lock_all_candidate_retention_facts", ordered_patch_lock
    )
    monkeypatch.setattr(
        screening_actions, "transition_application_record", observed_transition
    )
    monkeypatch.setattr(screening_api, "apply_bulk_action", timed_apply_bulk)

    patch_client = TestClient(app)
    bulk_client = TestClient(app)
    patch_headers = _login(patch_client)
    bulk_headers = _login(bulk_client)

    def patch_policy():
        responses["patch"] = patch_client.patch(
            "/api/v1/settings/retention-policy",
            json={
                "terminal_days": 400,
                "talent_pool_days": 730,
                "backup_window_days": 90,
            },
            headers={
                **patch_headers,
                "If-Match": '"1"',
                "Idempotency-Key": "ordered-bulk-retention",
            },
        )

    def bulk_reject():
        responses["bulk"] = bulk_client.post(
            f"/api/v1/screening-runs/{run_id}/bulk-actions",
            json={
                "command": "reject",
                "reason_code": "not_selected",
                "items": [
                    {
                        "item_id": str(first_item_id),
                        "expected_application_version": 1,
                    },
                    {
                        "item_id": str(second_item_id),
                        "expected_application_version": 1,
                    },
                ],
            },
            headers={**bulk_headers, "Idempotency-Key": "ordered-bulk-reject"},
        )

    patch_thread = threading.Thread(target=patch_policy)
    bulk_thread = threading.Thread(target=bulk_reject)
    patch_thread.start()
    assert patch_has_low_candidate.wait(10)
    bulk_thread.start()
    bulk_about_to_lock_low_candidate.wait(1)
    release_patch.set()
    patch_thread.join(12)
    bulk_thread.join(12)
    patch_client.close()
    bulk_client.close()

    assert not patch_thread.is_alive() and not bulk_thread.is_alive()
    assert responses["patch"].status_code == 200
    assert responses["bulk"].status_code == 200
    with Session(engine) as db:
        expected = candidate_due_dates(
            db, organization_id, 400, {low_candidate_id, high_candidate_id}
        )
        for candidate_id in (low_candidate_id, high_candidate_id):
            candidate = db.get(Candidate, candidate_id)
            application = db.get(Application, application_ids[candidate_id])
            assert application.stage == "rejected"
            assert application.version == 2
            assert aware(candidate.retention_due_at) == aware(expected[candidate_id])
        assert db.scalar(
            select(func.count()).select_from(AuditLog).where(
                AuditLog.event_type == "application.stage_changed"
            )
        ) == 2


@pytest.mark.parametrize("writer_kind", ["feedback", "talent"])
def test_fact_writer_waits_for_candidate_before_locking_business_row(
    postgres_app, writer_kind, monkeypatch
) -> None:
    app, engine = postgres_app
    ids = _seed_concurrent_retention_fact_writers(engine)
    with TestClient(app) as login_client:
        headers = _login(login_client)
        session_cookies = dict(login_client.cookies)
    with Session(engine) as db:
        membership = TalentPoolMembership(
            organization_id=ids["organization_id"],
            pool_id=ids["pool_id"],
            candidate_id=ids["candidate_id"],
            owner_id=ids["user_id"],
            suitable_roles=["Engineer"],
            tags=[],
            reason="Existing fact",
            retention_until=datetime.now(timezone.utc) + timedelta(days=800),
        )
        db.add(membership)
        db.commit()
        membership_id = membership.id

    candidate_writer = Session(engine)
    candidate_writer.execute(text("SET LOCAL lock_timeout = '2s'"))
    candidate = candidate_writer.scalar(
        select(Candidate)
        .where(Candidate.id == ids["candidate_id"])
        .with_for_update()
    )
    candidate.current_title = "Concurrent candidate writer"
    candidate_writer.flush()
    response = {}
    done = threading.Event()
    writer_before_candidate = threading.Event()
    writer_module = interviews_api if writer_kind == "feedback" else talent_api
    original_lock = writer_module.lock_active_candidate

    def observed_lock(db, organization_id, candidate_id):
        db.execute(text("SET LOCAL lock_timeout = '3s'"))
        db.execute(text("SET LOCAL statement_timeout = '6s'"))
        writer_before_candidate.set()
        return original_lock(db, organization_id, candidate_id)

    monkeypatch.setattr(writer_module, "lock_active_candidate", observed_lock)

    def write_business_fact():
        with TestClient(app) as client:
            client.cookies.update(session_cookies)
            if writer_kind == "feedback":
                response["value"] = client.post(
                    f"/api/v1/interview-feedback/{ids['feedback_id']}/amendments",
                    json={
                        "ratings": {
                            "professional_ability": 4,
                            "problem_solving": 4,
                            "communication": 4,
                            "role_fit": 4,
                        },
                        "strengths": "Updated",
                        "risks": "Low",
                        "conclusion": "recommend",
                        "notes": "Lock order",
                        "reason": "Concurrent evidence",
                    },
                    headers={**headers, "If-Match": '"1"'},
                )
            else:
                response["value"] = client.patch(
                    f"/api/v1/talent-pool-memberships/{membership_id}",
                    json={"retention_until": (datetime.now(timezone.utc) + timedelta(days=900)).isoformat()},
                    headers={**headers, "If-Match": '"1"'},
                )
        done.set()

    thread = threading.Thread(target=write_business_fact, name="fact-writer")
    thread.start()
    reached_candidate_lock = writer_before_candidate.wait(10)
    fact_row_was_free = False
    try:
        if reached_candidate_lock:
            fact_row_was_free = True
            try:
                with Session(engine) as probe:
                    probe.execute(text("SET LOCAL lock_timeout = '500ms'"))
                    if writer_kind == "feedback":
                        probe.scalar(
                            select(InterviewFeedback)
                            .where(InterviewFeedback.id == ids["feedback_id"])
                            .with_for_update(nowait=True)
                        )
                    else:
                        probe.scalar(
                            select(TalentPoolMembership)
                            .where(TalentPoolMembership.id == membership_id)
                            .with_for_update(nowait=True)
                        )
                    probe.rollback()
            except OperationalError:
                fact_row_was_free = False
    finally:
        candidate_writer.commit()
        candidate_writer.close()
    thread.join(6)

    assert reached_candidate_lock
    assert fact_row_was_free
    assert done.is_set()
    assert response["value"].status_code == 200
    with Session(engine) as db:
        candidate = db.get(Candidate, ids["candidate_id"])
        expected = candidate_due_dates(
            db, ids["organization_id"], 365
        )[ids["candidate_id"]]
        assert aware(candidate.retention_due_at) == aware(expected)
