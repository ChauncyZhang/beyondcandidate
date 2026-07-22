import asyncio
import hashlib
import os
import subprocess

from concurrent.futures import ThreadPoolExecutor
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import func, select, text

from server.app.core.settings import Settings
from server.app.identity.models import AuditLog, Job, JobCollaborator, Organization, User, UserRole
from server.app.identity.security import PasswordService
from server.app.llm.models import LlmProviderConfig, PromptVersion
from server.app.main import create_app
from server.app.queue.models import BackgroundJob
from server.app.queue.repository import QueueRepository
from server.app.queue.runtime import DatabaseQueueGateway
from server.app.recruiting.models import Application, ApplicationStageEvent, Candidate, FileObject, JobJdVersion, Resume, ScreeningRuleVersion
from server.app.screening.models import ScreeningItem, ScreeningResult, ScreeningRun
from server.app.screening.pipeline import ScreeningPipeline
from server.app.screening.terminal import screening_terminal_callbacks
from server.app.worker.main import Worker
from server.tests.test_screening_api import Probe, login
from server.tests.test_screening_pipeline import MemoryPipelineStorage, Scanner


pytestmark = pytest.mark.skipif(not os.getenv("POSTGRES_SMOKE_URL"), reason="PostgreSQL smoke URL not configured")


def test_postgres_concurrent_llm_retry_enqueues_once_and_preserves_rule_facts():
    url = os.environ["POSTGRES_SMOKE_URL"]
    subprocess.run(["python", "-m", "alembic", "-c", "server/alembic.ini", "upgrade", "head"], check=True, env={**os.environ, "DATABASE_URL": url})
    app = create_app(settings=Settings(environment="test", database_url=url, cors_origins=["https://hr.example.test"]), database_probe=Probe(), storage_probe=Probe(), quarantine_storage=object())
    with app.state.identity_store.sync_session() as db:
        db.execute(text("TRUNCATE organizations CASCADE"))
        org = Organization(slug="llm-retry", name="LLM Retry", status="active")
        user = User(organization=org, email="llm-retry@pg.test", normalized_email="llm-retry@pg.test", display_name="Retry", password_hash=PasswordService().hash("correct"))
        user.roles.append(UserRole(role="recruiting_admin")); db.add(user); db.flush()
        recruiting_job = Job(organization_id=org.id, title="Backend", owner_id=user.id, status="draft"); db.add(recruiting_job); db.flush()
        jd = JobJdVersion(organization_id=org.id, job_id=recruiting_job.id, version_number=1, content={"text": "Python"}, created_by=user.id)
        rule = ScreeningRuleVersion(organization_id=org.id, job_id=recruiting_job.id, version_number=1, content={}, created_by=user.id); db.add_all([jd, rule]); db.flush()
        run = ScreeningRun(organization_id=org.id, job_id=recruiting_job.id, jd_version_id=jd.id, rule_version_id=rule.id, source="upload", status="partial", total_count=1, processed_count=1, succeeded_count=1, failed_count=0, created_by=user.id); db.add(run); db.flush()
        candidate = Candidate(organization_id=org.id, display_name="Candidate", owner_id=user.id); db.add(candidate); db.flush()
        data = b"Python candidate"
        stored_file = FileObject(organization_id=org.id, storage_key="clean/llm-retry", original_filename="candidate.txt", mime_type="text/plain", size_bytes=len(data), sha256=hashlib.sha256(data).hexdigest(), uploaded_by=user.id, storage_state="clean", detected_type="txt", scan_status="clean"); db.add(stored_file); db.flush()
        resume = Resume(organization_id=org.id, candidate_id=candidate.id, file_object_id=stored_file.id, version_number=1, parsed_text=data.decode()); db.add(resume); db.flush()
        application = Application(organization_id=org.id, candidate_id=candidate.id, job_id=recruiting_job.id, resume_id=resume.id, owner_id=user.id, stage="new", source="screening"); db.add(application); db.flush()
        item = ScreeningItem(organization_id=org.id, run_id=run.id, file_object_id=stored_file.id, candidate_id=candidate.id, resume_id=resume.id, application_id=application.id, status="scored", attempts=1, llm_status="failed", llm_safe_error_code="provider_unavailable", llm_attempts=3, llm_started_at=run.created_at, llm_finished_at=run.created_at, finished_at=run.created_at); db.add(item); db.flush()
        result = ScreeningResult(organization_id=org.id, item_id=item.id, application_id=application.id, resume_id=resume.id, rule_engine_version="rule-v1", rule_score=80, recommendation="可沟通", required_hits=["Python"], required_missing=[], bonus_hits=[], estimated_years=0, risks=[], questions=[]); db.add(result)
        config = LlmProviderConfig(organization_id=org.id, provider_id="approved", model="model", encrypted_api_key=b"encrypted-key", enabled=True, allowed_job_ids=[], version=4, created_by=user.id, updated_by=user.id)
        prompt = PromptVersion(organization_id=org.id, name="screening-evaluation", version_number=1, content={"system": "bounded"}, content_hash="p" * 64, created_by=user.id); db.add_all([config, prompt]); db.flush()
        old_job = QueueRepository(db).enqueue(org.id, "screening.llm_score_item", {"organization_id": str(org.id), "screening_item_id": str(item.id), "screening_result_id": str(result.id), "config_id": str(config.id), "config_version": 3, "prompt_version_id": str(prompt.id)}, dedupe_key=f"llm:{item.id}:3:{prompt.id}", max_attempts=3)
        old_job.status = "dead_letter"; old_job.attempts = old_job.max_attempts
        db.commit(); item_id, application_id, old_job_id = item.id, application.id, old_job.id

    with TestClient(app) as client:
        headers = login(client, "llm-retry@pg.test", organization_slug="llm-retry")
        def retry(key):
            return client.post(f"/api/v1/screening-items/{item_id}/retry", headers={**headers, "Idempotency-Key": key})
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(retry, ("llm-a", "llm-b")))
        assert sorted(response.status_code for response in responses) == [200, 409]
        assert next(response for response in responses if response.status_code == 409).json()["code"] == "screening_retry_active"

    with app.state.identity_store.sync_session() as db:
        retried = db.get(ScreeningItem, item_id)
        jobs = list(db.scalars(select(BackgroundJob).where(BackgroundJob.type == "screening.llm_score_item")))
        assert retried.status == "scored" and retried.llm_status == "queued" and retried.llm_safe_error_code is None
        assert retried.llm_started_at is None and retried.llm_finished_at is None and retried.finished_at is None
        assert len(jobs) == 2 and db.get(BackgroundJob, old_job_id).status == "dead_letter"
        assert sum(job.status == "queued" for job in jobs) == 1 and len({job.dedupe_key for job in jobs}) == 2
        active_job = next(job for job in jobs if job.status == "queued")
        assert active_job.payload["application_id"] == str(application_id)
        assert db.scalar(select(func.count(ScreeningResult.id)).where(ScreeningResult.item_id == item_id)) == 1
        assert db.get(Application, application_id).stage == "new"
        assert db.scalar(select(func.count(AuditLog.id)).where(AuditLog.event_type == "screening.item_retried")) == 1


def test_postgres_concurrent_retry_preserves_history_and_restores_progress_once():
    url = os.environ["POSTGRES_SMOKE_URL"]
    subprocess.run(["python", "-m", "alembic", "-c", "server/alembic.ini", "upgrade", "head"], check=True, env={**os.environ, "DATABASE_URL": url})
    app = create_app(settings=Settings(environment="test", database_url=url, cors_origins=["https://hr.example.test"]), database_probe=Probe(), storage_probe=Probe(), quarantine_storage=object())
    with app.state.identity_store.sync_session() as db:
        db.execute(text("TRUNCATE organizations CASCADE"))
        org = Organization(slug="acme", name="Retry", status="active")
        user = User(organization=org, email="retry@pg.test", normalized_email="retry@pg.test", display_name="Retry", password_hash=PasswordService().hash("correct"))
        user.roles.append(UserRole(role="recruiting_admin")); db.add(user); db.flush()
        job = Job(organization_id=org.id, title="Retry", owner_id=user.id, status="draft"); db.add(job); db.flush()
        jd = JobJdVersion(organization_id=org.id, job_id=job.id, version_number=1, content={"text": "candidate"}, created_by=user.id)
        rule = ScreeningRuleVersion(organization_id=org.id, job_id=job.id, version_number=1, content={"required_terms": [], "bonus_terms": []}, created_by=user.id); db.add_all([jd, rule]); db.flush()
        run = ScreeningRun(organization_id=org.id, job_id=job.id, jd_version_id=jd.id, rule_version_id=rule.id, source="upload", status="failed", total_count=1, processed_count=1, succeeded_count=0, failed_count=1, created_by=user.id); db.add(run); db.flush()
        data = b"candidate"
        stored_file = FileObject(organization_id=org.id, storage_key="quarantine/retry", original_filename="candidate.txt", mime_type="text/plain", size_bytes=len(data), sha256=hashlib.sha256(data).hexdigest(), uploaded_by=user.id, storage_state="quarantine", detected_type="txt", scan_status="pending"); db.add(stored_file); db.flush()
        item = ScreeningItem(organization_id=org.id, run_id=run.id, file_object_id=stored_file.id, status="failed", safe_error_code="scanner_unavailable", attempts=3, finished_at=run.created_at); db.add(item); db.flush()
        prior = QueueRepository(db).enqueue(org.id, "screening.parse_item", {"organization_id": str(org.id), "screening_item_id": str(item.id), "parser_version": "parser-v1"}, dedupe_key=f"parse:{item.id}", max_attempts=3)
        prior.status = "dead_letter"; prior.attempts = 3; db.commit(); item_id, run_id, prior_id = item.id, run.id, prior.id

    with TestClient(app) as client:
        headers = login(client, "retry@pg.test")
        def retry(key):
            return client.post(f"/api/v1/screening-items/{item_id}/retry", headers={**headers, "Idempotency-Key": key})
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(retry, ("retry-a", "retry-b")))
        assert sorted(response.status_code for response in responses) == [200, 409]

    with app.state.identity_store.sync_session() as db:
        aggregate = db.get(ScreeningRun, run_id); retried = db.get(ScreeningItem, item_id)
        assert aggregate.processed_count == 0 and aggregate.failed_count == 0
        assert retried.status == "queued" and retried.safe_error_code is None and retried.attempts == 3
        assert db.get(BackgroundJob, prior_id).status == "dead_letter"
        assert db.scalar(select(func.count(BackgroundJob.id)).where(BackgroundJob.dedupe_key == f"parse:{item_id}")) == 2
        assert db.scalar(select(func.count(BackgroundJob.id)).where(BackgroundJob.dedupe_key == f"parse:{item_id}", BackgroundJob.status == "queued")) == 1

    storage=MemoryPipelineStorage(objects={"quarantine/retry":data}); pipeline=ScreeningPipeline(app.state.identity_store.sync_session,storage,Scanner(),app.state.settings)
    worker=Worker(Probe(),Probe(),interval_seconds=0,queue=DatabaseQueueGateway(url,terminal_callbacks=screening_terminal_callbacks()),handlers={"screening.parse_item":pipeline.parse_item,"screening.score_item":pipeline.score_item},outbox_handlers={},worker_id="retry-worker",lease_seconds=60,heartbeat_seconds=20)
    asyncio.run(worker._poll_once()); asyncio.run(worker._poll_once())
    with app.state.identity_store.sync_session() as db:
        assert db.get(BackgroundJob,prior_id).status=="dead_letter"
        completed=db.get(ScreeningRun,run_id); assert completed.status=="completed" and completed.processed_count==1
        assert db.get(ScreeningItem,item_id).status=="scored"


def test_postgres_bulk_action_is_atomic_scoped_and_manager_read_only():
    url = os.environ["POSTGRES_SMOKE_URL"]
    subprocess.run(["python", "-m", "alembic", "-c", "server/alembic.ini", "upgrade", "head"], check=True, env={**os.environ, "DATABASE_URL": url})
    app = create_app(settings=Settings(environment="test", database_url=url, cors_origins=["https://hr.example.test"]), database_probe=Probe(), storage_probe=Probe(), quarantine_storage=object())
    with app.state.identity_store.sync_session() as db:
        db.execute(text("TRUNCATE organizations CASCADE"))
        org = Organization(slug="acme", name="Bulk", status="active")
        admin = User(organization=org, email="admin@pg.test", normalized_email="admin@pg.test", display_name="Admin", password_hash=PasswordService().hash("correct")); admin.roles.append(UserRole(role="recruiting_admin"))
        manager = User(organization=org, email="manager@pg.test", normalized_email="manager@pg.test", display_name="Manager", password_hash=PasswordService().hash("correct")); manager.roles.append(UserRole(role="hiring_manager"))
        db.add_all([admin, manager]); db.flush()
        job = Job(organization_id=org.id, title="Bulk", owner_id=admin.id, status="draft"); db.add(job); db.flush(); db.add(JobCollaborator(organization_id=org.id, job_id=job.id, user_id=manager.id, access_role="job_manager"))
        jd = JobJdVersion(organization_id=org.id, job_id=job.id, version_number=1, content={"text":"required: Python"}, created_by=admin.id); rule = ScreeningRuleVersion(organization_id=org.id, job_id=job.id, version_number=1, content={}, created_by=admin.id); db.add_all([jd,rule]); db.flush()
        runs=[]; item_rows=[]
        for run_index,count in ((0,2),(1,1)):
            run=ScreeningRun(organization_id=org.id,job_id=job.id,jd_version_id=jd.id,rule_version_id=rule.id,source="upload",status="completed",total_count=count,processed_count=count,succeeded_count=count,failed_count=0,created_by=admin.id); db.add(run); db.flush(); runs.append(run)
            for item_index in range(count):
                suffix=f"{run_index}-{item_index}"; candidate=Candidate(organization_id=org.id,display_name=f"Candidate {suffix}",owner_id=admin.id); db.add(candidate); db.flush()
                file=FileObject(organization_id=org.id,storage_key=f"clean/{suffix}",original_filename=f"{suffix}.txt",mime_type="text/plain",size_bytes=1,sha256=(str(run_index)+str(item_index))*32,uploaded_by=admin.id,storage_state="clean",detected_type="txt",scan_status="clean"); db.add(file); db.flush()
                resume=Resume(organization_id=org.id,candidate_id=candidate.id,file_object_id=file.id,version_number=1,parsed_text="Python"); db.add(resume); db.flush()
                application=Application(organization_id=org.id,candidate_id=candidate.id,job_id=job.id,resume_id=resume.id,owner_id=admin.id,stage="new",source="screening"); db.add(application); db.flush()
                item=ScreeningItem(organization_id=org.id,run_id=run.id,file_object_id=file.id,candidate_id=candidate.id,resume_id=resume.id,application_id=application.id,status="scored",attempts=1); db.add(item); db.flush(); db.add(ScreeningResult(organization_id=org.id,item_id=item.id,application_id=application.id,resume_id=resume.id,rule_engine_version="rule-v1",rule_score=75,recommendation="可沟通",required_hits=["Python"],required_missing=[],bonus_hits=[],estimated_years=0,risks=[],questions=[])); item_rows.append((run,item,application))
        db.commit(); run_id=runs[0].id
        first=(item_rows[0][1].id,item_rows[0][2].id,item_rows[0][2].version); second=(item_rows[1][1].id,item_rows[1][2].id,item_rows[1][2].version); foreign=(item_rows[2][1].id,item_rows[2][2].id,item_rows[2][2].version)
    with TestClient(app) as client:
        headers=login(client,"admin@pg.test")
        stale={"command":"advance_to_review","items":[{"item_id":str(first[0]),"expected_application_version":first[2]},{"item_id":str(second[0]),"expected_application_version":second[2]+1}]}
        response=client.post(f"/api/v1/screening-runs/{run_id}/bulk-actions",json=stale,headers={**headers,"Idempotency-Key":"stale"}); assert response.status_code==409
        cross={"command":"advance_to_review","items":[{"item_id":str(foreign[0]),"expected_application_version":foreign[2]}]}
        response=client.post(f"/api/v1/screening-runs/{run_id}/bulk-actions",json=cross,headers={**headers,"Idempotency-Key":"cross"}); assert response.status_code==409
        client.post("/api/v1/auth/logout",headers=headers); manager_headers=login(client,"manager@pg.test")
        response=client.post(f"/api/v1/screening-runs/{run_id}/bulk-actions",json={"command":"advance_to_review","items":[{"item_id":str(first[0]),"expected_application_version":first[2]}]},headers={**manager_headers,"Idempotency-Key":"manager"}); assert response.status_code==404
    with app.state.identity_store.sync_session() as db:
        assert {db.get(Application,first[1]).stage,db.get(Application,second[1]).stage}=={"new"}
        assert db.scalar(select(func.count(ApplicationStageEvent.id)))==0
        assert db.scalar(select(func.count(AuditLog.id)).where(AuditLog.event_type=="application.stage_changed"))==0
