import os
import subprocess
import threading
from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, func, select, text
from sqlalchemy.orm import Session, sessionmaker

from server.app.core.settings import Settings
from server.app.governance.deletion_models import DeletionArtifact, DeletionRequest, LegalHold
from server.app.governance.deletion_service import build_private_manifest, canonical_manifest_hash
from server.app.identity.bootstrap import bootstrap_system_admin
from server.app.identity.models import AuditLog, Job, Organization, User, UserRole
from server.app.identity.security import PasswordService
from server.app.main import create_app
from server.app.queue.models import BackgroundJob
from server.app.queue.repository import QueueRepository
from server.app.queue.service import RetryableJobError
from server.app.recruiting.models import (
    Candidate,
    FileObject,
    JobJdVersion,
    ScreeningRuleVersion,
)
from server.app.screening.models import ScreeningItem, ScreeningRun
from server.app.reports.models import ExportCandidateMembership, ExportRecord


pytestmark = pytest.mark.skipif(
    not os.getenv("POSTGRES_SMOKE_URL"), reason="PostgreSQL smoke URL not configured"
)
ORIGIN = "https://hr.example.test"
BARRIER_TIMEOUT_SECONDS = 10
THREAD_JOIN_TIMEOUT_SECONDS = 30


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
        settings=Settings(environment="test", database_url=url, cors_origins=[ORIGIN]),
        database_probe=Probe(),
        storage_probe=Probe(),
    )
    bootstrap_system_admin(
        app.state.identity_store,
        "deletion-pg",
        "Deletion PG",
        "system@deletion-pg.test",
        "System admin",
        "correct horse battery staple",
    )
    with Session(engine) as db:
        organization = db.scalar(select(Organization).where(Organization.slug == "deletion-pg"))
        users = []
        for email, role in (
            ("recruiter@deletion-pg.test", "recruiter"),
            ("recruiting-admin@deletion-pg.test", "recruiting_admin"),
        ):
            user = User(
                organization_id=organization.id,
                email=email,
                normalized_email=email,
                display_name=role,
                password_hash=PasswordService().hash("correct horse battery staple"),
            )
            db.add(user)
            db.flush()
            db.add(UserRole(user_id=user.id, role=role))
            users.append(user)
        candidate = Candidate(
            organization_id=organization.id,
            display_name="Concurrent private candidate",
            owner_id=users[0].id,
        )
        db.add(candidate)
        db.commit()
        candidate_id = candidate.id
    try:
        yield app, engine, candidate_id
    finally:
        with engine.begin() as connection:
            connection.execute(text("TRUNCATE organizations CASCADE"))
        engine.dispose()


def login(client, email):
    response = client.post(
        "/api/v1/auth/login",
        json={
            "organization_slug": "deletion-pg",
            "email": email,
            "password": "correct horse battery staple",
        },
        headers={"Origin": ORIGIN},
    )
    assert response.status_code == 200
    return {"Origin": ORIGIN, "X-CSRF-Token": response.headers["X-CSRF-Token"]}


def create_request(client, headers, candidate_id, key):
    return client.post(
        f"/api/v1/candidates/{candidate_id}/deletion-requests",
        json={"reason_code": "candidate_request"},
        headers={**headers, "Idempotency-Key": key},
    )


def run_concurrently(*workers) -> None:
    barrier = threading.Barrier(len(workers))
    worker_errors = []
    error_lock = threading.Lock()

    def guarded(worker) -> None:
        try:
            barrier.wait(timeout=BARRIER_TIMEOUT_SECONDS)
            worker()
        except BaseException as error:
            with error_lock:
                worker_errors.append(error)

    threads = [
        threading.Thread(target=guarded, args=(worker,), daemon=True)
        for worker in workers
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=THREAD_JOIN_TIMEOUT_SECONDS)

    alive = [thread.name for thread in threads if thread.is_alive()]
    if alive:
        barrier.abort()
    assert not alive, f"concurrent workers remained alive: {alive}"
    assert not worker_errors, f"concurrent worker failures: {worker_errors!r}"


def test_two_request_creators_serialize_to_one_open_request(postgres_app) -> None:
    app, engine, candidate_id = postgres_app
    clients = [TestClient(app), TestClient(app)]
    headers = [login(client, "recruiter@deletion-pg.test") for client in clients]
    responses = []

    def create(index):
        responses.append(create_request(clients[index], headers[index], candidate_id, f"request-{index}"))

    run_concurrently(*(lambda index=index: create(index) for index in range(2)))
    for client in clients:
        client.close()

    assert sorted(response.status_code for response in responses) == [201, 409]
    assert next(response for response in responses if response.status_code == 409).json()["code"] == "deletion_request_open"
    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(DeletionRequest)) == 1


def test_two_deletion_workers_materialize_started_state_once(postgres_app) -> None:
    from server.app.governance.worker import DeletionJobHandler

    app, engine, candidate_id = postgres_app
    with TestClient(app) as client:
        requested = create_request(
            client,
            login(client, "recruiter@deletion-pg.test"),
            candidate_id,
            "worker-request",
        )
        request_id = UUID(requested.json()["data"]["id"])
        approved = client.post(
            f"/api/v1/deletion-requests/{request_id}/transitions",
            json={"target_status": "approved"},
            headers={
                **login(client, "system@deletion-pg.test"),
                "If-Match": '"1"',
                "Idempotency-Key": "worker-approve",
            },
        )
    assert approved.status_code == 200
    with Session(engine) as db:
        organization_id = db.scalar(select(Organization.id))
    sessions = sessionmaker(engine, expire_on_commit=False)
    outcomes = []

    def claim() -> None:
        handler = DeletionJobHandler(
            sessions,
            None,
            None,
            None,
            resume_bucket="resumes",
            export_bucket="resumes",
        )
        outcomes.append(
            handler._claim(
                organization_id,
                request_id,
                2,
                "two-worker-claim",
            )
        )

    run_concurrently(claim, claim)

    with Session(engine) as db:
        request = db.get(DeletionRequest, request_id)
        assert outcomes == [False, False]
        assert request.status == "executing"
        assert db.scalar(
            select(func.count())
            .select_from(AuditLog)
            .where(AuditLog.event_type == "governance.deletion_started")
        ) == 1


def test_atomic_screening_claim_precedes_and_blocks_deletion_claim(postgres_app) -> None:
    from server.app.governance.worker import DeletionJobHandler

    app, engine, candidate_id = postgres_app
    with TestClient(app) as client:
        requested = create_request(
            client,
            login(client, "recruiter@deletion-pg.test"),
            candidate_id,
            "screening-barrier-request",
        )
        request_id = UUID(requested.json()["data"]["id"])
        approved = client.post(
            f"/api/v1/deletion-requests/{request_id}/transitions",
            json={"target_status": "approved"},
            headers={
                **login(client, "system@deletion-pg.test"),
                "If-Match": '"1"',
                "Idempotency-Key": "screening-barrier-approve",
            },
        )
    assert approved.status_code == 200

    with Session(engine) as db:
        candidate = db.get(Candidate, candidate_id)
        request = db.get(DeletionRequest, request_id)
        recruiting_job = Job(
            organization_id=candidate.organization_id,
            title="Claimed screening",
            owner_id=candidate.owner_id,
            status="closed",
        )
        db.add(recruiting_job)
        db.flush()
        jd = JobJdVersion(
            organization_id=candidate.organization_id,
            job_id=recruiting_job.id,
            version_number=1,
            content={"text": "Python"},
            created_by=candidate.owner_id,
        )
        rule = ScreeningRuleVersion(
            organization_id=candidate.organization_id,
            job_id=recruiting_job.id,
            version_number=1,
            content={},
            created_by=candidate.owner_id,
        )
        stored = FileObject(
            organization_id=candidate.organization_id,
            storage_key=f"clean/{candidate.organization_id}/{uuid4()}",
            original_filename="claimed.txt",
            mime_type="text/plain",
            size_bytes=6,
            sha256="1" * 64,
            uploaded_by=candidate.owner_id,
        )
        db.add_all([jd, rule, stored])
        db.flush()
        run = ScreeningRun(
            organization_id=candidate.organization_id,
            job_id=recruiting_job.id,
            jd_version_id=jd.id,
            rule_version_id=rule.id,
            source="upload",
            status="rule_scoring",
            total_count=1,
            processed_count=0,
            succeeded_count=0,
            failed_count=0,
            created_by=candidate.owner_id,
        )
        db.add(run)
        db.flush()
        item = ScreeningItem(
            organization_id=candidate.organization_id,
            run_id=run.id,
            file_object_id=stored.id,
            candidate_id=candidate.id,
            status="parsed",
            attempts=1,
        )
        db.add(item)
        db.flush()
        now = datetime.now(timezone.utc)
        score_job = BackgroundJob(
            organization_id=candidate.organization_id,
            type="screening.score_item",
            payload={
                "organization_id": str(candidate.organization_id),
                "screening_item_id": str(item.id),
            },
            status="queued",
            priority=100,
            attempts=0,
            max_attempts=3,
            run_after=now,
            dedupe_key=f"score:{item.id}",
            created_at=now,
            updated_at=now,
        )
        db.add(score_job)
        db.flush()
        manifest, policy = build_private_manifest(db, candidate, now=request.requested_at)
        request.impact_manifest = manifest
        request.manifest_hash = canonical_manifest_hash(manifest)
        request.policy_version = policy.version
        organization_id = candidate.organization_id
        score_job_id = score_job.id
        db.commit()

    with Session(engine) as db:
        claimed = QueueRepository(db).claim(
            organization_id, "score-worker", lease_seconds=60
        )
        assert claimed is not None and claimed.id == score_job_id
        db.commit()

    handler = DeletionJobHandler(
        sessionmaker(engine, expire_on_commit=False),
        None,
        None,
        None,
        resume_bucket="resumes",
        export_bucket="resumes",
    )
    with pytest.raises(RetryableJobError) as raised:
        handler._claim(organization_id, request_id, 2, "screening-claimed-first")

    assert raised.value.safe_code == "deletion_screening_inflight"
    with Session(engine) as db:
        assert db.get(DeletionRequest, request_id).status == "approved"
        assert db.get(BackgroundJob, score_job_id).status == "running"
        assert db.scalar(select(DeletionArtifact.id)) is None


def test_export_prepare_and_deletion_settle_share_candidate_first_lock_order(
    postgres_app,
) -> None:
    from server.app.governance.worker import DeletionJobHandler
    from server.app.worker.main import ReportExportJobHandler

    app, engine, candidate_id = postgres_app
    now = datetime.now(timezone.utc)
    with Session(engine) as db:
        candidate = db.get(Candidate, candidate_id)
        requester = db.scalar(
            select(User).where(User.email == "recruiter@deletion-pg.test")
        )
        export_id = uuid4()
        export_job = BackgroundJob(
            organization_id=candidate.organization_id,
            type="reports.export",
            payload={
                "organization_id": str(candidate.organization_id),
                "export_id": str(export_id),
            },
            status="queued",
            priority=0,
            attempts=0,
            max_attempts=3,
            run_after=now,
            dedupe_key=f"pg-export-race:{export_id}",
            created_at=now,
            updated_at=now,
        )
        db.add(export_job)
        db.flush()
        db.add(
            ExportRecord(
                id=export_id,
                organization_id=candidate.organization_id,
                requested_by=requester.id,
                background_job_id=export_job.id,
                filters={"job_ids": [], "from": None, "to": None},
                created_at=now,
                updated_at=now,
            )
        )
        db.flush()
        db.add(
            ExportCandidateMembership(
                organization_id=candidate.organization_id,
                export_id=export_id,
                candidate_id=candidate.id,
            )
        )
        organization_id = candidate.organization_id
        export_job_id = export_job.id
        db.commit()

    with TestClient(app) as client:
        requested = create_request(
            client,
            login(client, "recruiter@deletion-pg.test"),
            candidate_id,
            "export-race-request",
        )
        request_id = UUID(requested.json()["data"]["id"])
        approved = client.post(
            f"/api/v1/deletion-requests/{request_id}/transitions",
            json={"target_status": "approved"},
            headers={
                **login(client, "system@deletion-pg.test"),
                "If-Match": '"1"',
                "Idempotency-Key": "export-race-approve",
            },
        )
    assert approved.status_code == 200

    sessions = sessionmaker(engine, expire_on_commit=False)
    deletion = DeletionJobHandler(
        sessions,
        None,
        None,
        None,
        resume_bucket="resumes",
        export_bucket="resumes",
    )
    assert deletion._claim(organization_id, request_id, 2, "export-race") is False

    class RecordingStorage:
        def __init__(self):
            self.writes = []

        def write(self, object_key, content, content_type):
            self.writes.append((object_key, content, content_type))

    storage = RecordingStorage()
    report = ReportExportJobHandler(sessions, storage)
    deletion_candidate_locked = threading.Event()
    prepare_candidate_started = threading.Event()
    outcomes = []
    errors = []

    def candidate_lock_statement(statement: str) -> bool:
        normalized = " ".join(statement.lower().split())
        return " from candidates " in f" {normalized} " and "for update" in normalized

    def before_cursor_execute(
        _connection, _cursor, statement, _parameters, _context, _executemany
    ):
        if (
            threading.current_thread().name == "prepare-export"
            and candidate_lock_statement(statement)
        ):
            prepare_candidate_started.set()

    def after_cursor_execute(
        _connection, _cursor, statement, _parameters, _context, _executemany
    ):
        if (
            threading.current_thread().name == "settle-deletion"
            and candidate_lock_statement(statement)
        ):
            deletion_candidate_locked.set()
            if not prepare_candidate_started.wait(timeout=BARRIER_TIMEOUT_SECONDS):
                raise AssertionError("export prepare did not reach the candidate lock")

    def settle_deletion():
        try:
            deletion._settle_exports(organization_id, request_id)
            outcomes.append("deletion_settled")
        except BaseException as error:
            errors.append(error)

    def prepare_report():
        try:
            report._generate(organization_id, export_id)
            outcomes.append("export_prepared")
        except (LookupError, PermissionError):
            outcomes.append("export_safely_rejected")
        except BaseException as error:
            errors.append(error)

    event.listen(engine, "before_cursor_execute", before_cursor_execute)
    event.listen(engine, "after_cursor_execute", after_cursor_execute)
    try:
        deletion_thread = threading.Thread(
            target=settle_deletion, name="settle-deletion", daemon=True
        )
        prepare_thread = threading.Thread(
            target=prepare_report, name="prepare-export", daemon=True
        )
        deletion_thread.start()
        assert deletion_candidate_locked.wait(timeout=BARRIER_TIMEOUT_SECONDS)
        prepare_thread.start()
        deletion_thread.join(timeout=THREAD_JOIN_TIMEOUT_SECONDS)
        prepare_thread.join(timeout=THREAD_JOIN_TIMEOUT_SECONDS)
        assert not deletion_thread.is_alive()
        assert not prepare_thread.is_alive()
    finally:
        event.remove(engine, "before_cursor_execute", before_cursor_execute)
        event.remove(engine, "after_cursor_execute", after_cursor_execute)

    assert errors == []
    assert sorted(outcomes) == ["deletion_settled", "export_safely_rejected"]
    assert storage.writes == []
    with Session(engine) as db:
        export = db.get(ExportRecord, export_id)
        assert export.status == "failed"
        assert export.safe_error_code == "deletion_in_progress"
        assert export.generation_token is None
        assert export.object_key is None
        assert db.get(BackgroundJob, export_job_id).status == "cancelled"


def test_same_key_different_candidates_never_replays_cross_candidate(postgres_app) -> None:
    app, engine, first_id = postgres_app
    with Session(engine) as db:
        owner = db.scalar(select(User).where(User.email == "recruiter@deletion-pg.test"))
        second = Candidate(organization_id=owner.organization_id, display_name="Second", owner_id=owner.id)
        db.add(second); db.commit(); second_id = second.id
    clients = [TestClient(app), TestClient(app)]
    headers = [login(client, "recruiter@deletion-pg.test") for client in clients]
    responses = []

    def create(index, candidate_id):
        responses.append(create_request(clients[index], headers[index], candidate_id, "same-resource-key"))

    run_concurrently(
        lambda: create(0, first_id),
        lambda: create(1, second_id),
    )
    assert sorted(response.status_code for response in responses) == [201, 409]
    assert next(r for r in responses if r.status_code == 409).json()["code"] == "idempotency_conflict"
    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(DeletionRequest)) == 1
    for client in clients: client.close()


def test_two_approvers_enqueue_exactly_one_job_and_stale_version_cannot_mutate(postgres_app) -> None:
    app, engine, candidate_id = postgres_app
    with TestClient(app) as creator:
        recruiter = login(creator, "recruiter@deletion-pg.test")
        created = create_request(creator, recruiter, candidate_id, "approval-request")
    request_id = created.json()["data"]["id"]
    clients = [TestClient(app), TestClient(app)]
    headers = [login(client, "system@deletion-pg.test") for client in clients]
    responses = []

    def approve(index):
        responses.append(
            clients[index].post(
                f"/api/v1/deletion-requests/{request_id}/transitions",
                json={"target_status": "approved"},
                headers={
                    **headers[index],
                    "If-Match": '"1"',
                    "Idempotency-Key": f"approve-{index}",
                },
            )
        )

    run_concurrently(*(lambda index=index: approve(index) for index in range(2)))
    for client in clients:
        client.close()

    assert sorted(response.status_code for response in responses) == [200, 409]
    with Session(engine) as db:
        request = db.get(DeletionRequest, UUID(request_id))
        assert request.status == "approved" and request.version == 2
        assert db.scalar(
            select(func.count()).select_from(BackgroundJob).where(
                BackgroundJob.type == "governance.delete_candidate"
            )
        ) == 1


def test_approval_and_hold_placement_serialize_without_executable_job(postgres_app) -> None:
    app, engine, candidate_id = postgres_app
    with TestClient(app) as creator:
        recruiter = login(creator, "recruiter@deletion-pg.test")
        created = create_request(creator, recruiter, candidate_id, "race-request")
    request_id = created.json()["data"]["id"]
    approve_client, hold_client = TestClient(app), TestClient(app)
    approve_headers = login(approve_client, "system@deletion-pg.test")
    hold_headers = login(hold_client, "recruiting-admin@deletion-pg.test")
    responses = {}

    def approve():
        responses["approval"] = approve_client.post(
            f"/api/v1/deletion-requests/{request_id}/transitions",
            json={"target_status": "approved"},
            headers={**approve_headers, "If-Match": '"1"', "Idempotency-Key": "race-approve"},
        )

    def hold():
        responses["hold"] = hold_client.post(
            f"/api/v1/candidates/{candidate_id}/legal-holds",
            json={"reason": "Concurrent legal hold"},
            headers={**hold_headers, "Idempotency-Key": "race-hold"},
        )

    run_concurrently(approve, hold)
    approve_client.close()
    hold_client.close()

    assert set(responses) == {"approval", "hold"}
    approval_response = responses["approval"]
    hold_response = responses["hold"]
    assert hold_response.status_code == 201
    assert hold_response.headers["ETag"] == '"1"'
    assert hold_response.json()["data"]["status"] == "active"
    assert hold_response.json()["data"]["version"] == 1
    assert hold_response.json()["data"]["reason"] == "Concurrent legal hold"

    with Session(engine) as db:
        request = db.get(DeletionRequest, UUID(request_id))
        active_holds = list(
            db.scalars(select(LegalHold).where(LegalHold.released_at.is_(None)))
        )
        jobs = list(
            db.scalars(
                select(BackgroundJob).where(
                    BackgroundJob.type == "governance.delete_candidate"
                )
            )
        )
        assert len(active_holds) == 1
        assert not any(job.status in {"queued", "running"} for job in jobs)

        if approval_response.status_code == 409:
            assert approval_response.json()["code"] == "legal_hold_active"
            assert request.status == "requested"
            assert request.version == 1
            assert request.safe_error_code is None
            assert jobs == []
        elif approval_response.status_code == 200:
            assert approval_response.json()["data"]["status"] == "approved"
            assert approval_response.json()["data"]["version"] == 2
            assert request.status == "failed"
            assert request.version == 3
            assert request.safe_error_code == "legal_hold_active"
            assert len(jobs) == 1
            assert jobs[0].dedupe_key == f"candidate-delete:{request_id}:2"
            assert jobs[0].status == "cancelled"
        else:
            pytest.fail(
                "approve-vs-hold produced an illegal response serialization: "
                f"approval={approval_response.status_code} "
                f"body={approval_response.json()!r}"
            )


def test_failed_retry_refreshes_manifest_and_uses_new_version_once(postgres_app) -> None:
    app, engine, candidate_id = postgres_app
    with TestClient(app) as client:
        recruiter = login(client, "recruiter@deletion-pg.test")
        created = create_request(client, recruiter, candidate_id, "retry-request")
        request_id = created.json()["data"]["id"]
        with Session(engine) as db:
            request = db.get(DeletionRequest, UUID(request_id))
            request.status = "failed"
            request.safe_error_code = "worker_failed"
            request.version = 2
            candidate = db.get(Candidate, candidate_id)
            candidate.version += 1
            db.commit()
        system = login(client, "system@deletion-pg.test")
        retried = client.post(
            f"/api/v1/deletion-requests/{request_id}/transitions",
            json={"target_status": "approved"},
            headers={**system, "If-Match": '"2"', "Idempotency-Key": "retry-approve"},
        )
        replay = client.post(
            f"/api/v1/deletion-requests/{request_id}/transitions",
            json={"target_status": "approved"},
            headers={**system, "If-Match": '"2"', "Idempotency-Key": "retry-approve"},
        )
    assert retried.status_code == replay.status_code == 200
    assert retried.json() == replay.json()
    assert retried.json()["data"]["version"] == 3
    with Session(engine) as db:
        assert db.scalar(
            select(func.count()).select_from(BackgroundJob).where(
                BackgroundJob.dedupe_key == f"candidate-delete:{request_id}:3"
            )
        ) == 1
