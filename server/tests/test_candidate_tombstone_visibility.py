import asyncio
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.dialects import postgresql

from server.app.governance.deletion_models import DeletionRequest
from server.app.identity.models import JobCollaborator, User
from server.app.llm.models import LlmInvocation, LlmScreeningEvaluation
from server.app.llm.security import ApiKeyCipher
from server.app.queue.service import PermanentJobError
from server.app.recruiting import service as recruiting_service
from server.app.recruiting.models import Application, Candidate, FileObject, Resume
from server.app.screening.llm_pipeline import LlmScreeningPipeline
from server.app.screening import actions as screening_actions
from server.app.screening.models import ScreeningItem, ScreeningResult, ScreeningRun
from server.tests.test_interview_api import create_interview, make_app, seed_application
from server.tests.test_llm_pipeline import Gateway, prepared
from server.tests.test_recruiting_api import login, seed_user
from server.tests.test_screening_api import login as screening_login
from server.tests.test_screening_pipeline import seeded_pipeline
from server.tests.test_talent_api import create_pool_and_membership


def _tombstone(app, candidate_id):
    with app.state.identity_store.sync_session() as db:
        candidate = db.get(Candidate, candidate_id)
        candidate.deleted_at = datetime.now(timezone.utc)
        db.commit()


def _active_deletion(db, candidate, status="approved"):
    request = DeletionRequest(
        organization_id=candidate.organization_id,
        candidate_id=candidate.id,
        status=status,
        reason_code="administrator_request",
        requested_by=candidate.owner_id,
        approved_by=candidate.owner_id,
        impact_manifest={},
        manifest_hash="0" * 64,
        policy_version=1,
        candidate_version=candidate.version,
    )
    db.add(request)
    db.flush()
    return request


class FinalizeBarrierParser:
    def __init__(self, callback):
        self.callback = callback

    async def parse(self, *_args, **_kwargs):
        self.callback()
        return SimpleNamespace(text="private parsed resume", parser_version="test-v1", quality="good")


def test_tombstone_is_non_enumerable_from_candidate_resume_and_download_routes(tmp_path):
    app = make_app(tmp_path)
    seed = seed_application(app)
    recruiter_id = seed_user(app, "recruiter", "tombstone-recruiter@example.test")
    manager_id = seed_user(app, "hiring_manager", "tombstone-manager@example.test")
    with app.state.identity_store.sync_session() as db:
        for user_id, access_role in ((recruiter_id, "job_recruiter"), (manager_id, "job_manager")):
            db.add(JobCollaborator(
                organization_id=db.get(User, user_id).organization_id,
                job_id=seed["job_id"],
                user_id=user_id,
                access_role=access_role,
            ))
        resume_id = db.scalar(select(Resume.id).where(Resume.candidate_id == seed["candidate_id"]))
        db.commit()

    with TestClient(app) as client:
        active_headers = login(client, "interview-admin@example.test")
        issued = client.post(f"/api/v1/resumes/{resume_id}/download-tickets", headers=active_headers)
        assert issued.status_code == 201
        issued_token = issued.json()["data"]["token"]

    _tombstone(app, seed["candidate_id"])
    paths = (
        "/api/v1/candidates",
        f"/api/v1/candidates/{seed['candidate_id']}",
        f"/api/v1/candidates/{seed['candidate_id']}/applications",
        f"/api/v1/resumes/{resume_id}/preview",
    )
    with TestClient(app) as client:
        for email in (
            "interview-admin@example.test",
            "tombstone-recruiter@example.test",
            "tombstone-manager@example.test",
        ):
            headers = login(client, email)
            listing = client.get(paths[0], headers=headers)
            assert listing.status_code == 200
            assert listing.json()["data"] == []
            for path in paths[1:]:
                response = client.get(path, headers=headers)
                assert response.status_code == 404
                assert response.json()["code"] == "resource_not_found"
        admin = login(client, "interview-admin@example.test")
        ticket = client.post(f"/api/v1/resumes/{resume_id}/download-tickets", headers=admin)
        assert ticket.status_code == 404
        assert ticket.json()["code"] == "resource_not_found"
        consumed = client.post("/api/v1/download-tickets/consume", json={"token": issued_token}, headers=admin)
        assert consumed.status_code == 404
        assert consumed.json()["code"] == "resource_not_found"
        workbench = client.get("/api/v1/workbench", headers=admin)
        assert workbench.status_code == 200
        assert str(seed["candidate_id"]) not in workbench.text


def test_tombstone_is_hidden_from_interviewer_detail_material_calendar_and_tasks(tmp_path):
    app = make_app(tmp_path)
    seed = seed_application(app)
    with TestClient(app) as client:
        admin = login(client, "interview-admin@example.test")
        interview, _ = create_interview(client, seed)
        interview_id = interview.json()["data"]["id"]
        _tombstone(app, seed["candidate_id"])
        interviewer = login(client, "assigned@example.test")
        for suffix in ("", "/materials", "/calendar-file", "/my-feedback", "/feedbacks"):
            response = client.get(f"/api/v1/interviews/{interview_id}{suffix}", headers=interviewer)
            assert response.status_code == 404
            assert response.json()["code"] == "resource_not_found"
        tasks = client.get("/api/v1/me/tasks", headers=interviewer)
        assert tasks.status_code == 200
        assert tasks.json()["data"] == []
        listing = client.get("/api/v1/interviews", headers=interviewer)
        assert listing.status_code == 200
        assert listing.json()["data"] == []


def test_tombstone_is_hidden_from_screening_items_and_retry(tmp_path):
    app, pipeline, _storage, _scanner, parse_job, run, item = seeded_pipeline(tmp_path)
    asyncio.run(pipeline.parse_item(parse_job))
    with app.state.identity_store.sync_session() as db:
        stored = db.get(ScreeningItem, uuid.UUID(item["id"]))
        stored.status = "failed"
        stored.safe_error_code = "scoring_failed"
        db.get(Candidate, stored.candidate_id).deleted_at = datetime.now(timezone.utc)
        db.commit()

    with TestClient(app) as client:
        headers = screening_login(client, "admin@example.test")
        listing = client.get(f"/api/v1/screening-runs/{run['id']}/items", headers=headers)
        retry = client.post(
            f"/api/v1/screening-items/{item['id']}/retry",
            headers={**headers, "Idempotency-Key": "tombstone-retry"},
        )

    assert listing.status_code == 200
    assert listing.json()["data"] == []
    assert retry.status_code == 404
    assert retry.json()["code"] == "resource_not_found"


def test_tombstone_is_hidden_from_talent_memberships_and_mutations(tmp_path):
    app = make_app(tmp_path)
    seed = seed_application(app)
    with app.state.identity_store.sync_session() as db:
        application = db.get(Application, seed["application_id"])
        application.stage = "rejected"
        application.version += 1
        db.commit()
    with TestClient(app) as client:
        headers, pool, membership = create_pool_and_membership(client, seed)
        pool_id = pool.json()["data"]["id"]
        membership_data = membership.json()["data"]
        membership_id = membership_data["id"]
        version = membership_data["version"]
        _tombstone(app, seed["candidate_id"])

        listed = client.get(f"/api/v1/talent-pools/{pool_id}/memberships", headers=headers)
        patched = client.patch(
            f"/api/v1/talent-pool-memberships/{membership_id}",
            json={"tags": ["must-not-write"]},
            headers={**headers, "If-Match": f'"{version}"'},
        )
        reactivated = client.post(
            f"/api/v1/talent-pool-memberships/{membership_id}/reactivations",
            json={"job_id": str(seed["job_id"])},
            headers={**headers, "Idempotency-Key": "tombstone-reactivate"},
        )

    assert listed.status_code == 200
    assert listed.json()["data"] == []
    for response in (patched, reactivated):
        assert response.status_code == 404
        assert response.json()["code"] == "resource_not_found"


def test_candidate_patch_rechecks_tombstone_after_candidate_lock(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    seed = seed_application(app)

    def delete_after_read(db, organization_id, candidate_id):
        candidate = db.get(Candidate, candidate_id)
        candidate.deleted_at = datetime.now(timezone.utc)
        db.flush()
        return candidate

    monkeypatch.setattr(recruiting_service, "lock_candidate_retention_facts", delete_after_read)
    with TestClient(app) as client:
        response = client.patch(
            f"/api/v1/candidates/{seed['candidate_id']}",
            json={"current_title": "must not persist"},
            headers={**login(client, "interview-admin@example.test"), "If-Match": '"1"'},
        )

    assert response.status_code == 404
    assert response.json()["code"] == "resource_not_found"
    with app.state.identity_store.sync_session() as db:
        candidate = db.get(Candidate, seed["candidate_id"])
        assert candidate.current_title != "must not persist"


def test_parse_and_score_stale_jobs_do_not_process_tombstones(tmp_path):
    app, pipeline, _storage, _scanner, parse_job, _run, item = seeded_pipeline(tmp_path)
    deterministic_candidate_id = uuid.uuid5(uuid.UUID(item["id"]), "candidate")
    with app.state.identity_store.sync_session() as db:
        run = db.get(__import__("server.app.screening.models", fromlist=["ScreeningRun"]).ScreeningRun, uuid.UUID(item["run_id"]))
        db.add(Candidate(
            id=deterministic_candidate_id,
            organization_id=run.organization_id,
            display_name="deleted import",
            owner_id=run.created_by,
            deleted_at=datetime.now(timezone.utc),
        ))
        db.commit()

    with pytest.raises(PermanentJobError) as parse_error:
        asyncio.run(pipeline.parse_item(parse_job))
    assert parse_error.value.safe_code == "screening_item_missing"
    with app.state.identity_store.sync_session() as db:
        assert db.scalar(select(func.count(Resume.id))) == 0
        assert db.scalar(select(func.count(Application.id))) == 0

    score_path = tmp_path / "score"
    score_path.mkdir()
    app, pipeline, _storage, _scanner, parse_job, run, item = seeded_pipeline(score_path)
    asyncio.run(pipeline.parse_item(parse_job))
    with app.state.identity_store.sync_session() as db:
        stored = db.get(ScreeningItem, uuid.UUID(item["id"]))
        db.get(Candidate, stored.candidate_id).deleted_at = datetime.now(timezone.utc)
        aggregate = db.get(__import__("server.app.screening.models", fromlist=["ScreeningRun"]).ScreeningRun, uuid.UUID(run["id"]))
        score_job = SimpleNamespace(
            payload={"organization_id": str(stored.organization_id), "screening_item_id": str(stored.id), "jd_version_id": str(aggregate.jd_version_id), "rule_version_id": str(aggregate.rule_version_id), "rule_engine_version": "rule-v1"},
            attempts=1,
            max_attempts=3,
        )
        db.commit()
    with pytest.raises(PermanentJobError) as score_error:
        asyncio.run(pipeline.score_item(score_job))
    assert score_error.value.safe_code == "screening_item_missing"
    with app.state.identity_store.sync_session() as db:
        stored = db.get(ScreeningItem, uuid.UUID(item["id"]))
        assert stored.status == "parsed"
        assert db.scalar(select(func.count(ScreeningResult.id))) == 0


def test_llm_stale_job_never_calls_provider_for_tombstone(tmp_path):
    app, cipher, job = prepared(tmp_path)
    with app.state.identity_store.sync_session() as db:
        item = db.get(ScreeningItem, uuid.UUID(job.payload["screening_item_id"]))
        db.get(Candidate, item.candidate_id).deleted_at = datetime.now(timezone.utc)
        db.commit()
    gateway = Gateway()
    pipeline = LlmScreeningPipeline(app.state.identity_store.sync_session, gateway, cipher)

    with pytest.raises(PermanentJobError) as error:
        asyncio.run(pipeline.evaluate_item(job))

    assert error.value.safe_code == "screening_item_missing"
    assert gateway.calls == []


def test_active_deletion_blocks_parse_and_score_without_state_changes(tmp_path):
    app, pipeline, _storage, _scanner, parse_job, _run, item = seeded_pipeline(tmp_path)
    candidate_id = uuid.uuid5(uuid.UUID(item["id"]), "candidate")
    with app.state.identity_store.sync_session() as db:
        run = db.get(__import__("server.app.screening.models", fromlist=["ScreeningRun"]).ScreeningRun, uuid.UUID(item["run_id"]))
        candidate = Candidate(id=candidate_id, organization_id=run.organization_id, display_name="deleting", owner_id=run.created_by)
        db.add(candidate)
        db.flush()
        _active_deletion(db, candidate)
        db.commit()

    with pytest.raises(PermanentJobError) as parse_error:
        asyncio.run(pipeline.parse_item(parse_job))
    assert parse_error.value.safe_code == "screening_item_missing"
    with app.state.identity_store.sync_session() as db:
        stored = db.get(ScreeningItem, uuid.UUID(item["id"]))
        assert stored.status == "queued"
        assert stored.candidate_id is None
        assert db.scalar(select(func.count(Resume.id))) == 0

    score_path = tmp_path / "score-active-deletion"
    score_path.mkdir()
    app, pipeline, _storage, _scanner, parse_job, run, item = seeded_pipeline(score_path)
    asyncio.run(pipeline.parse_item(parse_job))
    with app.state.identity_store.sync_session() as db:
        stored = db.get(ScreeningItem, uuid.UUID(item["id"]))
        candidate = db.get(Candidate, stored.candidate_id)
        _active_deletion(db, candidate)
        aggregate = db.get(__import__("server.app.screening.models", fromlist=["ScreeningRun"]).ScreeningRun, uuid.UUID(run["id"]))
        score_job = SimpleNamespace(payload={"organization_id": str(stored.organization_id), "screening_item_id": str(stored.id), "jd_version_id": str(aggregate.jd_version_id), "rule_version_id": str(aggregate.rule_version_id), "rule_engine_version": "rule-v1"}, attempts=1, max_attempts=3)
        db.commit()
    with pytest.raises(PermanentJobError) as score_error:
        asyncio.run(pipeline.score_item(score_job))
    assert score_error.value.safe_code == "screening_item_missing"
    with app.state.identity_store.sync_session() as db:
        stored = db.get(ScreeningItem, uuid.UUID(item["id"]))
        assert stored.status == "parsed"
        assert db.scalar(select(func.count(ScreeningResult.id))) == 0


@pytest.mark.parametrize("barrier", ["tombstone", "approved_deletion"])
def test_parse_finalize_barrier_cancels_and_aggregates_without_pii(tmp_path, barrier):
    app, pipeline, _storage, _scanner, parse_job, run_data, item_data = seeded_pipeline(tmp_path)
    item_id = uuid.UUID(item_data["id"])
    candidate_id = uuid.uuid5(item_id, "candidate")
    with app.state.identity_store.sync_session() as db:
        run = db.get(ScreeningRun, uuid.UUID(run_data["id"]))
        db.add(Candidate(id=candidate_id, organization_id=run.organization_id, display_name="Existing", owner_id=run.created_by))
        db.commit()

    def activate_barrier():
        with app.state.identity_store.sync_session() as db:
            item = db.get(ScreeningItem, item_id)
            assert item.status == "parsing"
            candidate = db.get(Candidate, candidate_id)
            if barrier == "tombstone":
                candidate.deleted_at = datetime.now(timezone.utc)
            else:
                _active_deletion(db, candidate)
            db.commit()

    pipeline.parser = FinalizeBarrierParser(activate_barrier)
    asyncio.run(pipeline.parse_item(parse_job))

    with app.state.identity_store.sync_session() as db:
        item = db.get(ScreeningItem, item_id)
        run = db.get(ScreeningRun, uuid.UUID(run_data["id"]))
        first_terminal = (item.finished_at, run.version)
        assert item.status == "cancelled"
        assert item.safe_error_code == "candidate_unavailable"
        assert item.finished_at is not None
        assert item.candidate_id is None and item.resume_id is None and item.application_id is None
        assert run.status == "failed"
        assert (run.processed_count, run.succeeded_count, run.failed_count) == (1, 0, 1)
        assert run.finished_at is not None
        assert db.scalar(select(func.count(Resume.id)).where(Resume.candidate_id == candidate_id)) == 0
        assert db.scalar(select(func.count(Application.id)).where(Application.candidate_id == candidate_id)) == 0

    asyncio.run(pipeline.parse_item(parse_job))
    with app.state.identity_store.sync_session() as db:
        item = db.get(ScreeningItem, item_id)
        run = db.get(ScreeningRun, uuid.UUID(run_data["id"]))
        assert (item.finished_at, run.version) == first_terminal
        assert (run.processed_count, run.succeeded_count, run.failed_count) == (1, 0, 1)


def test_parse_finalize_barrier_makes_mixed_run_partial(tmp_path):
    app, pipeline, _storage, _scanner, parse_job, run_data, item_data = seeded_pipeline(tmp_path)
    item_id = uuid.UUID(item_data["id"])
    candidate_id = uuid.uuid5(item_id, "candidate")
    with app.state.identity_store.sync_session() as db:
        run = db.get(ScreeningRun, uuid.UUID(run_data["id"]))
        db.add(Candidate(id=candidate_id, organization_id=run.organization_id, display_name="Existing", owner_id=run.created_by))
        completed_file = FileObject(organization_id=run.organization_id, storage_key=f"clean/{run.organization_id}/{uuid.uuid4()}", original_filename="completed.txt", mime_type="text/plain", size_bytes=6, sha256="2" * 64, uploaded_by=run.created_by, storage_state="clean", detected_type="txt", scan_status="clean")
        db.add(completed_file)
        db.flush()
        db.add(ScreeningItem(organization_id=run.organization_id, run_id=run.id, file_object_id=completed_file.id, status="scored", attempts=1, llm_status="skipped", finished_at=datetime.now(timezone.utc)))
        run.total_count = 2
        db.commit()

    def tombstone_during_parse():
        with app.state.identity_store.sync_session() as db:
            assert db.get(ScreeningItem, item_id).status == "parsing"
            db.get(Candidate, candidate_id).deleted_at = datetime.now(timezone.utc)
            db.commit()

    pipeline.parser = FinalizeBarrierParser(tombstone_during_parse)
    asyncio.run(pipeline.parse_item(parse_job))

    with app.state.identity_store.sync_session() as db:
        item = db.get(ScreeningItem, item_id)
        run = db.get(ScreeningRun, uuid.UUID(run_data["id"]))
        assert item.status == "cancelled"
        assert run.status == "partial"
        assert (run.processed_count, run.succeeded_count, run.failed_count) == (2, 1, 1)
        assert run.finished_at is not None


def test_active_deletion_blocks_llm_provider_and_post_provider_results(tmp_path):
    app, cipher, job = prepared(tmp_path)
    with app.state.identity_store.sync_session() as db:
        item = db.get(ScreeningItem, uuid.UUID(job.payload["screening_item_id"]))
        _active_deletion(db, db.get(Candidate, item.candidate_id))
        db.commit()
    gateway = Gateway()
    asyncio.run(LlmScreeningPipeline(app.state.identity_store.sync_session, gateway, cipher).evaluate_item(job))
    assert gateway.calls == []
    with app.state.identity_store.sync_session() as db:
        item = db.get(ScreeningItem, uuid.UUID(job.payload["screening_item_id"]))
        assert item.llm_status == "queued"
        assert db.scalar(select(func.count(LlmInvocation.id))) == 0

    post_path = tmp_path / "post-provider"
    post_path.mkdir()
    app, cipher, job = prepared(post_path)

    def approve_during_provider():
        with app.state.identity_store.sync_session() as db:
            item = db.get(ScreeningItem, uuid.UUID(job.payload["screening_item_id"]))
            _active_deletion(db, db.get(Candidate, item.candidate_id))
            db.commit()

    gateway = Gateway(inspect=approve_during_provider)
    asyncio.run(LlmScreeningPipeline(app.state.identity_store.sync_session, gateway, cipher).evaluate_item(job))
    assert len(gateway.calls) == 1
    with app.state.identity_store.sync_session() as db:
        item = db.get(ScreeningItem, uuid.UUID(job.payload["screening_item_id"]))
        assert item.llm_status == "skipped"
        assert db.scalar(select(func.count(LlmScreeningEvaluation.id))) == 0
        assert db.scalar(select(func.count(LlmInvocation.id))) == 0


def test_atomic_claim_does_not_reenter_running_llm_or_scoring_item(tmp_path, monkeypatch):
    app, cipher, job = prepared(tmp_path)
    with app.state.identity_store.sync_session() as db:
        item = db.get(ScreeningItem, uuid.UUID(job.payload["screening_item_id"]))
        item.llm_status = "running"
        db.commit()
    gateway = Gateway()
    asyncio.run(LlmScreeningPipeline(app.state.identity_store.sync_session, gateway, cipher).evaluate_item(job))
    assert gateway.calls == []
    with app.state.identity_store.sync_session() as db:
        item = db.get(ScreeningItem, uuid.UUID(job.payload["screening_item_id"]))
        assert item.llm_status == "running"
        assert db.scalar(select(func.count(LlmInvocation.id))) == 0

    score_path = tmp_path / "score-running"
    score_path.mkdir()
    app, pipeline, _storage, _scanner, parse_job, run, item = seeded_pipeline(score_path)
    asyncio.run(pipeline.parse_item(parse_job))
    with app.state.identity_store.sync_session() as db:
        stored = db.get(ScreeningItem, uuid.UUID(item["id"]))
        stored.status = "scoring"
        aggregate = db.get(__import__("server.app.screening.models", fromlist=["ScreeningRun"]).ScreeningRun, uuid.UUID(run["id"]))
        score_job = SimpleNamespace(payload={"organization_id": str(stored.organization_id), "screening_item_id": str(stored.id), "jd_version_id": str(aggregate.jd_version_id), "rule_version_id": str(aggregate.rule_version_id), "rule_engine_version": "rule-v1"}, attempts=1, max_attempts=3)
        db.commit()

    def duplicate_score(*_args, **_kwargs):
        raise AssertionError("duplicate score computation")

    monkeypatch.setattr("server.app.screening.pipeline.score_resume", duplicate_score)
    asyncio.run(pipeline.score_item(score_job))
    with app.state.identity_store.sync_session() as db:
        stored = db.get(ScreeningItem, uuid.UUID(item["id"]))
        assert stored.status == "scoring"
        assert db.scalar(select(func.count(ScreeningResult.id))) == 0


def test_retry_candidate_lock_race_maps_to_non_enumerating_404(tmp_path, monkeypatch):
    app, pipeline, _storage, _scanner, parse_job, _run, item = seeded_pipeline(tmp_path)
    asyncio.run(pipeline.parse_item(parse_job))
    with app.state.identity_store.sync_session() as db:
        stored = db.get(ScreeningItem, uuid.UUID(item["id"]))
        stored.status = "failed"
        stored.safe_error_code = "scoring_failed"
        db.commit()

    tombstoned = getattr(screening_actions, "CandidateTombstoned", RuntimeError)

    def lose_race(*_args, **_kwargs):
        raise tombstoned

    monkeypatch.setattr(screening_actions, "lock_candidate_screening_item", lose_race, raising=False)
    with TestClient(app) as client:
        headers = screening_login(client, "admin@example.test")
        response = client.post(f"/api/v1/screening-items/{item['id']}/retry", headers={**headers, "Idempotency-Key": "retry-race"})

    assert response.status_code == 404
    assert response.json()["code"] == "resource_not_found"


def test_screening_claim_locks_candidate_checks_deletion_then_locks_item():
    organization_id = uuid.uuid4()
    candidate_id = uuid.uuid4()
    item_id = uuid.uuid4()

    class RecordingSession:
        def __init__(self):
            self.statements = []

        def scalar(self, statement):
            sql = str(statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
            self.statements.append(sql)
            if "FROM candidates" in sql:
                return SimpleNamespace(id=candidate_id, deleted_at=None)
            if "FROM deletion_requests" in sql:
                return None
            if "FROM screening_items" in sql:
                return SimpleNamespace(id=item_id, candidate_id=candidate_id)
            raise AssertionError(sql)

    db = RecordingSession()
    screening_actions.lock_candidate_screening_item(db, organization_id, item_id, candidate_id)

    assert ["FROM candidates", "FROM deletion_requests", "FROM screening_items"] == [
        next(marker for marker in ("FROM candidates", "FROM deletion_requests", "FROM screening_items") if marker in sql)
        for sql in db.statements
    ]
    assert "FOR UPDATE" in db.statements[0]
    assert "FOR UPDATE" in db.statements[2]
    assert all(status in db.statements[1] for status in ("approved", "executing", "completed"))
