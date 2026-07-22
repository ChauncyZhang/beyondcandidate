import asyncio,io,uuid
from dataclasses import dataclass,field
from types import SimpleNamespace
from fastapi.testclient import TestClient
from sqlalchemy import func,select

from server.app.recruiting.models import Application,ApplicationReviewTask,Candidate,FileObject,Resume,ResumeProfile
from server.app.screening.models import ScreeningItem,ScreeningResult,ScreeningRun
from server.app.llm.models import LlmProviderConfig,PromptVersion
from server.app.queue.models import BackgroundJob
from server.app.screening.pipeline import ScreeningPipeline,_PROMPT_CONTENT,_ensure_screening_prompt
from server.app.llm.resume_profile import ResumeProfileResult
from server.app.recruiting.profile_builder import ProfileBuild,ResumeProfileBuilder
from server.app.recruiting.profile_jobs import ResumeProfileJobHandler,enqueue_missing_resume_profiles
from server.app.screening.scanner import ScanResult
from server.app.queue.service import PermanentJobError,RetryableJobError
import pytest
from server.tests.test_screening_api import app_and_seed,login

@dataclass
class MemoryPipelineStorage:
    objects:dict[str,bytes]=field(default_factory=dict); calls:list[str]=field(default_factory=list); delete_ok:bool=True; streams:list=field(default_factory=list); open_error:Exception|None=None
    async def open(self,key,max_bytes):
        self.calls.append("open")
        if self.open_error: raise self.open_error
        stream=TrackingStream(self.objects[key]); self.streams.append(stream); return stream
    async def copy(self,source,target,max_bytes): self.calls.append("copy"); self.objects[target]=self.objects[source]
    async def delete(self,key): self.calls.append("delete"); self.objects.pop(key,None) if self.delete_ok else None; return self.delete_ok

class Scanner:
    def __init__(self,result=ScanResult.CLEAN): self.result=result; self.calls=[]
    async def scan(self,stream,max_bytes): self.calls.append("scan"); return self.result

class TrackingStream(io.BytesIO):
    def __init__(self,data): super().__init__(data); self.close_count=0
    def close(self): self.close_count+=1; super().close()

def seeded_pipeline(tmp_path,text=b"required: Python\nPython 5 years",filename="Alice.txt",mime="text/plain"):
    app,upload_storage,job_id=app_and_seed(tmp_path)
    with TestClient(app) as client:
        headers=login(client,"admin@example.test"); run=client.post(f"/api/v1/jobs/{job_id}/screening-runs",json={},headers={**headers,"Idempotency-Key":"run"}).json()["data"]
        item=client.post(f"/api/v1/screening-runs/{run['id']}/items",files={"file":(filename,text,mime)},headers={**headers,"Idempotency-Key":"item"}).json()["data"]
    storage=MemoryPipelineStorage(dict(upload_storage.objects)); scanner=Scanner(); pipeline=ScreeningPipeline(app.state.identity_store.sync_session,storage,scanner,app.state.settings)
    return app,pipeline,storage,scanner,SimpleNamespace(organization_id=next(iter(storage.objects)).split("/")[1],payload={"organization_id":next(iter(storage.objects)).split("/")[1],"screening_item_id":item["id"],"parser_version":"parser-v1"},attempts=1,max_attempts=3),run,item

def test_job_definition_api_rule_boundaries_parse_and_score_without_truncation(tmp_path):
    app,upload_storage,_=app_and_seed(tmp_path)
    term=lambda prefix,index: f"{prefix}-{index:02d}-"+"x"*(100-len(f"{prefix}-{index:02d}-"))
    must_have=[term("required",index) for index in range(50)]; nice_to_have=[term("bonus",index) for index in range(50)]
    definition={"title":"Platform Engineer","department_id":None,"headcount":2,"priority":"high","hiring_owner_id":None,"description":"D"*50_000,"location":"Shanghai","process_template":"standard","llm_enabled":False,"must_have":must_have,"nice_to_have":nice_to_have,"publish":False}
    resume_text=" ".join([*must_have,*nice_to_have]).encode()
    with TestClient(app) as client:
        headers=login(client,"admin@example.test")
        created=client.post("/api/v1/job-definitions",json=definition,headers={**headers,"Idempotency-Key":"real-definition"})
        assert created.status_code==201
        too_long=client.post("/api/v1/job-definitions",json={**definition,"must_have":["x"*101]},headers={**headers,"Idempotency-Key":"too-long-rule"})
        too_many=client.post("/api/v1/job-definitions",json={**definition,"nice_to_have":["x"]*51},headers={**headers,"Idempotency-Key":"too-many-rules"})
        assert too_long.status_code==too_many.status_code==422
        job_id=created.json()["data"]["job"]["id"]
        run=client.post(f"/api/v1/jobs/{job_id}/screening-runs",json={},headers={**headers,"Idempotency-Key":"real-run"}).json()["data"]
        item=client.post(f"/api/v1/screening-runs/{run['id']}/items",files={"file":("candidate.txt",resume_text,"text/plain")},headers={**headers,"Idempotency-Key":"real-item"}).json()["data"]
    organization_id=next(iter(upload_storage.objects)).split("/")[1]
    pipeline=ScreeningPipeline(app.state.identity_store.sync_session,MemoryPipelineStorage(dict(upload_storage.objects)),Scanner(),app.state.settings)
    parse_job=SimpleNamespace(payload={"organization_id":organization_id,"screening_item_id":item["id"]},attempts=1,max_attempts=3)
    asyncio.run(pipeline.parse_item(parse_job))
    with app.state.identity_store.sync_session() as db:
        stored=db.get(ScreeningItem,uuid.UUID(item["id"])); aggregate=db.get(ScreeningRun,uuid.UUID(run["id"]))
        score_job=SimpleNamespace(payload={"organization_id":organization_id,"screening_item_id":item["id"],"jd_version_id":str(aggregate.jd_version_id),"rule_version_id":str(aggregate.rule_version_id),"rule_engine_version":"rule-v1"},attempts=1,max_attempts=3)
        assert stored.status=="parsed"
    asyncio.run(pipeline.score_item(score_job))
    with app.state.identity_store.sync_session() as db:
        stored=db.get(ScreeningItem,uuid.UUID(item["id"])); result=db.scalar(select(ScreeningResult).where(ScreeningResult.item_id==stored.id))
        assert stored.status=="scored" and stored.safe_error_code is None
        assert result.required_hits==must_have and result.bonus_hits==nice_to_have

def test_clean_parse_then_score_is_replay_safe_and_auto_sends_completed_resume_to_review(tmp_path):
    app,pipeline,storage,scanner,job,run,item=seeded_pipeline(tmp_path)
    asyncio.run(pipeline.parse_item(job))
    with app.state.identity_store.sync_session() as db: first_started=db.get(ScreeningItem,uuid.UUID(item["id"])).started_at
    asyncio.run(pipeline.parse_item(job))
    assert scanner.calls==["scan"] and storage.calls[:3]==["open","copy","delete"]
    assert storage.streams and all(stream.close_count==1 for stream in storage.streams)
    with app.state.identity_store.sync_session() as db:
        stored=db.get(ScreeningItem,uuid.UUID(item["id"])); profile=db.scalar(select(ResumeProfile)); assert stored.status=="parsed" and stored.attempts==1 and stored.started_at==first_started and stored.finished_at is None and stored.candidate_id and stored.resume_id and stored.application_id
        assert profile and profile.resume_id==stored.resume_id and profile.source=="rules"
        score_job=SimpleNamespace(organization_id=stored.organization_id,payload={"organization_id":str(stored.organization_id),"screening_item_id":str(stored.id),"jd_version_id":str(db.get(ScreeningRun,uuid.UUID(run["id"])).jd_version_id),"rule_version_id":str(db.get(ScreeningRun,uuid.UUID(run["id"])).rule_version_id),"rule_engine_version":"rule-v1"},attempts=1,max_attempts=3)
    asyncio.run(pipeline.score_item(score_job)); asyncio.run(pipeline.score_item(score_job))
    with app.state.identity_store.sync_session() as db:
        assert db.scalar(select(func.count(Candidate.id)))==1 and db.scalar(select(func.count(Resume.id)))==1 and db.scalar(select(func.count(Application.id)))==1 and db.scalar(select(func.count(ScreeningResult.id)))==1
        application=db.scalar(select(Application)); result=db.scalar(select(ScreeningResult)); completed=db.get(ScreeningRun,uuid.UUID(run["id"])); task=db.scalar(select(ApplicationReviewTask))
        assert application.stage=="review" and application.version==2 and application.source=="screening" and result.rule_score<=100 and completed.status=="completed" and completed.processed_count==1 and db.scalar(select(ScreeningItem.finished_at)).isoformat()
        assert task.ai_status=="failed" and task.safe_error_code=="llm_config_disabled"

def test_worker_startup_enqueues_missing_historical_resume_profiles(tmp_path):
    app,pipeline,_,_,job,_,item=seeded_pipeline(tmp_path)
    asyncio.run(pipeline.parse_item(job))
    with app.state.identity_store.sync_session() as db:
        db.query(ResumeProfile).delete()
        db.commit()

    assert enqueue_missing_resume_profiles(app.state.identity_store.sync_session)==1
    with app.state.identity_store.sync_session() as db:
        backfill=db.scalar(select(BackgroundJob).where(BackgroundJob.type=="screening.profile_resume"))
        assert backfill.payload=={
            "organization_id":str(backfill.organization_id),
            "resume_id":str(db.get(ScreeningItem,uuid.UUID(item["id"])).resume_id),
        }

def test_profile_job_rejects_tenant_mismatch_before_processing(tmp_path):
    app,pipeline,_,_,job,_,item=seeded_pipeline(tmp_path)
    asyncio.run(pipeline.parse_item(job))
    with app.state.identity_store.sync_session() as db:
        resume_id=db.get(ScreeningItem,uuid.UUID(item["id"])).resume_id
        organization_id=db.get(ScreeningItem,uuid.UUID(item["id"])).organization_id
        db.query(ResumeProfile).delete(); db.commit()
    handler=ResumeProfileJobHandler(app.state.identity_store.sync_session,None,None)
    profile_job=SimpleNamespace(
        organization_id=uuid.uuid4(),
        payload={"organization_id":str(organization_id),"resume_id":str(resume_id)},
    )
    with pytest.raises(PermanentJobError,match="resume_profile_payload_invalid"):
        asyncio.run(handler(profile_job))

def test_job_independent_profile_uses_unrestricted_llm_without_application_job(tmp_path):
    app,_,_,_,job,run,_=seeded_pipeline(tmp_path)
    organization_id=uuid.UUID(job.payload["organization_id"])
    with app.state.identity_store.sync_session() as db:
        owner=db.get(ScreeningRun,uuid.UUID(run["id"])).created_by
        db.add(LlmProviderConfig(
            organization_id=organization_id,
            provider_id="profile-provider",
            model="profile-model",
            encrypted_api_key=b"encrypted",
            enabled=True,
            allowed_job_ids=[],
            version=1,
            created_by=owner,
            updated_by=owner,
        ))
        db.commit()

    class Cipher:
        def decrypt(self,value): return "secret"

    class Gateway:
        async def extract_resume_profile(self,*args,**kwargs):
            result=ResumeProfileResult.model_validate({
                "summary":"具备企业级 AI 项目交付经验。",
                "summary_origin":"generated",
                "skills":["Python"],
                "experience":"负责 AI 平台研发。",
                "education":None,
                "evidence":{
                    "summary":["负责 AI 平台研发"],
                    "skills":["Python"],
                    "experience":["负责 AI 平台研发"],
                    "education":[],
                },
            })
            return SimpleNamespace(result=result,usage={},latency_ms=12)

    result=asyncio.run(ResumeProfileBuilder(
        app.state.identity_store.sync_session,Gateway(),Cipher()
    ).build(
        organization_id,
        job_id=None,
        resume_text="Python\n负责 AI 平台研发",
        candidate_name=None,
        used_ocr=False,
    ))
    assert result.source=="llm" and result.status=="partial"

def test_profile_job_does_not_restore_profile_after_candidate_is_deleted(tmp_path):
    app,pipeline,_,_,job,_,item=seeded_pipeline(tmp_path)
    asyncio.run(pipeline.parse_item(job))
    with app.state.identity_store.sync_session() as db:
        stored=db.get(ScreeningItem,uuid.UUID(item["id"])); resume_id=stored.resume_id
        organization_id=stored.organization_id
        db.query(ResumeProfile).delete(); db.commit()

    class DeletingEnhancer:
        async def enhance(self,*args,**kwargs):
            with app.state.identity_store.sync_session() as db:
                resume=db.get(Resume,resume_id)
                db.get(Candidate,resume.candidate_id).deleted_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc)
                db.commit()
            return SimpleNamespace(text="Python 5 years",used_ocr=False,safe_error_code=None)

    class Builder:
        async def build(self,*args,**kwargs):
            return ProfileBuild({"summary":None,"skills":["Python"],"experience":None,"education":None,"status":"partial","source":"rules"},"partial","rules")

    handler=ResumeProfileJobHandler(app.state.identity_store.sync_session,DeletingEnhancer(),Builder())
    profile_job=SimpleNamespace(
        organization_id=organization_id,
        payload={"organization_id":str(organization_id),"resume_id":str(resume_id)},
        trace_id="delete-race",
    )
    asyncio.run(handler(profile_job))
    with app.state.identity_store.sync_session() as db:
        assert db.scalar(select(ResumeProfile).where(ResumeProfile.resume_id==resume_id)) is None

def test_rule_score_atomically_enqueues_eligible_llm_without_finishing_item(tmp_path):
    app,pipeline,storage,scanner,job,run,item=seeded_pipeline(tmp_path); asyncio.run(pipeline.parse_item(job))
    with app.state.identity_store.sync_session() as db:
        stored=db.get(ScreeningItem,uuid.UUID(item["id"])); aggregate=db.get(ScreeningRun,uuid.UUID(run["id"])); db.add(LlmProviderConfig(organization_id=stored.organization_id,provider_id="approved",model="model",encrypted_api_key=b"encrypted",enabled=True,allowed_job_ids=[],version=3,created_by=aggregate.created_by,updated_by=aggregate.created_by)); db.commit()
        score_job=SimpleNamespace(payload={"organization_id":str(stored.organization_id),"screening_item_id":str(stored.id),"jd_version_id":str(aggregate.jd_version_id),"rule_version_id":str(aggregate.rule_version_id),"rule_engine_version":"rule-v1"},attempts=1,max_attempts=3,trace_id="llm-score")
    asyncio.run(pipeline.score_item(score_job)); asyncio.run(pipeline.score_item(score_job))
    with app.state.identity_store.sync_session() as db:
        stored=db.get(ScreeningItem,uuid.UUID(item["id"])); aggregate=db.get(ScreeningRun,uuid.UUID(run["id"])); jobs=list(db.scalars(select(BackgroundJob).where(BackgroundJob.type=="screening.llm_score_item"))); prompt=db.scalar(select(PromptVersion))
        assert stored.status=="scored" and stored.llm_status=="queued" and stored.finished_at is None
        assert aggregate.status=="llm_scoring" and aggregate.processed_count==0
        assert len(jobs)==1 and jobs[0].payload=={"organization_id":str(stored.organization_id),"screening_item_id":str(stored.id),"screening_result_id":str(db.scalar(select(ScreeningResult.id))),"application_id":str(stored.application_id),"config_id":str(db.scalar(select(LlmProviderConfig.id))),"config_version":3,"prompt_version_id":str(prompt.id)}
        assert "resume" not in str(jobs[0].payload).lower() and "jd" not in jobs[0].payload
        assert prompt.version_number==3 and prompt.content["schema_version"]=="screening-evaluation-v2"
        assert "recommendation" in prompt.content["system"] and "must not" in prompt.content["system"]
        assert all(value in prompt.content["system"] for value in ("core_capability (0-35)", "experience_depth (0-25)", "role_seniority (0-20)", "transferability (0-10)", "explicit_constraints (0-10)"))
        assert db.scalar(select(Application)).stage=="new"

def test_screening_prompt_versions_can_upgrade_without_identity_conflict(tmp_path):
    app,_,_=app_and_seed(tmp_path)
    with app.state.identity_store.sync_session() as db:
        user=db.scalar(select(__import__("server.app.identity.models",fromlist=["User"]).User)); organization_id=user.organization_id
        legacy={"system":"legacy v1 prompt with rule facts","schema_version":"screening-evaluation-v1"}
        first=_ensure_screening_prompt(db,organization_id,user.id,content=legacy,version_number=1)
        second=_ensure_screening_prompt(db,organization_id,user.id,content=_PROMPT_CONTENT,version_number=2)
        db.commit()
        assert first.id!=second.id and first.version_number==1 and second.version_number==2
        assert first.content==legacy and second.content["schema_version"]=="screening-evaluation-v2"
        assert db.scalar(select(func.count(PromptVersion.id)))==2

def test_infected_is_terminal_without_parse_or_identity(tmp_path):
    app,pipeline,storage,scanner,job,run,item=seeded_pipeline(tmp_path); scanner.result=ScanResult.INFECTED
    asyncio.run(pipeline.parse_item(job))
    with app.state.identity_store.sync_session() as db:
        assert db.get(ScreeningItem,uuid.UUID(item["id"])).safe_error_code=="malware_detected" and db.scalar(select(func.count(Candidate.id)))==0 and db.get(ScreeningRun,uuid.UUID(run["id"])).status=="failed"
    assert "copy" not in storage.calls
    assert [stream.close_count for stream in storage.streams]==[1]

def test_clean_promotion_retains_cleanup_marker_when_source_delete_fails(tmp_path):
    app,pipeline,storage,scanner,job,run,item=seeded_pipeline(tmp_path); storage.delete_ok=False
    asyncio.run(pipeline.parse_item(job))
    with app.state.identity_store.sync_session() as db:
        stored=db.get(ScreeningItem,uuid.UUID(item["id"])); file=db.get(__import__("server.app.recruiting.models",fromlist=["FileObject"]).FileObject,stored.file_object_id)
        assert file.storage_state=="clean" and file.quarantine_cleanup_key and file.quarantine_cleanup_key in storage.objects

def test_retryable_scan_failure_then_exhaustion_leaves_terminal_progress(tmp_path):
    app,pipeline,storage,scanner,job,run,item=seeded_pipeline(tmp_path); scanner.result=ScanResult.UNAVAILABLE
    with pytest.raises(RetryableJobError): asyncio.run(pipeline.parse_item(job))
    with app.state.identity_store.sync_session() as db:
        retrying=db.get(ScreeningItem,uuid.UUID(item["id"])); aggregate=db.get(ScreeningRun,uuid.UUID(run["id"])); assert retrying.status=="queued" and retrying.safe_error_code=="scanner_unavailable" and retrying.finished_at is None and aggregate.processed_count==0
    with TestClient(app) as client:
        headers=login(client,"admin@example.test"); listing=client.get(f"/api/v1/screening-runs/{run['id']}/items?status=queued",headers=headers); progress=client.get(f"/api/v1/screening-runs/{run['id']}",headers=headers); assert listing.status_code==200 and listing.json()["data"][0]["status"]=="queued" and progress.json()["data"]["processed_count"]==0
    job.attempts=job.max_attempts
    with pytest.raises(PermanentJobError): asyncio.run(pipeline.parse_item(job))
    with app.state.identity_store.sync_session() as db:
        stored=db.get(ScreeningItem,uuid.UUID(item["id"])); aggregate=db.get(ScreeningRun,uuid.UUID(run["id"]))
        assert stored.status=="failed" and stored.safe_error_code=="scanner_unavailable" and stored.attempts==3 and stored.started_at and stored.finished_at and aggregate.status=="failed" and aggregate.processed_count==1
    assert all(stream.close_count==1 for stream in storage.streams)

def test_parse_and_score_attempt_states_are_truthful_and_score_retry_returns_parsed(tmp_path,monkeypatch):
    app,pipeline,storage,scanner,job,run,item=seeded_pipeline(tmp_path); observed=[]
    original_scan=scanner.scan
    async def inspect_scan(stream,max_bytes):
        with app.state.identity_store.sync_session() as db: observed.append(db.get(ScreeningItem,uuid.UUID(item["id"])).status)
        return await original_scan(stream,max_bytes)
    scanner.scan=inspect_scan; asyncio.run(pipeline.parse_item(job)); assert observed==["parsing"]
    with app.state.identity_store.sync_session() as db:
        stored=db.get(ScreeningItem,uuid.UUID(item["id"])); aggregate=db.get(ScreeningRun,uuid.UUID(run["id"])); score_job=SimpleNamespace(payload={"organization_id":str(stored.organization_id),"screening_item_id":str(stored.id),"jd_version_id":str(aggregate.jd_version_id),"rule_version_id":str(aggregate.rule_version_id),"rule_engine_version":"rule-v1"},attempts=1,max_attempts=3)
    def fail_score(*args):
        with app.state.identity_store.sync_session() as db: observed.append(db.get(ScreeningItem,uuid.UUID(item["id"])).status)
        raise RuntimeError("private score failure")
    monkeypatch.setattr("server.app.screening.pipeline.score_resume",fail_score)
    with pytest.raises(RetryableJobError) as retry: asyncio.run(pipeline.score_item(score_job))
    assert retry.value.safe_code=="scoring_failed" and observed[-1]=="scoring"
    with app.state.identity_store.sync_session() as db:
        stored=db.get(ScreeningItem,uuid.UUID(item["id"])); aggregate=db.get(ScreeningRun,uuid.UUID(run["id"])); assert stored.status=="parsed" and stored.safe_error_code=="scoring_failed" and stored.finished_at is None and aggregate.processed_count==0
    score_job.attempts=3
    with pytest.raises(PermanentJobError): asyncio.run(pipeline.score_item(score_job))
    with app.state.identity_store.sync_session() as db: assert db.get(ScreeningItem,uuid.UUID(item["id"])).status=="failed" and db.get(ScreeningRun,uuid.UUID(run["id"])).processed_count==1

def test_storage_size_violation_is_permanent_not_unavailable(tmp_path):
    from server.app.screening.storage import StorageWriteFailed
    app,pipeline,storage,scanner,job,run,item=seeded_pipeline(tmp_path); storage.open_error=StorageWriteFailed("file_too_large")
    asyncio.run(pipeline.parse_item(job))
    with app.state.identity_store.sync_session() as db: assert db.get(ScreeningItem,uuid.UUID(item["id"])).safe_error_code=="file_too_large"

def test_pipeline_cancellation_closes_stream_and_kills_parser(tmp_path):
    from server.app.screening.isolated_parser import IsolatedParser
    app,pipeline,storage,scanner,job,run,item=seeded_pipeline(tmp_path); parser=IsolatedParser(timeout_seconds=30,worker_module="server.tests.parser_hang_worker"); pipeline.parser=parser
    async def scenario():
        task=asyncio.create_task(pipeline.parse_item(job));
        while parser.last_pid is None: await asyncio.sleep(.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError): await task
    asyncio.run(scenario()); assert all(stream.close_count==1 for stream in storage.streams)
    with pytest.raises(ProcessLookupError): __import__("os").kill(parser.last_pid,0)

@pytest.mark.parametrize(("filename","mime","content","version"),[
    ("resume.txt","text/plain",b"Python","txt-v1"),
    ("resume.docx","application/vnd.openxmlformats-officedocument.wordprocessingml.document",__import__("server.tests.test_screening",fromlist=["docx_bytes"]).docx_bytes("Python"),"docx-v2"),
    ("resume.pdf","application/pdf",__import__("server.tests.test_screening",fromlist=["pdf_bytes"]).pdf_bytes(),"pdf-v4"),
], ids=("txt", "docx", "pdf"))
def test_pipeline_parses_each_approved_document_type(tmp_path,filename,mime,content,version):
    app,pipeline,storage,scanner,job,run,item=seeded_pipeline(tmp_path,content,filename,mime); asyncio.run(pipeline.parse_item(job))
    with app.state.identity_store.sync_session() as db: assert db.get(ScreeningItem,uuid.UUID(item["id"])).parser_version==version

def test_run_aggregation_is_partial_for_mixed_terminal_facts(tmp_path):
    app,pipeline,storage,scanner,job,run,item=seeded_pipeline(tmp_path)
    with app.state.identity_store.sync_session() as db:
        first=db.get(ScreeningItem,uuid.UUID(item["id"])); aggregate=db.get(ScreeningRun,uuid.UUID(run["id"])); first.status="scored"; extra_file=FileObject(organization_id=first.organization_id,storage_key=f"quarantine/{first.organization_id}/{aggregate.id}/{uuid.uuid4()}",original_filename="failed.txt",mime_type="text/plain",size_bytes=1,sha256="0"*64,uploaded_by=aggregate.created_by,storage_state="rejected",detected_type="txt",scan_status="rejected"); db.add(extra_file); db.flush(); db.add(ScreeningItem(organization_id=first.organization_id,run_id=aggregate.id,file_object_id=extra_file.id,status="failed",safe_error_code="malware_detected",attempts=1)); aggregate.total_count=2; db.flush(); pipeline._aggregate(db,aggregate); db.commit()
        assert aggregate.status=="partial" and aggregate.processed_count==2 and aggregate.succeeded_count==1 and aggregate.failed_count==1

def test_score_handler_uses_exact_rule_version_and_rejects_malformed_snapshot(tmp_path):
    from server.app.recruiting.models import ScreeningRuleVersion
    app,uploads,job_id=app_and_seed(tmp_path)
    with app.state.identity_store.sync_session() as db:
        first=db.scalar(select(ScreeningRuleVersion).where(ScreeningRuleVersion.job_id==job_id)); first.content={"required_terms":["Python"],"bonus_terms":[]}; second=ScreeningRuleVersion(organization_id=first.organization_id,job_id=job_id,version_number=2,content={"required_terms":["Rust"],"bonus_terms":[]},created_by=first.created_by); db.add(second); db.commit(); first_id,second_id=first.id,second.id
    runs=[]; items=[]
    with TestClient(app) as client:
        headers=login(client,"admin@example.test")
        for index,rule_id in enumerate((first_id,second_id)):
            run=client.post(f"/api/v1/jobs/{job_id}/screening-runs",json={"rule_version_id":str(rule_id)},headers={**headers,"Idempotency-Key":f"run-{index}"}).json()["data"]; item=client.post(f"/api/v1/screening-runs/{run['id']}/items",files={"file":(f"candidate-{index}.txt",b"Python 5 years","text/plain")},headers={**headers,"Idempotency-Key":f"item-{index}"}).json()["data"]; runs.append(run); items.append(item)
    storage=MemoryPipelineStorage(dict(uploads.objects)); pipeline=ScreeningPipeline(app.state.identity_store.sync_session,storage,Scanner(),app.state.settings); scores=[]
    for run,item in zip(runs,items):
        parse_job=SimpleNamespace(payload={"organization_id":next(iter(storage.objects)).split("/")[1],"screening_item_id":item["id"],"parser_version":"parser-v1"},attempts=1,max_attempts=3,trace_id="rules"); asyncio.run(pipeline.parse_item(parse_job))
        with app.state.identity_store.sync_session() as db:
            stored=db.get(ScreeningItem,uuid.UUID(item["id"])); aggregate=db.get(ScreeningRun,uuid.UUID(run["id"])); score_job=SimpleNamespace(payload={"organization_id":str(stored.organization_id),"screening_item_id":str(stored.id),"jd_version_id":str(aggregate.jd_version_id),"rule_version_id":str(aggregate.rule_version_id),"rule_engine_version":"rule-v1"},attempts=1,max_attempts=3)
        asyncio.run(pipeline.score_item(score_job))
        with app.state.identity_store.sync_session() as db: scores.append(db.scalar(select(ScreeningResult.rule_score).where(ScreeningResult.item_id==uuid.UUID(item["id"]))))
    assert scores[0]!=scores[1]
    root=tmp_path/"bad"; root.mkdir(); app,pipeline,storage,scanner,job,run,item=seeded_pipeline(root); asyncio.run(pipeline.parse_item(job))
    with app.state.identity_store.sync_session() as db:
        stored=db.get(ScreeningItem,uuid.UUID(item["id"])); aggregate=db.get(ScreeningRun,uuid.UUID(run["id"])); db.get(__import__("server.app.recruiting.models",fromlist=["ScreeningRuleVersion"]).ScreeningRuleVersion,aggregate.rule_version_id).content={"required_terms":"secret text"}; db.commit(); score_job=SimpleNamespace(payload={"organization_id":str(stored.organization_id),"screening_item_id":str(stored.id),"jd_version_id":str(aggregate.jd_version_id),"rule_version_id":str(aggregate.rule_version_id),"rule_engine_version":"rule-v1"},attempts=1,max_attempts=3)
    with pytest.raises(PermanentJobError) as raised: asyncio.run(pipeline.score_item(score_job))
    assert raised.value.safe_code=="rule_snapshot_invalid"
    with app.state.identity_store.sync_session() as db: assert db.get(ScreeningItem,uuid.UUID(item["id"])).safe_error_code=="rule_snapshot_invalid"

def test_score_handler_rejects_malformed_jd_snapshot_permanently(tmp_path):
    from server.app.recruiting.models import JobJdVersion
    app,pipeline,storage,scanner,job,run,item=seeded_pipeline(tmp_path); asyncio.run(pipeline.parse_item(job))
    with app.state.identity_store.sync_session() as db:
        stored=db.get(ScreeningItem,uuid.UUID(item["id"])); aggregate=db.get(ScreeningRun,uuid.UUID(run["id"])); db.get(JobJdVersion,aggregate.jd_version_id).content={"text":["private"]}; db.commit(); score_job=SimpleNamespace(payload={"organization_id":str(stored.organization_id),"screening_item_id":str(stored.id),"jd_version_id":str(aggregate.jd_version_id),"rule_version_id":str(aggregate.rule_version_id),"rule_engine_version":"rule-v1"},attempts=1,max_attempts=3)
    with pytest.raises(PermanentJobError) as raised: asyncio.run(pipeline.score_item(score_job))
    assert raised.value.safe_code=="rule_snapshot_invalid"

def test_score_handler_rejects_conflicting_typed_and_legacy_jd_snapshot_permanently(tmp_path):
    from server.app.recruiting.models import JobJdVersion
    app,pipeline,storage,scanner,job,run,item=seeded_pipeline(tmp_path); asyncio.run(pipeline.parse_item(job))
    with app.state.identity_store.sync_session() as db:
        stored=db.get(ScreeningItem,uuid.UUID(item["id"])); aggregate=db.get(ScreeningRun,uuid.UUID(run["id"])); db.get(JobJdVersion,aggregate.jd_version_id).content={"description":"Python role","text":"Rust role"}; db.commit(); score_job=SimpleNamespace(payload={"organization_id":str(stored.organization_id),"screening_item_id":str(stored.id),"jd_version_id":str(aggregate.jd_version_id),"rule_version_id":str(aggregate.rule_version_id),"rule_engine_version":"rule-v1"},attempts=1,max_attempts=3)
    with pytest.raises(PermanentJobError) as raised: asyncio.run(pipeline.score_item(score_job))
    assert raised.value.safe_code=="rule_snapshot_invalid"
    with app.state.identity_store.sync_session() as db:
        stored=db.get(ScreeningItem,uuid.UUID(item["id"])); assert stored.status=="failed" and stored.safe_error_code=="rule_snapshot_invalid"

@pytest.mark.parametrize("malformed",[
    {"must_have":[],"nice_to_have":[],"unknown":[]},
    {"must_have":[],"nice_to_have":[],"required_terms":[],"bonus_terms":[]},
    {"must_have":[]},
    {"required_terms":[]},
    {"must_have":None,"nice_to_have":[]},
    {"required_terms":[],"bonus_terms":None},
])
def test_score_handler_marks_malformed_rule_shapes_permanent(tmp_path,malformed):
    from server.app.recruiting.models import ScreeningRuleVersion
    app,pipeline,storage,scanner,job,run,item=seeded_pipeline(tmp_path); asyncio.run(pipeline.parse_item(job))
    with app.state.identity_store.sync_session() as db:
        stored=db.get(ScreeningItem,uuid.UUID(item["id"])); aggregate=db.get(ScreeningRun,uuid.UUID(run["id"])); db.get(ScreeningRuleVersion,aggregate.rule_version_id).content=malformed; db.commit(); score_job=SimpleNamespace(payload={"organization_id":str(stored.organization_id),"screening_item_id":str(stored.id),"jd_version_id":str(aggregate.jd_version_id),"rule_version_id":str(aggregate.rule_version_id),"rule_engine_version":"rule-v1"},attempts=1,max_attempts=3)
    with pytest.raises(PermanentJobError) as raised: asyncio.run(pipeline.score_item(score_job))
    assert raised.value.safe_code=="rule_snapshot_invalid"
    with app.state.identity_store.sync_session() as db:
        stored=db.get(ScreeningItem,uuid.UUID(item["id"])); assert stored.status=="failed" and stored.safe_error_code=="rule_snapshot_invalid"
