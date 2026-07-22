import io
import uuid
from datetime import datetime,timedelta,timezone
from dataclasses import dataclass, field

from fastapi.testclient import TestClient

from server.app.core.settings import Settings
from server.app.identity.models import AuditLog, Job, JobCollaborator, Organization, User, UserRole
from server.app.identity.security import PasswordService
from server.app.llm.models import LlmInvocation, LlmProviderConfig, LlmScreeningEvaluation, PromptVersion
from server.app.main import create_app
from server.app.recruiting.models import Application, Candidate, CandidateContact, FileObject, JobJdVersion, Resume, ScreeningRuleVersion
from server.app.screening.storage import StorageWriteFailed
from server.app.screening.models import ScreeningItem, ScreeningResult, ScreeningRun
from server.app.queue.models import BackgroundJob
from sqlalchemy import select

class Probe:
    async def check(self): pass

@dataclass
class FakeQuarantineStorage:
    objects: dict[str,bytes]=field(default_factory=dict); writes:int=0; deletes:int=0
    fail:bool=False
    def write(self, stream, storage_key, content_type, max_bytes):
        if self.fail: raise StorageWriteFailed("storage_write_failed")
        data=b""; size=0
        while chunk:=stream.read(3):
            size+=len(chunk)
            if size>max_bytes: raise ValueError("file_too_large")
            data+=chunk
        self.objects[storage_key]=data; self.writes+=1
    def delete(self, storage_key): self.objects.pop(storage_key,None); self.deletes+=1

def app_and_seed(tmp_path):
    storage=FakeQuarantineStorage(); app=create_app(settings=Settings(environment="test",database_url=f"sqlite+aiosqlite:///{tmp_path/'screening.db'}",cors_origins=["https://hr.example.test"]),database_probe=Probe(),storage_probe=Probe(),initialize_identity_schema=True,quarantine_storage=storage)
    app.state.identity_store.create_schema()
    with app.state.identity_store.sync_session() as db:
        org=Organization(slug="acme",name="Acme",status="active"); admin=User(organization=org,email="admin@example.test",normalized_email="admin@example.test",display_name="Admin",password_hash=PasswordService().hash("correct")); admin.roles.append(UserRole(role="recruiting_admin")); system=User(organization=org,email="system@example.test",normalized_email="system@example.test",display_name="System",password_hash=PasswordService().hash("correct")); system.roles.append(UserRole(role="system_admin")); manager=User(organization=org,email="manager@example.test",normalized_email="manager@example.test",display_name="Manager",password_hash=PasswordService().hash("correct")); manager.roles.append(UserRole(role="hiring_manager")); db.add_all([admin,system,manager]); db.flush(); job=Job(organization_id=org.id,title="Engineer",owner_id=admin.id,status="draft"); db.add(job); db.flush(); db.add(JobCollaborator(organization_id=org.id,job_id=job.id,user_id=manager.id,access_role="job_manager")); jd=JobJdVersion(organization_id=org.id,job_id=job.id,version_number=1,content={"text":"required: Python"},created_by=admin.id); rule=ScreeningRuleVersion(organization_id=org.id,job_id=job.id,version_number=1,content={"required_terms":[],"bonus_terms":[]},created_by=admin.id); db.add_all([jd,rule]); db.commit(); return app,storage,job.id

def login(client,email,organization_slug="acme"):
    response=client.post("/api/v1/auth/login",json={"organization_slug":organization_slug,"email":email,"password":"correct"},headers={"Origin":"https://hr.example.test"}); assert response.status_code==200; return {"Origin":"https://hr.example.test","X-CSRF-Token":response.headers["X-CSRF-Token"]}

def test_screening_runs_are_listed_with_display_context_and_job_scope(tmp_path):
    app,_,job_id=app_and_seed(tmp_path)
    with TestClient(app) as client:
        admin_headers=login(client,"admin@example.test")
        created=client.post(f"/api/v1/jobs/{job_id}/screening-runs",json={},headers={**admin_headers,"Idempotency-Key":"run-list"})
        assert created.status_code==201
        listing=client.get("/api/v1/screening-runs?limit=50",headers=admin_headers)
        assert listing.status_code==200
        listed=listing.json()["data"]
        assert len(listed)==1
        assert {key:listed[0][key] for key in ("id","job_id","status","total_count")}=={key:created.json()["data"][key] for key in ("id","job_id","status","total_count")}
        assert listed[0]["job_title"]=="Engineer" and listed[0]["created_by_name"]=="Admin"
        assert listing.json()["meta"]=={"limit":50,"next_cursor":None}

        client.post("/api/v1/auth/logout",headers=admin_headers)
        system_headers=login(client,"system@example.test")
        denied=client.get("/api/v1/screening-runs?limit=50",headers=system_headers)
        assert denied.status_code==200 and denied.json()["data"]==[]

def test_screening_review_progress_updates_after_human_conclusion(tmp_path):
    app,_,job_id=app_and_seed(tmp_path)
    with TestClient(app) as client:
        headers=login(client,"admin@example.test")
        run=client.post(f"/api/v1/jobs/{job_id}/screening-runs",json={},headers={**headers,"Idempotency-Key":"review-run"}).json()["data"]
        item=client.post(f"/api/v1/screening-runs/{run['id']}/items",files={"file":("review.txt",b"Python","text/plain")},headers={**headers,"Idempotency-Key":"review-item"}).json()["data"]
        with app.state.identity_store.sync_session() as db:
            candidate,_=enrich_item(db,item["id"],"Review Candidate")
            stored=db.get(ScreeningItem,uuid.UUID(item["id"])); aggregate=db.get(ScreeningRun,stored.run_id)
            resume=Resume(organization_id=stored.organization_id,candidate_id=candidate.id,file_object_id=stored.file_object_id,version_number=1,parsed_text="Python")
            db.add(resume); db.flush()
            application=Application(organization_id=stored.organization_id,candidate_id=candidate.id,job_id=aggregate.job_id,resume_id=resume.id,owner_id=aggregate.created_by,stage="new",source="screening")
            db.add(application); db.flush()
            stored.resume_id=resume.id; stored.application_id=application.id
            aggregate.status="completed"; aggregate.total_count=1; aggregate.processed_count=1; aggregate.succeeded_count=1
            db.commit(); application_id=str(application.id)

        before=client.get(f"/api/v1/screening-runs/{run['id']}",headers=headers).json()["data"]
        assert {key:before[key] for key in ("review_total_count","reviewed_count","review_pending_count","review_status")}=={"review_total_count":1,"reviewed_count":0,"review_pending_count":1,"review_status":"pending"}

        saved=client.patch(f"/api/v1/applications/{application_id}",json={"human_conclusion":"建议推进"},headers={**headers,"If-Match":'"1"'})
        assert saved.status_code==200
        detail=client.get(f"/api/v1/screening-runs/{run['id']}",headers=headers).json()["data"]
        listing=client.get("/api/v1/screening-runs?limit=50",headers=headers).json()["data"][0]
        rows=client.get(f"/api/v1/screening-runs/{run['id']}/items",headers=headers).json()["data"]
        expected={"review_total_count":1,"reviewed_count":1,"review_pending_count":0,"review_status":"completed"}
        assert {key:detail[key] for key in expected}==expected
        assert {key:listing[key] for key in expected}==expected
        assert rows[0]["human_reviewed"] is True

        advanced=client.post(f"/api/v1/screening-runs/{run['id']}/bulk-actions",json={"command":"advance_to_review","items":[{"item_id":item["id"],"expected_application_version":2}]},headers={**headers,"Idempotency-Key":"submit-review"})
        assert advanced.status_code==200
        approved=client.post(f"/api/v1/applications/{application_id}/workflow-actions",json={"action":"review_approved"},headers={**headers,"If-Match":'"3"',"Idempotency-Key":"approve-review"})
        assert approved.status_code==200
        after_approval=client.get(f"/api/v1/screening-runs/{run['id']}",headers=headers).json()["data"]
        assert after_approval["reviewed_count"]==1 and after_approval["review_approved_count"]==1 and after_approval["review_status"]=="completed"

def json_keys_and_scalar_values(value):
    keys=set(); values=[]
    def visit(node):
        if isinstance(node,dict):
            for key,nested in node.items(): keys.add(key); visit(nested)
        elif isinstance(node,list):
            for nested in node: visit(nested)
        else: values.append(node)
    visit(value)
    return keys,values

def enrich_item(db,item_id,name,*,score=81,recommendation="可沟通",created_at=None,result_id=None):
    item=db.get(ScreeningItem,uuid.UUID(item_id)); run=db.get(ScreeningRun,item.run_id)
    candidate=Candidate(organization_id=item.organization_id,display_name=name,owner_id=run.created_by); db.add(candidate); db.flush()
    item.candidate_id=candidate.id; item.status="scored"
    result=ScreeningResult(id=result_id or uuid.uuid4(),organization_id=item.organization_id,item_id=item.id,rule_engine_version=f"rule-{uuid.uuid4()}",rule_score=score,recommendation=recommendation,required_hits=["Python"],required_missing=["Kubernetes"],bonus_hits=["LLM"],estimated_years=5,risks=["项目规模待确认"],questions=[])
    if created_at is not None: result.created_at=created_at
    db.add(result); db.commit()
    return candidate,result

def add_llm_evaluation(db,item,result,*,score=72,recommendation="建议评审",created_at=None):
    run=db.get(ScreeningRun,item.run_id)
    prompt=PromptVersion(organization_id=item.organization_id,name=f"screening-{uuid.uuid4()}",version_number=2,content={"system":"private prompt"},content_hash=uuid.uuid4().hex*2,created_by=run.created_by)
    config=db.scalar(select(LlmProviderConfig).where(LlmProviderConfig.organization_id==item.organization_id))
    if config is None: config=LlmProviderConfig(organization_id=item.organization_id,provider_id="approved",model="model",encrypted_api_key=b"private-key",enabled=True,allowed_job_ids=[],version=1,created_by=run.created_by,updated_by=run.created_by)
    db.add_all([prompt,config]); db.flush()
    invocation=LlmInvocation(organization_id=item.organization_id,config_id=config.id,prompt_version_id=prompt.id,screening_result_id=result.id,provider_id=config.provider_id,model=config.model,request_field_manifest=["job_description","resume_text"],status="succeeded",usage={})
    db.add(invocation); db.flush()
    remaining=score; values=[]
    for maximum in (40,25,15,10,10): value=min(maximum,remaining); values.append(value); remaining-=value
    evaluation=LlmScreeningEvaluation(organization_id=item.organization_id,screening_result_id=result.id,invocation_id=invocation.id,prompt_version_id=prompt.id,score=score,recommendation=recommendation,dimensions=[
        {"key":key,"score":value,"evidence":["Python"],"gaps":[]}
        for key,value in zip(("core_capability","experience_depth","role_seniority","transferability","explicit_constraints"),values)
    ],summary="适合进入用人经理评审",strengths=["Python"],gaps=["团队规模"],risks=["到岗时间"],interview_questions=["请介绍 RAG 项目"])
    if created_at is not None: evaluation.created_at=created_at
    db.add(evaluation); db.flush()
    return evaluation

def add_terminal_route_audit(db,item,application,*,route="review",ai_status="succeeded",score=72,safe_error_code=None,created_at=None):
    metadata={"application_id":str(application.id),"item_id":str(item.id),"from_stage":"new","to_stage":route,"ai_status":ai_status,"recommendation":"AI评分不可用" if ai_status=="failed" else "建议评审" if route=="review" else "暂缓"}
    if score is not None: metadata["score"]=score
    if safe_error_code is not None: metadata["safe_error_code"]=safe_error_code
    audit=AuditLog(organization_id=item.organization_id,actor_user_id=application.owner_id,category="recruiting",event_type="screening.terminal_routed",outcome="success",resource_type="application",resource_id=application.id,trace_id="projection-test",metadata_json=metadata)
    if created_at is not None: audit.created_at=created_at
    db.add(audit); db.flush(); return audit

def test_screening_item_projects_latest_persisted_llm_route_and_dimensions(tmp_path):
    app,_,job_id=app_and_seed(tmp_path)
    with TestClient(app) as client:
        headers=login(client,"admin@example.test")
        run=client.post(f"/api/v1/jobs/{job_id}/screening-runs",json={},headers={**headers,"Idempotency-Key":"llm-run"}).json()["data"]
        uploaded=client.post(f"/api/v1/screening-runs/{run['id']}/items",files={"file":("llm.txt",b"Python","text/plain")},headers={**headers,"Idempotency-Key":"llm-item"}).json()["data"]
        with app.state.identity_store.sync_session() as db:
            candidate,result=enrich_item(db,uploaded["id"],"LLM Candidate",score=99,recommendation="优先沟通")
            item=db.get(ScreeningItem,uuid.UUID(uploaded["id"])); aggregate=db.get(ScreeningRun,item.run_id)
            resume=Resume(organization_id=item.organization_id,candidate_id=candidate.id,file_object_id=item.file_object_id,version_number=1,parsed_text="private resume")
            db.add(resume); db.flush()
            application=Application(organization_id=item.organization_id,candidate_id=candidate.id,job_id=aggregate.job_id,resume_id=resume.id,owner_id=aggregate.created_by,stage="contact",source="screening")
            db.add(application); db.flush(); item.resume_id=resume.id; item.application_id=application.id; result.application_id=application.id; result.resume_id=resume.id
            item.llm_status="succeeded"; item.llm_attempts=1
            add_terminal_route_audit(db,item,application)
            add_llm_evaluation(db,item,result,score=65,recommendation="建议评审",created_at=datetime.now(timezone.utc)-timedelta(minutes=1))
            add_llm_evaluation(db,item,result,created_at=datetime.now(timezone.utc))
            db.commit()
        response=client.get(f"/api/v1/screening-runs/{run['id']}/items",headers=headers)

    body=response.json()["data"][0]
    assert body["route_result"]=="review"
    assert body["ai_score"]==72
    assert body["ai_recommendation"]=="建议评审"
    assert body["llm_evaluation"]["recommendation"]=="建议评审"
    assert len(body["llm_evaluation"]["dimensions"])==5
    assert body["rule_result"]["score"]==99

def test_screening_final_ai_failure_has_no_synthetic_evaluation_or_private_error(tmp_path):
    app,_,job_id=app_and_seed(tmp_path)
    with TestClient(app) as client:
        headers=login(client,"admin@example.test")
        run=client.post(f"/api/v1/jobs/{job_id}/screening-runs",json={},headers={**headers,"Idempotency-Key":"failed-run"}).json()["data"]
        uploaded=client.post(f"/api/v1/screening-runs/{run['id']}/items",files={"file":("failed.txt",b"Python","text/plain")},headers={**headers,"Idempotency-Key":"failed-item"}).json()["data"]
        with app.state.identity_store.sync_session() as db:
            candidate,result=enrich_item(db,uploaded["id"],"Failed LLM Candidate")
            item=db.get(ScreeningItem,uuid.UUID(uploaded["id"])); aggregate=db.get(ScreeningRun,item.run_id)
            resume=Resume(organization_id=item.organization_id,candidate_id=candidate.id,file_object_id=item.file_object_id,version_number=1)
            db.add(resume); db.flush()
            application=Application(organization_id=item.organization_id,candidate_id=candidate.id,job_id=aggregate.job_id,resume_id=resume.id,owner_id=aggregate.created_by,stage="contact",source="screening")
            db.add(application); db.flush(); item.resume_id=resume.id; item.application_id=application.id; result.application_id=application.id; result.resume_id=resume.id
            item.llm_status="failed"; item.llm_safe_error_code="provider_unavailable"; item.llm_attempts=3
            add_llm_evaluation(db,item,result,score=65,recommendation="建议评审")
            add_terminal_route_audit(db,item,application,ai_status="failed",score=None,safe_error_code="provider_unavailable")
            db.commit()
        response=client.get(f"/api/v1/screening-runs/{run['id']}/items",headers=headers)

    body=response.json()["data"][0]
    assert body["route_result"]=="review"
    assert body["ai_score"] is None
    assert body["ai_recommendation"]=="AI评分不可用"
    assert body["llm_evaluation"] is None
    assert body["llm_error_code"]=="provider_unavailable"
    assert "private" not in response.text.casefold()

def test_screening_run_summary_counts_terminal_routes_without_rule_recommendations(tmp_path):
    app,_,job_id=app_and_seed(tmp_path)
    with TestClient(app) as client:
        headers=login(client,"admin@example.test")
        run=client.post(f"/api/v1/jobs/{job_id}/screening-runs",json={},headers={**headers,"Idempotency-Key":"summary-run"}).json()["data"]
        uploaded=[client.post(f"/api/v1/screening-runs/{run['id']}/items",files={"file":(f"{name}.txt",b"Python","text/plain")},headers={**headers,"Idempotency-Key":name}).json()["data"] for name in ("review","deferred","file-failed")]
        with app.state.identity_store.sync_session() as db:
            for index,stage in enumerate(("review","deferred")):
                candidate,result=enrich_item(db,uploaded[index]["id"],f"Candidate {stage}",score=100 if stage=="deferred" else 0,recommendation="优先沟通" if stage=="deferred" else "暂缓")
                item=db.get(ScreeningItem,uuid.UUID(uploaded[index]["id"])); aggregate=db.get(ScreeningRun,item.run_id)
                resume=Resume(organization_id=item.organization_id,candidate_id=candidate.id,file_object_id=item.file_object_id,version_number=1)
                db.add(resume); db.flush()
                application=Application(organization_id=item.organization_id,candidate_id=candidate.id,job_id=aggregate.job_id,resume_id=resume.id,owner_id=aggregate.created_by,stage="contact" if stage=="review" else "review",source="screening")
                db.add(application); db.flush(); item.resume_id=resume.id; item.application_id=application.id; result.application_id=application.id; result.resume_id=resume.id
                item.llm_status="failed" if stage=="review" else "succeeded"; item.llm_safe_error_code="provider_unavailable" if stage=="review" else None
                add_terminal_route_audit(db,item,application,route=stage,ai_status="failed" if stage=="review" else "succeeded",score=None if stage=="review" else 50,safe_error_code="provider_unavailable" if stage=="review" else None)
            failed=db.get(ScreeningItem,uuid.UUID(uploaded[2]["id"])); failed.status="failed"; failed.safe_error_code="unsupported_format"
            aggregate=db.get(ScreeningRun,failed.run_id); aggregate.status="completed"; aggregate.processed_count=3; aggregate.succeeded_count=2; aggregate.failed_count=1
            db.commit()
        detail=client.get(f"/api/v1/screening-runs/{run['id']}",headers=headers).json()["data"]
        listing=client.get("/api/v1/screening-runs",headers=headers).json()["data"][0]

    expected={"manager_review_count":1,"deferred_count":1,"ai_unavailable_count":1,"file_failed_count":1}
    assert {key:detail[key] for key in expected}==expected
    assert {key:listing[key] for key in expected}==expected

def test_screening_route_projection_rejects_missing_or_invalid_terminal_audit_metadata(tmp_path):
    app,_,job_id=app_and_seed(tmp_path)
    with TestClient(app) as client:
        headers=login(client,"admin@example.test")
        run=client.post(f"/api/v1/jobs/{job_id}/screening-runs",json={},headers={**headers,"Idempotency-Key":"audit-validation-run"}).json()["data"]
        uploaded=[client.post(f"/api/v1/screening-runs/{run['id']}/items",files={"file":(f"audit-{index}.txt",b"Python","text/plain")},headers={**headers,"Idempotency-Key":f"audit-validation-item-{index}"}).json()["data"] for index in range(3)]
        with app.state.identity_store.sync_session() as db:
            for index,uploaded_item in enumerate(uploaded):
                candidate,result=enrich_item(db,uploaded_item["id"],f"Audit Candidate {index}")
                item=db.get(ScreeningItem,uuid.UUID(uploaded_item["id"])); aggregate=db.get(ScreeningRun,item.run_id)
                resume=Resume(organization_id=item.organization_id,candidate_id=candidate.id,file_object_id=item.file_object_id,version_number=1,parsed_text="Python"); db.add(resume); db.flush()
                application=Application(organization_id=item.organization_id,candidate_id=candidate.id,job_id=aggregate.job_id,resume_id=resume.id,owner_id=aggregate.created_by,stage="review",source="screening"); db.add(application); db.flush()
                item.resume_id=resume.id; item.application_id=application.id; result.application_id=application.id; result.resume_id=resume.id; item.llm_status="failed"; item.llm_safe_error_code="provider_unavailable"
                if index:
                    audit=add_terminal_route_audit(db,item,application,ai_status="failed",score=None,safe_error_code="provider_unavailable")
                    audit.metadata_json={**audit.metadata_json,**({"to_stage":"contact"} if index==1 else {"ai_status":"pending"})}
            db.commit()
        rows=client.get(f"/api/v1/screening-runs/{run['id']}/items",headers=headers).json()["data"]
        detail=client.get(f"/api/v1/screening-runs/{run['id']}",headers=headers).json()["data"]
    assert all(row["route_result"] is None and row["ai_recommendation"] is None and row["llm_error_code"] is None for row in rows)
    assert {key:detail[key] for key in ("manager_review_count","deferred_count","ai_unavailable_count")}=={"manager_review_count":0,"deferred_count":0,"ai_unavailable_count":0}

def test_screening_api_create_upload_read_replay_and_scope(tmp_path):
    app,storage,job_id=app_and_seed(tmp_path)
    with TestClient(app) as client:
        headers=login(client,"admin@example.test"); headers["Idempotency-Key"]="run-1"
        created=client.post(f"/api/v1/jobs/{job_id}/screening-runs",json={},headers=headers); assert created.status_code==201; run=created.json()["data"]
        assert run["status"]=="queued" and run["total_count"]==0 and run["version"]==1
        upload_headers={**headers,"Idempotency-Key":"item-1"}
        uploaded=client.post(f"/api/v1/screening-runs/{run['id']}/items",files={"file":("../ Candidate\n.txt",b"Python 5\xe5\xb9\xb4","text/plain")},headers=upload_headers); assert uploaded.status_code==201; item=uploaded.json()["data"]
        assert item["filename"]=="Candidate.txt" and item["status"]=="queued" and "storage_key" not in item and "sha256" not in item
        assert item["candidate_id"] is None and item["candidate_name"] is None and item["rule_result"] is None
        replay=client.post(f"/api/v1/screening-runs/{run['id']}/items",files={"file":("../ Candidate\n.txt",b"Python 5\xe5\xb9\xb4","text/plain")},headers=upload_headers); assert replay.status_code==201 and replay.json()==uploaded.json() and storage.writes==1
        conflict=client.post(f"/api/v1/screening-runs/{run['id']}/items",files={"file":("other.txt",b"Different","text/plain")},headers=upload_headers); assert conflict.status_code==409 and conflict.json()["code"]=="idempotency_conflict"
        assert storage.writes==1
        second=client.post(f"/api/v1/screening-runs/{run['id']}/items",files={"file":(("x"*250)+".txt",b"second","text/plain")},headers={**headers,"Idempotency-Key":"item-2"}); assert second.status_code==201 and len(second.json()["data"]["filename"])<=200
        detail=client.get(f"/api/v1/screening-runs/{run['id']}",headers=headers); assert detail.status_code==200 and detail.json()["data"]["total_count"]==2
        listing=client.get(f"/api/v1/screening-runs/{run['id']}/items?status=queued&limit=1",headers=headers); assert listing.status_code==200 and listing.json()["meta"]["next_cursor"]
        page2=client.get(f"/api/v1/screening-runs/{run['id']}/items?status=queued&limit=1&cursor={listing.json()['meta']['next_cursor']}",headers=headers); assert page2.status_code==200 and page2.json()["data"][0]["id"]!=listing.json()["data"][0]["id"]
        client.post("/api/v1/auth/logout",headers=headers); denied=login(client,"system@example.test"); denied["Idempotency-Key"]="denied"; assert client.post(f"/api/v1/jobs/{job_id}/screening-runs",json={},headers=denied).status_code==404
        client.post("/api/v1/auth/logout",headers=denied); manager=login(client,"manager@example.test"); assert client.get(f"/api/v1/screening-runs/{run['id']}",headers=manager).status_code==200; manager["Idempotency-Key"]="read-only"; unauthorized=client.post(f"/api/v1/screening-runs/{run['id']}/items",files={"file":("x.pdf",b"not-a-pdf","application/pdf")},headers=manager); assert unauthorized.status_code==404 and storage.writes==2

def test_screening_upload_rejects_empty_and_magic_mismatch_without_storage(tmp_path):
    app,storage,job_id=app_and_seed(tmp_path)
    with TestClient(app) as client:
        headers=login(client,"admin@example.test"); headers["Idempotency-Key"]="run"; run=client.post(f"/api/v1/jobs/{job_id}/screening-runs",json={},headers=headers).json()["data"]
        for key,name,data,mime,code in (("empty","x.txt",b"","text/plain","empty_file"),("magic","x.pdf",b"not pdf","application/pdf","file_magic_mismatch")):
            response=client.post(f"/api/v1/screening-runs/{run['id']}/items",files={"file":(name,data,mime)},headers={**headers,"Idempotency-Key":key}); assert response.status_code==422 and response.json()["code"]==code
        assert storage.writes==0

def test_screening_upload_exact_size_boundary_and_storage_failure(tmp_path):
    app,storage,job_id=app_and_seed(tmp_path); app.state.settings.parser_max_source_bytes=1024
    with TestClient(app) as client:
        headers=login(client,"admin@example.test"); headers["Idempotency-Key"]="run"; run=client.post(f"/api/v1/jobs/{job_id}/screening-runs",json={},headers=headers).json()["data"]
        exact=client.post(f"/api/v1/screening-runs/{run['id']}/items",files={"file":("x.txt",b"x"*1024,"text/plain")},headers={**headers,"Idempotency-Key":"exact"}); assert exact.status_code==201
        over=client.post(f"/api/v1/screening-runs/{run['id']}/items",files={"file":("x.txt",b"x"*1025,"text/plain")},headers={**headers,"Idempotency-Key":"over"}); assert over.status_code==422 and over.json()["code"]=="file_too_large"
        storage.fail=True; failed=client.post(f"/api/v1/screening-runs/{run['id']}/items",files={"file":("y.txt",b"safe","text/plain")},headers={**headers,"Idempotency-Key":"storage"}); assert failed.status_code==503 and failed.json()["code"]=="storage_write_failed" and storage.deletes==1

def test_database_failure_deletes_new_quarantine_object(tmp_path,monkeypatch):
    app,storage,job_id=app_and_seed(tmp_path)
    with TestClient(app) as client:
        headers=login(client,"admin@example.test"); run=client.post(f"/api/v1/jobs/{job_id}/screening-runs",json={},headers={**headers,"Idempotency-Key":"run"}).json()["data"]
        monkeypatch.setattr("server.app.screening.api.ScreeningItem",lambda **_: (_ for _ in ()).throw(RuntimeError("db failed with resume text")))
        response=client.post(f"/api/v1/screening-runs/{run['id']}/items",files={"file":("x.txt",b"private resume","text/plain")},headers={**headers,"Idempotency-Key":"db-fail"})
        assert response.status_code==503 and response.json()["code"]=="persistence_failed" and storage.writes==1 and storage.deletes==1 and storage.objects=={}

def test_run_create_schema_rejects_bad_uuid_and_source(tmp_path):
    app,_,job_id=app_and_seed(tmp_path)
    with TestClient(app) as client:
        headers=login(client,"admin@example.test"); headers["Idempotency-Key"]="bad"
        for body in ({"jd_version_id":"not-a-uuid"},{"source":"worker"}):
            assert client.post(f"/api/v1/jobs/{job_id}/screening-runs",json=body,headers=headers).status_code==422

def test_screening_item_cursor_is_created_at_id_keyset_with_between_page_insert(tmp_path):
    app,_,job_id=app_and_seed(tmp_path)
    with TestClient(app) as client:
        headers=login(client,"admin@example.test"); run=client.post(f"/api/v1/jobs/{job_id}/screening-runs",json={},headers={**headers,"Idempotency-Key":"run"}).json()["data"]
        ids=[]
        for key in ("first","second"):
            ids.append(client.post(f"/api/v1/screening-runs/{run['id']}/items",files={"file":(f"{key}.txt",key.encode(),"text/plain")},headers={**headers,"Idempotency-Key":key}).json()["data"]["id"])
        with app.state.identity_store.sync_session() as db:
            first=db.get(ScreeningItem,uuid.UUID(ids[0])); second=db.get(ScreeningItem,uuid.UUID(ids[1])); first.id=uuid.UUID(int=2**128-2); second.id=uuid.UUID(int=1); first.created_at=datetime.now(timezone.utc)-timedelta(seconds=2); second.created_at=first.created_at+timedelta(seconds=1); db.commit(); ids=[str(first.id),str(second.id)]
        page1=client.get(f"/api/v1/screening-runs/{run['id']}/items?limit=1",headers=headers); assert page1.json()["data"][0]["id"]==ids[0]
        inserted=client.post(f"/api/v1/screening-runs/{run['id']}/items",files={"file":("third.txt",b"third","text/plain")},headers={**headers,"Idempotency-Key":"third"}).json()["data"]["id"]
        cursor=page1.json()["meta"]["next_cursor"]; page2=client.get(f"/api/v1/screening-runs/{run['id']}/items?limit=10&cursor={cursor}",headers=headers)
        assert [row["id"] for row in page2.json()["data"]]==[ids[1],inserted]

def test_screening_items_return_exact_candidate_and_newest_rule_result(tmp_path):
    app,_,job_id=app_and_seed(tmp_path)
    with TestClient(app) as client:
        headers=login(client,"admin@example.test"); run=client.post(f"/api/v1/jobs/{job_id}/screening-runs",json={},headers={**headers,"Idempotency-Key":"run"}).json()["data"]
        item=client.post(f"/api/v1/screening-runs/{run['id']}/items",files={"file":("alice.txt",b"Python","text/plain")},headers={**headers,"Idempotency-Key":"item"}).json()["data"]
        with app.state.identity_store.sync_session() as db:
            created_at=datetime.now(timezone.utc); candidate,old=enrich_item(db,item["id"],"Alice Zhang",score=40,recommendation="暂缓",created_at=created_at,result_id=uuid.UUID(int=1))
            newest=ScreeningResult(id=uuid.UUID(int=2),organization_id=old.organization_id,item_id=old.item_id,rule_engine_version="rule-newest",rule_score=81,recommendation="可沟通",required_hits=["Python"],required_missing=["Kubernetes"],bonus_hits=["LLM"],estimated_years=5,risks=["项目规模待确认"],questions=[],created_at=created_at); db.add(newest); db.commit(); candidate_id=str(candidate.id)
        response=client.get(f"/api/v1/screening-runs/{run['id']}/items",headers=headers); assert response.status_code==200
    assert response.json()["data"][0]["candidate_id"]==candidate_id
    assert response.json()["data"][0]["candidate_name"]=="Alice Zhang"
    assert response.json()["data"][0]["rule_result"]=={"score":81,"recommendation":"可沟通","required_hits":["Python"],"required_missing":["Kubernetes"],"bonus_hits":["LLM"],"risks":["项目规模待确认"]}

def test_failed_screening_item_hides_legacy_rule_result(tmp_path):
    app,_,job_id=app_and_seed(tmp_path)
    with TestClient(app) as client:
        headers=login(client,"admin@example.test"); run=client.post(f"/api/v1/jobs/{job_id}/screening-runs",json={},headers={**headers,"Idempotency-Key":"run"}).json()["data"]
        item=client.post(f"/api/v1/screening-runs/{run['id']}/items",files={"file":("failed.txt",b"Python","text/plain")},headers={**headers,"Idempotency-Key":"item"}).json()["data"]
        with app.state.identity_store.sync_session() as db:
            enrich_item(db,item["id"],"Failed Candidate")
            stored=db.get(ScreeningItem,uuid.UUID(item["id"])); stored.status="failed"; stored.safe_error_code="scoring_failed"; db.commit()
        response=client.get(f"/api/v1/screening-runs/{run['id']}/items",headers=headers)
    assert response.status_code==200 and response.json()["data"][0]["rule_result"] is None

def test_screening_item_enrichment_stays_paired_across_cursor_pages(tmp_path):
    app,_,job_id=app_and_seed(tmp_path)
    with TestClient(app) as client:
        headers=login(client,"admin@example.test"); run=client.post(f"/api/v1/jobs/{job_id}/screening-runs",json={},headers={**headers,"Idempotency-Key":"run"}).json()["data"]
        items=[client.post(f"/api/v1/screening-runs/{run['id']}/items",files={"file":(f"{name}.txt",name.encode(),"text/plain")},headers={**headers,"Idempotency-Key":name}).json()["data"] for name in ("first","second")]
        with app.state.identity_store.sync_session() as db:
            enrich_item(db,items[0]["id"],"First Candidate",score=81)
            enrich_item(db,items[1]["id"],"Second Candidate",score=62,recommendation="需人工复核")
        first=client.get(f"/api/v1/screening-runs/{run['id']}/items?limit=1",headers=headers).json()
        second=client.get(f"/api/v1/screening-runs/{run['id']}/items?limit=1&cursor={first['meta']['next_cursor']}",headers=headers).json()
    assert (first["data"][0]["candidate_name"],first["data"][0]["rule_result"]["score"])==("First Candidate",81)
    assert (second["data"][0]["candidate_name"],second["data"][0]["rule_result"]["score"])==("Second Candidate",62)

def test_screening_item_enrichment_is_job_scoped_and_data_minimized(tmp_path):
    app,_,job_id=app_and_seed(tmp_path)
    with TestClient(app) as client:
        headers=login(client,"admin@example.test"); run=client.post(f"/api/v1/jobs/{job_id}/screening-runs",json={},headers={**headers,"Idempotency-Key":"run"}).json()["data"]
        item=client.post(f"/api/v1/screening-runs/{run['id']}/items",files={"file":("secret.txt",b"raw resume secret","text/plain")},headers={**headers,"Idempotency-Key":"item"}).json()["data"]
        with app.state.identity_store.sync_session() as db:
            candidate,result=enrich_item(db,item["id"],"Scoped Candidate")
            stored=db.get(ScreeningItem,uuid.UUID(item["id"])); resume=Resume(organization_id=stored.organization_id,candidate_id=candidate.id,file_object_id=stored.file_object_id,version_number=1,parsed_text="raw resume secret"); db.add(resume); db.flush(); stored.resume_id=resume.id
            db.add(CandidateContact(organization_id=stored.organization_id,candidate_id=candidate.id,kind="email",ciphertext=b"contact-ciphertext-secret",lookup_hash="c"*64,masked_value="private@example.test"))
            file_object=db.get(FileObject,stored.file_object_id); storage_key=file_object.storage_key; file_object.quarantine_cleanup_key="cleanup/private-storage-key"
            aggregate=db.get(ScreeningRun,stored.run_id)
            prompt=PromptVersion(organization_id=stored.organization_id,name="screening",version_number=1,content={"system":"private prompt body","metadata":{"owner":"private prompt metadata"}},content_hash="p"*64,created_by=aggregate.created_by)
            config=LlmProviderConfig(organization_id=stored.organization_id,provider_id="private-provider-id",model="private-provider-model",encrypted_api_key=b"private-encrypted-api-key",enabled=True,allowed_job_ids=[],version=1,created_by=aggregate.created_by,updated_by=aggregate.created_by)
            db.add_all([prompt,config]); db.flush()
            invocation=LlmInvocation(organization_id=stored.organization_id,config_id=config.id,prompt_version_id=prompt.id,screening_result_id=result.id,provider_id=config.provider_id,model=config.model,request_field_manifest=["private-provider-request"],status="succeeded",usage={"provider_metadata":{"request_id":"private-provider-request-id"},"provider_error_body":{"message":"private-provider-error-body"}},trace_id="private-provider-trace")
            db.add(invocation); db.flush()
            db.add(LlmScreeningEvaluation(organization_id=stored.organization_id,screening_result_id=result.id,invocation_id=invocation.id,prompt_version_id=prompt.id,score=82,recommendation="可沟通",summary="Safe summary",strengths=["Safe strength"],gaps=[],risks=[],interview_questions=[]))
            stored.llm_status="succeeded"; stored.llm_attempts=1; db.commit()
        allowed=client.get(f"/api/v1/screening-runs/{run['id']}/items",headers=headers); assert allowed.status_code==200
        client.post("/api/v1/auth/logout",headers=headers); denied_headers=login(client,"system@example.test"); denied=client.get(f"/api/v1/screening-runs/{run['id']}/items",headers=denied_headers)
    response_keys,response_values=json_keys_and_scalar_values(allowed.json())
    forbidden={"contacts","candidate_contacts","email","phone","parsed_text","resume_text","raw_resume_text","storage_key","quarantine_cleanup_key","prompt","prompt_id","prompt_version_id","prompt_metadata","request_field_manifest","input_sha256","provider","provider_id","provider_metadata","provider_request","provider_response","api_key","encrypted_api_key","provider_error","provider_error_body","error_body","unsanitized_error"}
    assert {"rule_result","required_hits","llm_evaluation","summary"}.issubset(response_keys) and "Python" in response_values and "Safe summary" in response_values
    assert forbidden.isdisjoint(response_keys)
    forbidden_values=("raw resume secret","private@example.test","contact-ciphertext-secret","c"*64,storage_key,"quarantine/","cleanup/private-storage-key","private prompt body","private prompt metadata","p"*64,"private-provider-id","private-provider-model","private-encrypted-api-key","private-provider-request","private-provider-request-id","private-provider-error-body","private-provider-trace")
    assert all(secret not in str(response_values) for secret in forbidden_values)
    assert denied.status_code==404 and "Scoped Candidate" not in denied.text and "rule_result" not in denied.text

def test_screening_items_cannot_cross_organization_boundary(tmp_path):
    app,_,job_id=app_and_seed(tmp_path)
    with TestClient(app) as client:
        first_headers=login(client,"admin@example.test"); run=client.post(f"/api/v1/jobs/{job_id}/screening-runs",json={},headers={**first_headers,"Idempotency-Key":"run"}).json()["data"]
        item=client.post(f"/api/v1/screening-runs/{run['id']}/items",files={"file":("tenant-secret.txt",b"Python","text/plain")},headers={**first_headers,"Idempotency-Key":"item"}).json()["data"]
        with app.state.identity_store.sync_session() as db:
            candidate,result=enrich_item(db,item["id"],"First Tenant Secret Candidate")
            result.required_hits=["ORG_A_REQUIRED_SECRET"]; result.required_missing=["ORG_A_MISSING_SECRET"]; result.bonus_hits=["ORG_A_BONUS_SECRET"]; result.risks=["ORG_A_RISK_SECRET"]
            second_org=Organization(slug="other",name="Other",status="active"); second_admin=User(organization=second_org,email="admin@other.test",normalized_email="admin@other.test",display_name="Other Admin",password_hash=PasswordService().hash("correct")); second_admin.roles.append(UserRole(role="recruiting_admin")); db.add(second_admin); db.commit(); candidate_id=str(candidate.id); rule_facts=[*result.required_hits,*result.required_missing,*result.bonus_hits,*result.risks]
        client.post("/api/v1/auth/logout",headers=first_headers); second_headers=login(client,"admin@other.test","other")
        denied=client.get(f"/api/v1/screening-runs/{run['id']}/items",headers=second_headers)
    assert denied.status_code==404
    run_facts=(str(job_id),run["jd_version_id"],run["rule_version_id"],run["created_at"])
    assert all(secret not in denied.text for secret in (*run_facts,candidate_id,"First Tenant Secret Candidate",*rule_facts))

def test_start_run_is_authorized_idempotent_and_enqueues_each_item_atomically(tmp_path):
    app,_,job_id=app_and_seed(tmp_path)
    with TestClient(app) as client:
        headers=login(client,"admin@example.test"); run=client.post(f"/api/v1/jobs/{job_id}/screening-runs",json={},headers={**headers,"Idempotency-Key":"run"}).json()["data"]
        empty=client.post(f"/api/v1/screening-runs/{run['id']}/start",headers={**headers,"Idempotency-Key":"empty"}); assert empty.status_code==409 and empty.json()["code"]=="screening_run_empty"
        for key in ("one","two"): assert client.post(f"/api/v1/screening-runs/{run['id']}/items",files={"file":(f"{key}.txt",key.encode(),"text/plain")},headers={**headers,"Idempotency-Key":key}).status_code==201
        started=client.post(f"/api/v1/screening-runs/{run['id']}/start",headers={**headers,"Idempotency-Key":"start"}); assert started.status_code==200 and started.json()["data"]["status"]=="parsing"
        replay=client.post(f"/api/v1/screening-runs/{run['id']}/start",headers={**headers,"Idempotency-Key":"start"}); assert replay.status_code==200 and replay.json()==started.json()
        conflict=client.post(f"/api/v1/screening-runs/{run['id']}/start",headers={**headers,"Idempotency-Key":"different"}); assert conflict.status_code==409 and conflict.json()["code"]=="screening_run_already_started"
        with app.state.identity_store.sync_session() as db:
            jobs=list(db.scalars(select(BackgroundJob).where(BackgroundJob.type=="screening.parse_item"))); assert len(jobs)==2 and len({job.dedupe_key for job in jobs})==2
            assert all(set(job.payload)=={"organization_id","screening_item_id","parser_version"} for job in jobs)
        client.post("/api/v1/auth/logout",headers=headers); manager=login(client,"manager@example.test")
        assert client.post(f"/api/v1/screening-runs/{run['id']}/start",headers={**manager,"Idempotency-Key":"manager"}).status_code==404

def test_empty_queued_run_can_be_cancelled_but_started_run_cannot(tmp_path):
    app,_,job_id=app_and_seed(tmp_path)
    with TestClient(app) as client:
        headers=login(client,"admin@example.test")
        empty=client.post(f"/api/v1/jobs/{job_id}/screening-runs",json={},headers={**headers,"Idempotency-Key":"empty-run"}).json()["data"]
        cancelled=client.post(f"/api/v1/screening-runs/{empty['id']}/cancel",headers={**headers,"Idempotency-Key":"cancel-empty"})
        assert cancelled.status_code==200
        assert cancelled.json()["data"]["status"]=="cancelled"
        replay=client.post(f"/api/v1/screening-runs/{empty['id']}/cancel",headers={**headers,"Idempotency-Key":"cancel-empty"})
        assert replay.status_code==200 and replay.json()==cancelled.json()

        nonempty=client.post(f"/api/v1/jobs/{job_id}/screening-runs",json={},headers={**headers,"Idempotency-Key":"nonempty-run"}).json()["data"]
        assert client.post(f"/api/v1/screening-runs/{nonempty['id']}/items",files={"file":("resume.txt",b"Python","text/plain")},headers={**headers,"Idempotency-Key":"resume"}).status_code==201
        rejected=client.post(f"/api/v1/screening-runs/{nonempty['id']}/cancel",headers={**headers,"Idempotency-Key":"cancel-nonempty"})
        assert rejected.status_code==409 and rejected.json()["code"]=="screening_run_not_cancellable"

    with app.state.identity_store.sync_session() as db:
        stored=db.get(ScreeningRun,uuid.UUID(empty["id"]))
        audit=db.scalar(select(AuditLog).where(AuditLog.event_type=="screening.run_cancelled"))
        assert stored.status=="cancelled" and stored.finished_at is not None and stored.version==2
        assert audit.metadata_json=={"run_id":empty["id"],"reason":"empty_upload_interrupted"}
