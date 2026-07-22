import asyncio,hashlib,os,subprocess,uuid
import pytest
from sqlalchemy import func,select,text
from server.app.core.settings import Settings
from server.app.identity.models import AuditLog,Job,Organization,User,UserRole
from server.app.identity.security import PasswordService
from server.app.llm.models import LlmProviderConfig,LlmScreeningEvaluation,PromptVersion
from server.app.main import create_app
from server.app.queue.models import BackgroundJob,JobAttempt
from server.app.queue.repository import QueueRepository
from server.app.queue.runtime import DatabaseQueueGateway
from server.app.recruiting.models import Application,ApplicationReviewTask,ApplicationStageEvent,Candidate,FileObject,JobJdVersion,Resume,ScreeningRuleVersion
from server.app.screening.models import ScreeningItem,ScreeningResult,ScreeningRun
from server.app.screening.terminal import LlmTerminalFinalizer,finalize_llm_dead_letter,finalize_screening_dead_letter,screening_terminal_callbacks
from server.app.worker.main import Worker
from server.tests.test_screening_api import Probe

pytestmark=pytest.mark.skipif(not os.getenv("POSTGRES_SMOKE_URL"),reason="PostgreSQL smoke URL not configured")
def test_unexpected_parse_and_score_exhaustion_atomically_finalize_domain_facts():
    url=os.environ["POSTGRES_SMOKE_URL"]; subprocess.run(["python","-m","alembic","-c","server/alembic.ini","upgrade","head"],check=True,env={**os.environ,"DATABASE_URL":url}); app=create_app(settings=Settings(environment="test",database_url=url,cors_origins=["https://hr.example.test"]),database_probe=Probe(),storage_probe=Probe(),quarantine_storage=object())
    with app.state.identity_store.sync_session() as db:
        db.execute(text("TRUNCATE organizations CASCADE")); org=Organization(slug="dead",name="Dead",status="active"); user=User(organization=org,email="dead@test",normalized_email="dead@test",display_name="Dead",password_hash=PasswordService().hash("correct")); user.roles.append(UserRole(role="recruiting_admin")); db.add(user); db.flush(); job=Job(organization_id=org.id,title="Dead",owner_id=user.id,status="draft"); db.add(job); db.flush(); jd=JobJdVersion(organization_id=org.id,job_id=job.id,version_number=1,content={"text":"required: Python"},created_by=user.id); rule=ScreeningRuleVersion(organization_id=org.id,job_id=job.id,version_number=1,content={},created_by=user.id); db.add_all([jd,rule]); db.flush(); run=ScreeningRun(organization_id=org.id,job_id=job.id,jd_version_id=jd.id,rule_version_id=rule.id,source="upload",status="parsing",total_count=2,processed_count=0,succeeded_count=0,failed_count=0,created_by=user.id); db.add(run); db.flush(); items=[]
        for index,status in enumerate(("parsing","parsed")):
            data=b"Python"; file=FileObject(organization_id=org.id,storage_key=f"clean/{org.id}/{uuid.uuid4()}",original_filename=f"{index}.txt",mime_type="text/plain",size_bytes=len(data),sha256=hashlib.sha256(data).hexdigest(),uploaded_by=user.id,storage_state="clean",detected_type="txt",scan_status="clean"); db.add(file); db.flush(); item=ScreeningItem(organization_id=org.id,run_id=run.id,file_object_id=file.id,status=status,attempts=1); db.add(item); db.flush(); items.append(item)
        repo=QueueRepository(db); parse=repo.enqueue(org.id,"screening.parse_item",{"organization_id":str(org.id),"screening_item_id":str(items[0].id),"parser_version":"parser-v1"},dedupe_key="dead-parse",max_attempts=1); score=repo.enqueue(org.id,"screening.score_item",{"organization_id":str(org.id),"screening_item_id":str(items[1].id),"jd_version_id":str(jd.id),"rule_version_id":str(rule.id),"rule_engine_version":"rule-v1"},dedupe_key="dead-score",max_attempts=1); db.commit(); org_id,run_id,item_ids,job_ids=org.id,run.id,[item.id for item in items],[parse.id,score.id]
    async def explode(_): raise RuntimeError("private resume body")
    worker=Worker(Probe(),Probe(),interval_seconds=0,queue=DatabaseQueueGateway(url,terminal_callbacks=screening_terminal_callbacks()),handlers={"screening.parse_item":explode,"screening.score_item":explode},outbox_handlers={},worker_id="dead-worker",lease_seconds=30,heartbeat_seconds=10)
    asyncio.run(worker._poll_once()); asyncio.run(worker._poll_once())
    with app.state.identity_store.sync_session() as db:
        assert set(db.scalars(select(BackgroundJob.status).where(BackgroundJob.id.in_(job_ids))))=={"dead_letter"}; stored=[db.get(ScreeningItem,item_id) for item_id in item_ids]; aggregate=db.get(ScreeningRun,run_id); assert all(item.status=="failed" and item.finished_at and item.safe_error_code=="handler_failed" for item in stored); assert aggregate.status=="failed" and aggregate.processed_count==2 and aggregate.failed_count==2
        assert db.scalar(select(func.count(ApplicationReviewTask.id)))==0
        for job_id in job_ids: finalize_screening_dead_letter(db,db.get(BackgroundJob,job_id),"handler_failed",db.get(BackgroundJob,job_id).updated_at)
        db.commit(); assert db.get(ScreeningRun,run_id).processed_count==2


def test_llm_worker_dead_letter_routes_only_complete_relational_context_once():
    url=os.environ["POSTGRES_SMOKE_URL"]
    subprocess.run(["python","-m","alembic","-c","server/alembic.ini","upgrade","head"],check=True,env={**os.environ,"DATABASE_URL":url})
    app=create_app(settings=Settings(environment="test",database_url=url,cors_origins=["https://hr.example.test"]),database_probe=Probe(),storage_probe=Probe(),quarantine_storage=object())
    with app.state.identity_store.sync_session() as db:
        db.execute(text("TRUNCATE organizations CASCADE"))
        org=Organization(slug="llm-dead",name="LLM Dead",status="active")
        user=User(organization=org,email="llm-dead@test",normalized_email="llm-dead@test",display_name="LLM Dead",password_hash=PasswordService().hash("correct"))
        user.roles.append(UserRole(role="recruiting_admin")); db.add(user); db.flush()
        hiring_job=Job(organization_id=org.id,title="LLM Dead",owner_id=user.id,hiring_owner_id=user.id,status="draft"); db.add(hiring_job); db.flush()
        jd=JobJdVersion(organization_id=org.id,job_id=hiring_job.id,version_number=1,content={"description":"Python"},created_by=user.id)
        rule=ScreeningRuleVersion(organization_id=org.id,job_id=hiring_job.id,version_number=1,content={},created_by=user.id); db.add_all([jd,rule]); db.flush()
        run=ScreeningRun(organization_id=org.id,job_id=hiring_job.id,jd_version_id=jd.id,rule_version_id=rule.id,source="upload",status="llm_scoring",total_count=1,processed_count=0,succeeded_count=0,failed_count=0,created_by=user.id); db.add(run); db.flush()
        data=b"Python"; stored_file=FileObject(organization_id=org.id,storage_key=f"clean/{org.id}/{uuid.uuid4()}",original_filename="candidate.txt",mime_type="text/plain",size_bytes=len(data),sha256=hashlib.sha256(data).hexdigest(),uploaded_by=user.id,storage_state="clean",detected_type="txt",scan_status="clean"); db.add(stored_file); db.flush()
        candidate=Candidate(organization_id=org.id,display_name="Candidate",owner_id=user.id); db.add(candidate); db.flush()
        resume=Resume(organization_id=org.id,candidate_id=candidate.id,file_object_id=stored_file.id,version_number=1,parsed_text="Python"); db.add(resume); db.flush()
        application=Application(organization_id=org.id,candidate_id=candidate.id,job_id=hiring_job.id,resume_id=resume.id,owner_id=user.id,stage="new",source="screening"); db.add(application); db.flush()
        item=ScreeningItem(organization_id=org.id,run_id=run.id,file_object_id=stored_file.id,candidate_id=candidate.id,resume_id=resume.id,application_id=application.id,status="scored",llm_status="queued",attempts=1); db.add(item); db.flush()
        result=ScreeningResult(organization_id=org.id,item_id=item.id,application_id=application.id,resume_id=resume.id,rule_engine_version="rule-v1",rule_score=50,recommendation="暂缓",required_hits=[],required_missing=[],bonus_hits=[],estimated_years=0,risks=[],questions=[]); db.add(result)
        config=LlmProviderConfig(organization_id=org.id,provider_id="approved",model="model",encrypted_api_key=b"encrypted",enabled=True,allowed_job_ids=[],version=3,created_by=user.id,updated_by=user.id); db.add(config)
        prompt=PromptVersion(organization_id=org.id,name="screening-evaluation",version_number=2,content={"system":"safe"},content_hash="a"*64,created_by=user.id); db.add(prompt); db.flush()
        queue_job=QueueRepository(db).enqueue(org.id,"screening.llm_score_item",{"organization_id":str(org.id),"screening_item_id":str(item.id),"screening_result_id":str(result.id),"config_id":str(config.id),"config_version":config.version,"prompt_version_id":str(prompt.id)},dedupe_key="llm-dead",max_attempts=1)
        db.commit(); ids=(queue_job.id,item.id,run.id,application.id,org.id)
    async def explode(_): raise RuntimeError("private resume provider body")
    finalizer=LlmTerminalFinalizer(app.state.identity_store.sync_session)
    worker=Worker(Probe(),Probe(),interval_seconds=0,queue=DatabaseQueueGateway(url,terminal_callbacks=screening_terminal_callbacks()),handlers={"screening.llm_score_item":explode,"screening.llm_finalize_terminal":finalizer},outbox_handlers={},worker_id="llm-dead-worker",lease_seconds=30,heartbeat_seconds=10)
    asyncio.run(worker._poll_once())
    with app.state.identity_store.sync_session() as db:
        queue_job=db.get(BackgroundJob,ids[0]); item=db.get(ScreeningItem,ids[1]); run=db.get(ScreeningRun,ids[2]); application=db.get(Application,ids[3])
        finalizer_job=db.scalar(select(BackgroundJob).where(BackgroundJob.type=="screening.llm_finalize_terminal"))
        assert queue_job.status=="dead_letter" and finalizer_job.status=="queued"
        assert "application_id" not in finalizer_job.payload
        assert item.llm_status=="queued" and item.llm_safe_error_code is None
        assert run.status=="llm_scoring" and application.stage=="new"
        assert db.scalar(select(func.count(ApplicationReviewTask.id)))==0
        assert db.scalar(select(func.count(ApplicationStageEvent.id)))==0
        assert db.scalar(select(func.count(AuditLog.id)).where(AuditLog.event_type=="screening.terminal_routed"))==0
        finalizer_id=finalizer_job.id
    for expected_attempt in range(1,4):
        with app.state.identity_store.sync_session() as db:
            repo=QueueRepository(db,terminal_callbacks=screening_terminal_callbacks())
            pending=db.get(BackgroundJob,finalizer_id); pending.run_after=repo.database_now(); db.flush()
            claimed=repo.claim(ids[4],"failing-finalizer",lease_seconds=30,recover_expired=False)
            assert claimed.id==finalizer_id and claimed.attempts==expected_attempt
            repo.fail(ids[4],finalizer_id,"failing-finalizer",safe_code="queue_unavailable",retryable=True)
            db.commit()
    with app.state.identity_store.sync_session() as db:
        repo=QueueRepository(db,terminal_callbacks=screening_terminal_callbacks())
        finalizer_job=db.get(BackgroundJob,finalizer_id); item=db.get(ScreeningItem,ids[1]); application=db.get(Application,ids[3])
        assert finalizer_job.status=="queued" and finalizer_job.attempts==3 and finalizer_job.max_attempts==6
        assert finalizer_job.run_after>finalizer_job.updated_at
        assert item.llm_status=="queued" and application.stage=="new"
        assert db.scalar(select(func.count(ApplicationReviewTask.id)))==0
        assert db.scalar(select(func.count(ApplicationStageEvent.id)))==0
        assert db.scalar(select(func.count(AuditLog.id)).where(AuditLog.event_type=="screening.terminal_routed"))==0
        before=(finalizer_job.attempts,finalizer_job.max_attempts,finalizer_job.run_after)
        screening_terminal_callbacks()["screening.llm_finalize_terminal"](db,finalizer_job,"queue_unavailable",repo.database_now())
        assert (finalizer_job.attempts,finalizer_job.max_attempts,finalizer_job.run_after)==before
        finalizer_job.run_after=repo.database_now(); db.commit()
    asyncio.run(worker._poll_once())
    with app.state.identity_store.sync_session() as db:
        queue_job=db.get(BackgroundJob,ids[0]); item=db.get(ScreeningItem,ids[1]); run=db.get(ScreeningRun,ids[2]); application=db.get(Application,ids[3])
        finalizer_job=db.scalar(select(BackgroundJob).where(BackgroundJob.type=="screening.llm_finalize_terminal"))
        assert finalizer_job.status=="succeeded" and finalizer_job.attempts==4 and finalizer_job.max_attempts==6
        assert item.llm_status=="failed" and item.llm_safe_error_code=="llm_handler_failed"
        assert run.status=="partial" and application.stage=="review"
        assert db.scalar(select(func.count(ApplicationReviewTask.id)))==1
        assert db.scalar(select(func.count(ApplicationStageEvent.id)))==1
        assert db.scalar(select(func.count(AuditLog.id)).where(AuditLog.event_type=="screening.terminal_routed"))==1
        assert db.scalar(select(func.count(LlmScreeningEvaluation.id)))==0
        attempts=list(db.scalars(select(JobAttempt.attempt_no).where(JobAttempt.job_id==finalizer_job.id).order_by(JobAttempt.attempt_no)))
        assert attempts==[1,2,3,4]
        finalize_llm_dead_letter(db,queue_job,"private resume provider body",queue_job.updated_at); db.commit()
        assert db.scalar(select(func.count(ApplicationReviewTask.id)))==1
        assert db.scalar(select(func.count(ApplicationStageEvent.id)))==1
