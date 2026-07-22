import asyncio,hashlib,os,subprocess,uuid
from datetime import timedelta
import pytest
from sqlalchemy import func,select,text
from server.app.core.settings import Settings
from server.app.identity.models import Job,Organization,User,UserRole
from server.app.identity.security import PasswordService
from server.app.main import create_app
from server.app.recruiting.models import Application,Candidate,FileObject,JobJdVersion,Resume,ScreeningRuleVersion
from server.app.screening.models import ScreeningItem,ScreeningResult,ScreeningRun
from server.app.screening.pipeline import ScreeningPipeline
from server.app.queue.models import BackgroundJob,JobAttempt
from server.app.queue.repository import QueueRepository
from server.app.queue.runtime import DatabaseQueueGateway
from server.app.worker.main import Worker
from server.tests.test_screening_api import Probe
from server.tests.test_screening_pipeline import MemoryPipelineStorage,Scanner

pytestmark=pytest.mark.skipif(not os.getenv("POSTGRES_SMOKE_URL"),reason="PostgreSQL smoke URL not configured")
def test_postgres_100_item_restart_replay_has_no_duplicates_or_lost_progress():
    url=os.environ["POSTGRES_SMOKE_URL"]; subprocess.run(["python","-m","alembic","-c","server/alembic.ini","upgrade","head"],check=True,env={**os.environ,"DATABASE_URL":url}); app=create_app(settings=Settings(environment="test",database_url=url,cors_origins=["https://hr.example.test"]),database_probe=Probe(),storage_probe=Probe(),quarantine_storage=object()); storage=MemoryPipelineStorage(); scanner=Scanner()
    with app.state.identity_store.sync_session() as db:
        db.execute(text("TRUNCATE organizations CASCADE")); org=Organization(slug="bulk",name="Bulk",status="active"); user=User(organization=org,email="bulk@test",normalized_email="bulk@test",display_name="Bulk",password_hash=PasswordService().hash("correct")); user.roles.append(UserRole(role="recruiting_admin")); db.add(user); db.flush(); job=Job(organization_id=org.id,title="Bulk",owner_id=user.id,status="draft"); db.add(job); db.flush(); jd=JobJdVersion(organization_id=org.id,job_id=job.id,version_number=1,content={"text":"required: Python"},created_by=user.id); rule=ScreeningRuleVersion(organization_id=org.id,job_id=job.id,version_number=1,content={"required_terms":[],"bonus_terms":[]},created_by=user.id); db.add_all([jd,rule]); db.flush(); run=ScreeningRun(organization_id=org.id,job_id=job.id,jd_version_id=jd.id,rule_version_id=rule.id,source="upload",status="parsing",total_count=100,processed_count=0,succeeded_count=0,failed_count=0,created_by=user.id); db.add(run); db.flush(); item_ids=[]
        for index in range(100):
            data=f"Python {index%10} years".encode(); file=FileObject(organization_id=org.id,storage_key=f"quarantine/{org.id}/{run.id}/{uuid.uuid4()}",original_filename=f"candidate-{index}.txt",mime_type="text/plain",size_bytes=len(data),sha256=hashlib.sha256(data).hexdigest(),uploaded_by=user.id,storage_state="quarantine",detected_type="txt",scan_status="pending"); db.add(file); db.flush(); item=ScreeningItem(organization_id=org.id,run_id=run.id,file_object_id=file.id,status="queued",attempts=0); db.add(item); db.flush(); item_ids.append(item.id); storage.objects[file.storage_key]=data
        queue=QueueRepository(db)
        for item_id in item_ids: queue.enqueue(org.id,"screening.parse_item",{"organization_id":str(org.id),"screening_item_id":str(item_id),"parser_version":"parser-v1"},dedupe_key=f"parse:{item_id}",trace_id="bulk",max_attempts=3)
        db.commit(); org_id,run_id=org.id,run.id
    pipeline=ScreeningPipeline(app.state.identity_store.sync_session,storage,scanner,app.state.settings)
    with app.state.identity_store.sync_session() as db: abandoned=QueueRepository(db).claim(org_id,"old-worker",lease_seconds=60); abandoned_id=abandoned.id; reclaimed_item_id=uuid.UUID(abandoned.payload["screening_item_id"]); db.commit()
    with app.state.identity_store.sync_session() as db: stale=db.get(BackgroundJob,abandoned_id); stale.lease_expires_at=stale.lease_expires_at-timedelta(seconds=120); db.commit()
    async def drain():
        worker=Worker(Probe(),Probe(),interval_seconds=0,queue=DatabaseQueueGateway(url),handlers={"screening.parse_item":pipeline.parse_item,"screening.score_item":pipeline.score_item},outbox_handlers={},worker_id="fresh-worker",lease_seconds=60,heartbeat_seconds=20)
        for _ in range(201): await worker._poll_once()
    asyncio.run(drain())
    with app.state.identity_store.sync_session() as db:
        assert db.scalar(select(func.count(Candidate.id)))==100 and db.scalar(select(func.count(Resume.id)))==100 and db.scalar(select(func.count(Application.id)))==100 and db.scalar(select(func.count(ScreeningResult.id)))==100
        completed=db.get(ScreeningRun,run_id); assert completed.status=="completed" and completed.processed_count==100 and completed.succeeded_count==100 and completed.failed_count==0
        assert set(db.scalars(select(Application.stage)))=={"new"}
        assert db.scalar(select(func.count(BackgroundJob.id)).where(BackgroundJob.status=="succeeded"))==200
        assert db.scalar(select(func.count(JobAttempt.id)))==201 and len(set(db.execute(select(JobAttempt.job_id,JobAttempt.attempt_no)).all()))==201
        items=list(db.scalars(select(ScreeningItem))); assert all(item.status=="scored" and item.started_at and item.finished_at for item in items); assert next(item for item in items if item.id==reclaimed_item_id).attempts==2; assert all(item.attempts==1 for item in items if item.id!=reclaimed_item_id)
    assert len(storage.streams)==200 and all(stream.close_count==1 for stream in storage.streams)
