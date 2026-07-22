import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from tempfile import SpooledTemporaryFile
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, func, select

from server.app.core.settings import Settings
from server.app.identity.models import Department, Organization, User, UserRole, UserStatus, Job, JobCollaborator
from server.app.identity.policy import Principal
from server.app.identity.security import PasswordService
from server.app.main import create_app
from server.app.llm.models import LlmInvocation, LlmProviderConfig, LlmScreeningEvaluation, PromptVersion
from server.app.recruiting import api as recruiting_api
from server.app.recruiting.models import Application, ApplicationStageEvent, Candidate, CandidateEvent, CandidateNote, DownloadTicket, FileObject, IdempotencyRecord, JobJdVersion, Resume, ScreeningRuleVersion
from server.app.screening.models import ScreeningItem, ScreeningResult, ScreeningRun
from server.app.identity.models import AuditLog
from server.app.recruiting.storage import StorageReadFailed


class Probe:
    async def check(self) -> None:
        pass


@dataclass
class FakeStorage:
    preview: str = "private parsed preview"
    last_spool: object | None = None

    def open_download(self, storage_key: str, max_bytes: int):
        assert storage_key == "private/resume"
        assert max_bytes == 10 * 1024 * 1024
        spool = SpooledTemporaryFile(max_size=1024, mode="w+b")
        spool.write(b"private-file"); spool.seek(0)
        self.last_spool = spool
        return spool


def make_app(tmp_path, storage=None):
    app = create_app(
        settings=Settings(
            environment="test",
            database_url=f"sqlite+aiosqlite:///{tmp_path / 'recruiting-api.db'}",
            cors_origins=["https://hr.example.test"],
        ),
        database_probe=Probe(),
        storage_probe=Probe(),
        initialize_identity_schema=True,
        resume_storage=storage or FakeStorage(),
    )
    app.state.identity_store.create_schema()
    return app


class FailingStorage:
    def __init__(self, failure: str): self.failure = failure
    def open_download(self, storage_key: str, max_bytes: int):
        raise StorageReadFailed(self.failure)


def seed_user(app, role: str, email: str):
    with app.state.identity_store.sync_session() as db:
        organization = db.query(Organization).filter_by(slug="acme").one_or_none()
        if organization is None:
            organization = Organization(slug="acme", name="Acme", status="active")
        user = User(
            organization=organization,
            email=email,
            normalized_email=email,
            display_name=role,
            password_hash=PasswordService().hash("correct horse"),
        )
        user.roles.append(UserRole(role=role))
        db.add(user)
        db.commit()
        return user.id


def login(client, email: str):
    response = client.post(
        "/api/v1/auth/login",
        json={"organization_slug": "acme", "email": email, "password": "correct horse"},
        headers={"Origin": "https://hr.example.test"},
    )
    assert response.status_code == 200
    return {"Origin": "https://hr.example.test", "X-CSRF-Token": response.headers["X-CSRF-Token"]}


def job_definition_payload(**changes):
    payload = {
        "title": "Platform Engineer",
        "department_id": None,
        "headcount": 2,
        "priority": "high",
        "hiring_owner_id": None,
        "description": "Build reliable hiring infrastructure.",
        "location": "Shanghai",
        "process_template": "standard",
        "llm_enabled": True,
        "must_have": ["Python", "SQL"],
        "nice_to_have": ["FastAPI"],
        "publish": False,
    }
    payload.update(changes)
    return payload


def seed_screening_results(db, application, file_id, actor_id, results):
    version_number = db.query(JobJdVersion).filter_by(job_id=application.job_id).count() + 1
    jd = JobJdVersion(organization_id=application.organization_id, job_id=application.job_id, version_number=version_number, content={"text": "JD"}, created_by=actor_id)
    rule = ScreeningRuleVersion(organization_id=application.organization_id, job_id=application.job_id, version_number=version_number, content={}, created_by=actor_id)
    db.add_all([jd, rule]); db.flush()
    run = ScreeningRun(organization_id=application.organization_id, job_id=application.job_id, jd_version_id=jd.id, rule_version_id=rule.id, source="upload", status="completed", total_count=1, processed_count=1, succeeded_count=1, failed_count=0, created_by=actor_id)
    db.add(run); db.flush()
    item = ScreeningItem(organization_id=application.organization_id, run_id=run.id, file_object_id=file_id, candidate_id=application.candidate_id, resume_id=application.resume_id, application_id=application.id, status="scored", attempts=1)
    db.add(item); db.flush()
    stored_results=[]
    for engine, score, recommendation, created_at in results:
        result=ScreeningResult(
            organization_id=application.organization_id,
            item_id=item.id,
            application_id=application.id,
            resume_id=application.resume_id,
            rule_engine_version=engine,
            rule_score=score,
            recommendation=recommendation,
            required_hits=[],
            required_missing=[],
            bonus_hits=[],
            estimated_years=0,
            risks=[],
            questions=[],
            created_at=created_at,
        )
        db.add(result); stored_results.append(result)
    db.flush()
    return item,stored_results


def seed_llm_evaluation(db, application, actor_id, result, score, recommendation, created_at):
    item=db.get(ScreeningItem,result.item_id); item.llm_status="succeeded"; item.llm_attempts=1
    prompt=PromptVersion(organization_id=application.organization_id,name=f"screening-{result.id}",version_number=2,content={"system":"private prompt"},content_hash=hashlib.sha256(str(result.id).encode()).hexdigest(),created_by=actor_id)
    config=db.scalar(select(LlmProviderConfig).where(LlmProviderConfig.organization_id==application.organization_id))
    if config is None:
        config=LlmProviderConfig(organization_id=application.organization_id,provider_id="approved",model="model",encrypted_api_key=b"private-key",enabled=True,allowed_job_ids=[],version=1,created_by=actor_id,updated_by=actor_id)
    db.add_all([prompt,config]); db.flush()
    invocation=LlmInvocation(organization_id=application.organization_id,config_id=config.id,prompt_version_id=prompt.id,screening_result_id=result.id,provider_id=config.provider_id,model=config.model,request_field_manifest=["job_description","resume_text"],status="succeeded",usage={})
    db.add(invocation); db.flush()
    remaining=score; values=[]
    for maximum in (40,25,15,10,10):
        value=min(maximum,remaining); values.append(value); remaining-=value
    evaluation=LlmScreeningEvaluation(organization_id=application.organization_id,screening_result_id=result.id,invocation_id=invocation.id,prompt_version_id=prompt.id,score=score,recommendation=recommendation,dimensions=[
        {"key":key,"score":value,"evidence":[f"evidence-{key}"],"gaps":[]}
        for key,value in zip(("core_capability","experience_depth","role_seniority","transferability","explicit_constraints"),values)
    ],summary="persisted summary",strengths=["persisted strength"],gaps=["persisted gap"],risks=["persisted risk"],interview_questions=["persisted question"],created_at=created_at)
    db.add(evaluation); db.flush(); return evaluation

def seed_terminal_route_audit(db,application,item,actor_id,*,route="review",ai_status="succeeded",score=72,safe_error_code=None,created_at=None):
    metadata={"application_id":str(application.id),"item_id":str(item.id),"from_stage":"new","to_stage":route,"ai_status":ai_status,"recommendation":"AI评分不可用" if ai_status=="failed" else "建议评审" if route=="review" else "暂缓"}
    if score is not None: metadata["score"]=score
    if safe_error_code is not None: metadata["safe_error_code"]=safe_error_code
    audit=AuditLog(organization_id=application.organization_id,actor_user_id=actor_id,category="recruiting",event_type="screening.terminal_routed",outcome="success",resource_type="application",resource_id=application.id,trace_id="projection-test",metadata_json=metadata)
    if created_at is not None: audit.created_at=created_at
    db.add(audit); db.flush(); return audit


def seed_same_candidate_cross_job(app, recruiter_id):
    with app.state.identity_store.sync_session() as db:
        recruiter = db.get(User, recruiter_id)
        job_allowed = Job(organization_id=recruiter.organization_id, title="Allowed job", owner_id=recruiter_id)
        job_denied = Job(organization_id=recruiter.organization_id, title="Denied job", owner_id=recruiter_id)
        candidate = Candidate(organization_id=recruiter.organization_id, display_name="Shared candidate", owner_id=recruiter_id)
        allowed_file = FileObject(organization_id=recruiter.organization_id, storage_key="private/resume", original_filename="allowed.pdf", mime_type="application/pdf", size_bytes=12, sha256="1" * 64, uploaded_by=recruiter_id)
        denied_file = FileObject(organization_id=recruiter.organization_id, storage_key="private/denied", original_filename="denied.pdf", mime_type="application/pdf", size_bytes=12, sha256="2" * 64, uploaded_by=recruiter_id)
        db.add_all([job_allowed, job_denied, candidate, allowed_file, denied_file]); db.flush()
        allowed_resume = Resume(
            organization_id=recruiter.organization_id,
            candidate_id=candidate.id,
            file_object_id=allowed_file.id,
            version_number=1,
            parsed_text="个人简介\n企业级 AI 平台负责人\n专业技能\nPython、RAG\n工作经历\n负责 Agent 平台交付\n教育经历\n浙江大学 计算机本科",
        )
        denied_resume = Resume(organization_id=recruiter.organization_id, candidate_id=candidate.id, file_object_id=denied_file.id, version_number=2, parsed_text="denied preview")
        db.add_all([allowed_resume, denied_resume]); db.flush()
        allowed_application = Application(organization_id=recruiter.organization_id, candidate_id=candidate.id, job_id=job_allowed.id, resume_id=allowed_resume.id, owner_id=recruiter_id)
        denied_application = Application(organization_id=recruiter.organization_id, candidate_id=candidate.id, job_id=job_denied.id, resume_id=denied_resume.id, owner_id=recruiter_id)
        db.add_all([allowed_application, denied_application]); db.flush()
        denied_note = CandidateNote(organization_id=recruiter.organization_id, candidate_id=candidate.id, actor_user_id=recruiter_id, event_type="candidate.note", payload={"application_id": str(denied_application.id), "body": "denied job note"})
        denied_event = CandidateEvent(organization_id=recruiter.organization_id, candidate_id=candidate.id, actor_user_id=recruiter_id, event_type="candidate.note_added", payload={"application_id": str(denied_application.id)})
        collaborator = JobCollaborator(organization_id=recruiter.organization_id, job_id=job_allowed.id, user_id=recruiter_id, access_role="job_recruiter")
        db.add_all([denied_note, denied_event, collaborator]); db.commit()
        return {
            "candidate_id": str(candidate.id),
            "allowed_job_id": job_allowed.id,
            "denied_job_id": job_denied.id,
            "allowed_application_id": str(allowed_application.id),
            "denied_application_id": str(denied_application.id),
            "allowed_resume_id": str(allowed_resume.id),
            "denied_resume_id": str(denied_resume.id),
        }


def test_recruiting_openapi_registers_complete_task_3b_contract_without_secret_fields(tmp_path) -> None:
    app = create_app(
        settings=Settings(
            environment="test",
            database_url=f"sqlite+aiosqlite:///{tmp_path / 'api.db'}",
            cors_origins=["https://hr.example.test"],
        ),
        database_probe=Probe(),
        storage_probe=Probe(),
        initialize_identity_schema=True,
    )
    with TestClient(app) as client:
        schema = client.get("/openapi.json").json()

    expected = {
        "/api/v1/job-definitions": {"post"},
        "/api/v1/job-definitions/{job_id}": {"get", "put"},
        "/api/v1/jobs": {"get", "post"},
        "/api/v1/jobs/{job_id}": {"get", "patch"},
        "/api/v1/jobs/{job_id}/transitions": {"post"},
        "/api/v1/jobs/{job_id}/jd-versions": {"get", "post"},
        "/api/v1/jobs/{job_id}/rule-versions": {"get", "post"},
        "/api/v1/jobs/{job_id}/funnel": {"get"},
        "/api/v1/candidates": {"get", "post"},
        "/api/v1/candidates/{candidate_id}": {"get", "patch"},
        "/api/v1/candidates/{candidate_id}/timeline": {"get"},
        "/api/v1/candidates/{candidate_id}/notes": {"get", "post"},
        "/api/v1/candidates/{candidate_id}/resumes": {"get"},
        "/api/v1/resumes/{resume_id}/preview": {"get"},
        "/api/v1/resumes/{resume_id}/file": {"get"},
        "/api/v1/resumes/{resume_id}/download-tickets": {"post"},
        "/api/v1/download-tickets/consume": {"post"},
        "/api/v1/candidates/{candidate_id}/applications": {"get"},
        "/api/v1/jobs/{job_id}/applications": {"post"},
        "/api/v1/applications/{application_id}": {"patch"},
        "/api/v1/applications/{application_id}/workflow-actions": {"post"},
    }
    assert {path: set(schema["paths"].get(path, {})) for path in expected} == expected
    for path, methods in expected.items():
        for method in methods:
            if path in {"/api/v1/resumes/{resume_id}/file", "/api/v1/download-tickets/consume"}:
                assert schema["paths"][path][method]["responses"]["200"]["content"]["application/octet-stream"]["schema"]
                continue
            responses = schema["paths"][path][method]["responses"]
            success = responses.get("200") or responses.get("201")
            assert success["content"]["application/json"]["schema"]
    rendered = str(schema).casefold()
    for secret in ("ciphertext", "lookup_hash", "storage_key", "token_hash", "parsed_text"):
        assert secret not in rendered


def test_recruiting_routes_require_an_opaque_session(tmp_path) -> None:
    app = create_app(
        settings=Settings(
            environment="test",
            database_url=f"sqlite+aiosqlite:///{tmp_path / 'auth.db'}",
            cors_origins=["https://hr.example.test"],
        ),
        database_probe=Probe(),
        storage_probe=Probe(),
        initialize_identity_schema=True,
    )
    with TestClient(app) as client:
        response = client.get("/api/v1/jobs")
    assert response.status_code == 401
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["code"] == "authentication_required"


def test_recruiting_validation_is_stable_problem_json_without_echoing_values(tmp_path) -> None:
    app = make_app(tmp_path)
    seed_user(app, "recruiting_admin", "admin@example.test")
    with TestClient(app) as client:
        headers = login(client, "admin@example.test")
        cases = [
            client.get("/api/v1/jobs/not-a-uuid"),
            client.get("/api/v1/jobs?limit=101"),
            client.get("/api/v1/candidates?cursor=raw-secret-value"),
            client.post("/api/v1/candidates", json={"display_name": ""}, headers=headers),
            client.post("/api/v1/download-tickets/consume", json={"token": "raw-secret-value"}, headers=headers),
        ]
    for response in cases:
        assert response.status_code == 422
        assert response.headers["content-type"].startswith("application/problem+json")
        assert response.json()["code"] == "validation_failed"
        assert "raw-secret-value" not in response.text


def test_admin_happy_path_preconditions_idempotency_and_private_download(tmp_path) -> None:
    app = make_app(tmp_path)
    admin_id = seed_user(app, "recruiting_admin", "admin@example.test")
    recruiter_id = seed_user(app, "recruiter", "recruiter@example.test")
    with TestClient(app) as client:
        headers = login(client, "admin@example.test")
        job = client.post("/api/v1/jobs", json={"title": "Engineer"}, headers=headers)
        assert job.status_code == 201 and job.headers["etag"] == '"1"'
        job_id = job.json()["data"]["id"]

        missing = client.patch(f"/api/v1/jobs/{job_id}", json={"title": "Senior Engineer"}, headers=headers)
        assert missing.status_code == 428 and missing.json()["code"] == "precondition_required"
        changed = client.patch(f"/api/v1/jobs/{job_id}", json={"title": "Senior Engineer"}, headers={**headers, "If-Match": '"1"'})
        assert changed.status_code == 200 and changed.headers["etag"] == '"2"'

        candidate = client.post(
            "/api/v1/candidates",
            json={"display_name": "Candidate", "contacts": [{"kind": " Email ", "value": "Person@Example.COM"}]},
            headers=headers,
        )
        assert candidate.status_code == 201
        assert candidate.json()["data"]["contacts"] == [{"kind": "email", "value": "p***@example.com"}]
        assert "Person@Example.COM" not in candidate.text
        candidate_id = candidate.json()["data"]["id"]

        with app.state.identity_store.sync_session() as db:
            file = FileObject(organization_id=db.get(User, admin_id).organization_id, storage_key="private/resume", original_filename="resume.pdf", mime_type="application/pdf", size_bytes=12, sha256="0" * 64, uploaded_by=admin_id)
            db.add(file); db.flush()
            resume = Resume(organization_id=file.organization_id, candidate_id=UUID(candidate_id), file_object_id=file.id, version_number=1, parsed_text="must-not-leak")
            db.add(resume); db.commit(); resume_id = str(resume.id)

        app_headers = {**headers, "Idempotency-Key": "create-application"}
        application_payload = {"candidate_id": candidate_id, "resume_id": resume_id, "owner_id": str(recruiter_id)}
        created = client.post(f"/api/v1/jobs/{job_id}/applications", json=application_payload, headers=app_headers)
        replay = client.post(f"/api/v1/jobs/{job_id}/applications", json=application_payload, headers=app_headers)
        assert created.status_code == replay.status_code == 201
        assert created.json() == replay.json()
        conflict = client.post(f"/api/v1/jobs/{job_id}/applications", json={**application_payload, "source": "other"}, headers=app_headers)
        assert conflict.status_code == 409 and conflict.json()["code"] == "idempotency_conflict"
        history = client.get(f"/api/v1/candidates/{candidate_id}/applications")
        assert history.status_code == 200
        history_item = history.json()["data"][0]
        assert history.json()["meta"]["count"] == 1
        assert {key: history_item[key] for key in ("id", "job_id", "stage", "source", "source_application_id", "job_title")} == {
            "id": created.json()["data"]["id"],
            "job_id": job_id,
            "stage": "new",
            "source": "manual",
            "source_application_id": None,
            "job_title": "Senior Engineer",
        }

        preview = client.get(f"/api/v1/resumes/{resume_id}/preview")
        assert preview.status_code == 200 and preview.headers["cache-control"] == "no-store"
        assert preview.json()["data"]["text"] == "must-not-leak"
        file_preview = client.get(f"/api/v1/resumes/{resume_id}/file")
        assert file_preview.status_code == 200 and file_preview.content == b"private-file"
        assert file_preview.headers["cache-control"] == "no-store"
        assert file_preview.headers["content-type"].startswith("application/pdf")
        assert file_preview.headers["content-disposition"].startswith("inline;")
        assert file_preview.headers["x-content-type-options"] == "nosniff"
        assert app.state.resume_storage.last_spool.closed
        with app.state.identity_store.sync_session() as db:
            stored_resume = db.get(Resume, UUID(resume_id))
            stored_resume.parsed_text = "x" * (1024 * 1024 + 1)
            db.commit()
        oversized = client.get(f"/api/v1/resumes/{resume_id}/preview")
        assert oversized.status_code == 422 and oversized.json()["code"] == "preview_too_large"
        with app.state.identity_store.sync_session() as db:
            db.get(Resume, UUID(resume_id)).parsed_text = "must-not-leak"
            db.commit()
        ticket = client.post(f"/api/v1/resumes/{resume_id}/download-tickets", headers=headers)
        raw = ticket.json()["data"]["token"]
        assert raw not in str(ticket.request.url)
        download = client.post("/api/v1/download-tickets/consume", json={"token": raw}, headers=headers)
        assert download.content == b"private-file"
        assert download.headers["cache-control"] == "no-store"
        assert download.headers["content-disposition"].startswith("attachment;")
        assert download.headers["x-content-type-options"] == "nosniff"
        assert app.state.resume_storage.last_spool.closed
        assert client.post("/api/v1/download-tickets/consume", json={"token": raw}, headers=headers).status_code == 404
        with app.state.identity_store.sync_session() as db:
            assert raw not in repr([row.metadata_json for row in db.query(AuditLog).all()])


def test_system_admin_and_interviewer_have_no_recruiting_access_by_role_alone(tmp_path) -> None:
    app = make_app(tmp_path)
    seed_user(app, "system_admin", "system@example.test")
    seed_user(app, "interviewer", "interviewer@example.test")
    with TestClient(app) as client:
        for email in ("system@example.test", "interviewer@example.test"):
            login(client, email)
            jobs = client.get("/api/v1/jobs").json()
            assert jobs["data"] == [] and jobs["meta"]["limit"] == 50
            denied = client.post("/api/v1/candidates", json={"display_name": "Forbidden"}, headers={"Origin": "https://hr.example.test", "X-CSRF-Token": client.get('/api/v1/me', headers={'Sec-Fetch-Site': 'same-origin'}).headers['X-CSRF-Token']})
            assert denied.status_code == 404 and denied.json()["code"] == "resource_not_found"


def test_job_list_read_model_enriches_rows_filters_and_uses_full_scope_facets(tmp_path, monkeypatch) -> None:
    app = make_app(tmp_path)
    admin_id = seed_user(app, "recruiting_admin", "job-list-admin@example.test")
    owner_id = seed_user(app, "recruiter", "job-list-owner@example.test")
    hiring_owner_id = seed_user(app, "hiring_manager", "job-list-manager@example.test")
    base = datetime(2026, 5, 1, tzinfo=timezone.utc)
    with app.state.identity_store.sync_session() as db:
        admin = db.get(User, admin_id)
        db.get(User, admin_id).display_name = "Admin Owner"
        db.get(User, owner_id).display_name = "Recruiting Owner"
        db.get(User, hiring_owner_id).display_name = "Hiring Owner"
        engineering = Department(organization_id=admin.organization_id, name="Engineering")
        product = Department(organization_id=admin.organization_id, name="Product")
        db.add_all([engineering, product]); db.flush()
        jobs = [
            Job(organization_id=admin.organization_id, title="  Platform Engineer  ", department_id=engineering.id, owner_id=owner_id, hiring_owner_id=hiring_owner_id, status="open", updated_at=base + timedelta(hours=5)),
            Job(organization_id=admin.organization_id, title="Backend Engineer", department_id=engineering.id, owner_id=owner_id, status="draft", updated_at=base + timedelta(hours=4)),
            Job(organization_id=admin.organization_id, title="Product Manager", department_id=product.id, owner_id=admin_id, status="paused", updated_at=base + timedelta(hours=3)),
            Job(organization_id=admin.organization_id, title="Closed Role", owner_id=admin_id, status="closed", updated_at=base + timedelta(hours=2)),
            Job(organization_id=admin.organization_id, title="Archived Role", owner_id=admin_id, status="archived", updated_at=base + timedelta(hours=1)),
        ]
        db.add_all(jobs); db.flush()
        for index, stage in enumerate(("new", "new", "review")):
            candidate = Candidate(organization_id=admin.organization_id, display_name=f"Secret Candidate {index}")
            file = FileObject(organization_id=admin.organization_id, storage_key=f"private/job-list-{index}", original_filename=f"secret-{index}.pdf", mime_type="application/pdf", size_bytes=1, sha256=str(index + 1) * 64, uploaded_by=admin_id)
            db.add_all([candidate, file]); db.flush()
            resume = Resume(organization_id=admin.organization_id, candidate_id=candidate.id, file_object_id=file.id, version_number=1, parsed_text="secret resume text")
            db.add(resume); db.flush()
            db.add(Application(organization_id=admin.organization_id, candidate_id=candidate.id, job_id=jobs[0].id, resume_id=resume.id, owner_id=owner_id, stage=stage, human_conclusion="secret note"))
        db.add(JobJdVersion(organization_id=admin.organization_id, job_id=jobs[0].id, version_number=1, content={"description": "secret JD text"}, created_by=admin_id))
        db.add(ScreeningRuleVersion(organization_id=admin.organization_id, job_id=jobs[0].id, version_number=1, content={"must_have": ["secret rule text"]}, created_by=admin_id))
        db.commit()
        ids = {
            "engineering": str(engineering.id),
            "product": str(product.id),
            "owner": str(owner_id),
            "hiring_owner": str(hiring_owner_id),
        }

    monkeypatch.setattr(recruiting_api, "_principal", lambda request: Principal(
        user_id=admin_id,
        organization_id=admin.organization_id,
        roles=frozenset({"recruiting_admin"}),
        active=True,
    ))
    with TestClient(app) as client:
        statements = []
        def count_statement(conn, cursor, statement, parameters, context, executemany):
            statements.append(statement)
        event.listen(app.state.identity_store.engine, "before_cursor_execute", count_statement)
        try:
            page = client.get("/api/v1/jobs", params={"limit": 1})
        finally:
            event.remove(app.state.identity_store.engine, "before_cursor_execute", count_statement)
        by_q = client.get("/api/v1/jobs", params={"q": "  PLATFORM engineer  "})
        by_status = client.get("/api/v1/jobs", params={"status": "draft"})
        by_department = client.get("/api/v1/jobs", params={"department_id": ids["product"]})
        by_hiring_owner = client.get("/api/v1/jobs", params={"owner_id": ids["hiring_owner"]})
        by_fallback_owner = client.get("/api/v1/jobs", params={"owner_id": ids["owner"]})
        invalid_status = client.get("/api/v1/jobs", params={"status": "unknown"})
        overlong_q = client.get("/api/v1/jobs", params={"q": "x" * 201})

    assert page.status_code == 200
    assert len(statements) == 3
    row = page.json()["data"][0]
    assert row["department_name"] == "Engineering"
    assert row["owner_name"] == "Recruiting Owner"
    assert row["hiring_owner_name"] == "Hiring Owner"
    assert row["funnel"] == {"stages": {"new": 2, "review": 1}, "total": 3}
    assert page.json()["meta"] == {
        "limit": 1,
        "next_cursor": page.json()["meta"]["next_cursor"],
        "departments": [
            {"id": ids["engineering"], "name": "Engineering"},
            {"id": ids["product"], "name": "Product"},
        ],
        "owners": [
            {"id": str(admin_id), "name": "Admin Owner"},
            {"id": ids["hiring_owner"], "name": "Hiring Owner"},
            {"id": ids["owner"], "name": "Recruiting Owner"},
        ],
        "status_counts": {"draft": 1, "open": 1, "paused": 1, "closed": 1, "archived": 1},
    }
    assert [item["title"] for item in by_q.json()["data"]] == ["  Platform Engineer  "]
    assert [item["title"] for item in by_status.json()["data"]] == ["Backend Engineer"]
    assert [item["title"] for item in by_department.json()["data"]] == ["Product Manager"]
    assert [item["title"] for item in by_hiring_owner.json()["data"]] == ["  Platform Engineer  "]
    assert [item["title"] for item in by_fallback_owner.json()["data"]] == ["Backend Engineer"]
    assert invalid_status.status_code == overlong_q.status_code == 422
    serialized = repr(page.json()).casefold()
    for secret in ("candidate", "secret", "description", "must_have", "human_conclusion", "parsed_text", "screening"):
        assert secret not in serialized


def test_job_list_facets_funnels_and_rows_exclude_unauthorized_and_cross_tenant_facts(tmp_path) -> None:
    app = make_app(tmp_path)
    recruiter_id = seed_user(app, "recruiter", "job-list-scoped@example.test")
    denied_owner_id = seed_user(app, "recruiter", "job-list-denied@example.test")
    with app.state.identity_store.sync_session() as db:
        recruiter = db.get(User, recruiter_id)
        recruiter.display_name = "Allowed Owner"
        denied_owner = db.get(User, denied_owner_id)
        denied_owner.display_name = "Denied Owner"
        allowed_department = Department(organization_id=recruiter.organization_id, name="Allowed Department")
        denied_department = Department(organization_id=recruiter.organization_id, name="Denied Department")
        db.add_all([allowed_department, denied_department]); db.flush()
        allowed = Job(organization_id=recruiter.organization_id, title="Allowed Job", department_id=allowed_department.id, owner_id=recruiter_id, status="open")
        denied = Job(organization_id=recruiter.organization_id, title="Denied Job", department_id=denied_department.id, owner_id=denied_owner_id, status="closed")
        other_org = Organization(slug="other-job-list", name="Other", status="active")
        db.add(other_org); db.flush()
        other_user = User(organization_id=other_org.id, email="other-job-list@example.test", normalized_email="other-job-list@example.test", display_name="Other Owner", password_hash=PasswordService().hash("correct horse"))
        other_user.roles.append(UserRole(role="recruiter"))
        other_department = Department(organization_id=other_org.id, name="Other Department")
        db.add_all([allowed, denied, other_user, other_department]); db.flush()
        cross_tenant = Job(organization_id=other_org.id, title="Cross Tenant Job", department_id=other_department.id, owner_id=other_user.id, status="paused")
        db.add(cross_tenant); db.flush()
        db.add(JobCollaborator(organization_id=recruiter.organization_id, job_id=allowed.id, user_id=recruiter_id, access_role="job_recruiter"))
        denied_candidate = Candidate(organization_id=recruiter.organization_id, display_name="Denied Funnel Candidate")
        denied_file = FileObject(organization_id=recruiter.organization_id, storage_key="private/denied-job-funnel", original_filename="denied.pdf", mime_type="application/pdf", size_bytes=1, sha256="7" * 64, uploaded_by=recruiter_id)
        cross_candidate = Candidate(organization_id=other_org.id, display_name="Cross Tenant Funnel Candidate")
        cross_file = FileObject(organization_id=other_org.id, storage_key="private/cross-job-funnel", original_filename="cross.pdf", mime_type="application/pdf", size_bytes=1, sha256="8" * 64, uploaded_by=other_user.id)
        db.add_all([denied_candidate, denied_file, cross_candidate, cross_file]); db.flush()
        denied_resume = Resume(organization_id=recruiter.organization_id, candidate_id=denied_candidate.id, file_object_id=denied_file.id, version_number=1)
        cross_resume = Resume(organization_id=other_org.id, candidate_id=cross_candidate.id, file_object_id=cross_file.id, version_number=1)
        db.add_all([denied_resume, cross_resume]); db.flush()
        db.add_all([
            Application(organization_id=recruiter.organization_id, candidate_id=denied_candidate.id, job_id=denied.id, resume_id=denied_resume.id, owner_id=denied_owner_id, stage="rejected"),
            Application(organization_id=other_org.id, candidate_id=cross_candidate.id, job_id=cross_tenant.id, resume_id=cross_resume.id, owner_id=other_user.id, stage="hired"),
        ])
        db.commit()

    with TestClient(app) as client:
        login(client, "job-list-scoped@example.test")
        response = client.get("/api/v1/jobs")

    assert response.status_code == 200
    assert [row["title"] for row in response.json()["data"]] == ["Allowed Job"]
    assert response.json()["meta"]["departments"] == [{"id": str(allowed_department.id), "name": "Allowed Department"}]
    assert response.json()["meta"]["owners"] == [{"id": str(recruiter_id), "name": "Allowed Owner"}]
    assert response.json()["meta"]["status_counts"] == {"draft": 0, "open": 1, "paused": 0, "closed": 0, "archived": 0}
    assert response.json()["data"][0]["funnel"] == {"stages": {}, "total": 0}
    assert "rejected" not in response.text and "hired" not in response.text
    assert "Denied" not in response.text and "Cross Tenant" not in response.text and "Other" not in response.text


def test_job_list_filtered_cursor_is_stable_and_rejects_filter_mismatch(tmp_path) -> None:
    app = make_app(tmp_path)
    admin_id = seed_user(app, "recruiting_admin", "job-list-cursor@example.test")
    base = datetime(2026, 6, 1, tzinfo=timezone.utc)
    with app.state.identity_store.sync_session() as db:
        admin = db.get(User, admin_id)
        for index in range(3):
            db.add(Job(organization_id=admin.organization_id, title=f"Engineer {index}", owner_id=admin_id, status="open", updated_at=base + timedelta(hours=index)))
        newest = Job(organization_id=admin.organization_id, title="Draft Engineer", owner_id=admin_id, status="draft", updated_at=base + timedelta(hours=4))
        db.add(newest); db.commit()
        legacy_cursor = app.state.recruiting_cursor.encode(str(admin.organization_id), "jobs:-updated_at", newest.updated_at.isoformat(), str(newest.id))

    with TestClient(app) as client:
        login(client, "job-list-cursor@example.test")
        first = client.get("/api/v1/jobs", params={"q": " engineer ", "status": "open", "limit": 2})
        second = client.get("/api/v1/jobs", params={"q": "ENGINEER", "status": "open", "limit": 2, "cursor": first.json()["meta"]["next_cursor"]})
        mismatch = client.get("/api/v1/jobs", params={"q": "engineer", "status": "draft", "limit": 2, "cursor": first.json()["meta"]["next_cursor"]})
        legacy_page = client.get("/api/v1/jobs", params={"cursor": legacy_cursor})

    assert first.status_code == second.status_code == 200
    assert [row["title"] for row in first.json()["data"] + second.json()["data"]] == ["Engineer 2", "Engineer 1", "Engineer 0"]
    assert second.json()["meta"]["next_cursor"] is None
    assert mismatch.status_code == 422 and mismatch.json()["code"] == "validation_failed"
    assert legacy_page.status_code == 200
    assert [row["title"] for row in legacy_page.json()["data"]] == ["Engineer 2", "Engineer 1", "Engineer 0"]


def test_job_list_cursor_scope_canonicalization_prevents_delimiter_collisions(tmp_path) -> None:
    app = make_app(tmp_path)
    admin_id = seed_user(app, "recruiting_admin", "job-list-collision@example.test")
    base = datetime(2026, 6, 2, tzinfo=timezone.utc)
    with app.state.identity_store.sync_session() as db:
        admin = db.get(User, admin_id)
        for index in range(2):
            db.add(Job(organization_id=admin.organization_id, title=f"Engineer\x1fopen {index}", owner_id=admin_id, status="open", updated_at=base + timedelta(hours=index)))
        db.commit()
        organization_id = str(admin.organization_id)

    with TestClient(app) as client:
        login(client, "job-list-collision@example.test")
        first = client.get("/api/v1/jobs", params={"q": "Engineer\x1fopen", "status": "open", "limit": 1})
        token = first.json()["meta"]["next_cursor"]
        mismatch = client.get("/api/v1/jobs", params={"q": "Engineer\x1fopen", "status": "draft", "limit": 1, "cursor": token})

    assert first.status_code == 200
    assert token is not None
    canonical = json.dumps({
        "department_id": None,
        "owner_id": None,
        "q": "engineer\x1fopen",
        "status": "open",
    }, sort_keys=True, separators=(",", ":"))
    scope_hash = hashlib.sha256(canonical.encode()).hexdigest()
    app.state.recruiting_cursor.decode(token, organization_id, f"jobs:-updated_at:{scope_hash}")
    assert mismatch.status_code == 422 and mismatch.json()["code"] == "validation_failed"


def test_job_list_search_treats_sql_wildcards_as_literal_text(tmp_path) -> None:
    app = make_app(tmp_path)
    admin_id = seed_user(app, "recruiting_admin", "job-list-wildcards@example.test")
    with app.state.identity_store.sync_session() as db:
        admin = db.get(User, admin_id)
        for title in ("100% Engineer", "100X Engineer", "Level_One", "LevelXOne"):
            db.add(Job(organization_id=admin.organization_id, title=title, owner_id=admin_id))
        db.commit()

    with TestClient(app) as client:
        login(client, "job-list-wildcards@example.test")
        percent = client.get("/api/v1/jobs", params={"q": "%"})
        underscore = client.get("/api/v1/jobs", params={"q": "_"})

    assert [row["title"] for row in percent.json()["data"]] == ["100% Engineer"]
    assert [row["title"] for row in underscore.json()["data"]] == ["Level_One"]


def test_job_list_search_validates_length_after_trimming(tmp_path) -> None:
    app = make_app(tmp_path)
    admin_id = seed_user(app, "recruiting_admin", "job-list-length@example.test")
    with app.state.identity_store.sync_session() as db:
        admin = db.get(User, admin_id)
        db.add(Job(organization_id=admin.organization_id, title="x" * 200, owner_id=admin_id))
        db.commit()

    with TestClient(app) as client:
        login(client, "job-list-length@example.test")
        boundary = client.get("/api/v1/jobs", params={"q": f"  {'x' * 200}  "})
        overlong = client.get("/api/v1/jobs", params={"q": f"  {'x' * 201}  "})

    assert boundary.status_code == 200
    assert [row["title"] for row in boundary.json()["data"]] == ["x" * 200]
    assert overlong.status_code == 422 and overlong.json()["code"] == "validation_failed"


def test_recruiting_mutations_preserve_central_csrf_boundary(tmp_path) -> None:
    app = make_app(tmp_path)
    seed_user(app, "recruiting_admin", "admin@example.test")
    with TestClient(app) as client:
        headers = login(client, "admin@example.test")
        cases = [
            {},
            {"Origin": "https://hr.example.test", "X-CSRF-Token": "wrong"},
            {"Origin": "https://evil.test", "X-CSRF-Token": headers["X-CSRF-Token"]},
        ]
        for bad_headers in cases:
            response = client.post("/api/v1/jobs", json={"title": "Forbidden"}, headers=bad_headers)
            assert response.status_code == 403 and response.json()["code"] == "csrf_validation_failed"


def test_owning_recruiter_reads_unassigned_timeline_but_cannot_add_unscoped_note(tmp_path) -> None:
    app = make_app(tmp_path)
    seed_user(app, "recruiter", "owner@example.test")
    with TestClient(app) as client:
        headers = login(client, "owner@example.test")
        candidate = client.post("/api/v1/candidates", json={"display_name": "Unassigned"}, headers=headers).json()["data"]
        timeline = client.get(f"/api/v1/candidates/{candidate['id']}/timeline")
        assert timeline.status_code == 200
        assert [event["event_type"] for event in timeline.json()["data"]] == ["candidate.created"]
        note = client.post(f"/api/v1/candidates/{candidate['id']}/notes", json={"body": "Allowed comment"}, headers=headers)
        assert note.status_code == 422 and note.json()["code"] == "validation_failed"


def test_resume_access_is_scoped_to_an_authorized_application_for_the_target_resume(tmp_path) -> None:
    app = make_app(tmp_path)
    recruiter_id = seed_user(app, "recruiter", "scoped-resume@example.test")
    ids = seed_same_candidate_cross_job(app, recruiter_id)

    with TestClient(app) as client:
        headers = login(client, "scoped-resume@example.test")
        listed = client.get(f"/api/v1/candidates/{ids['candidate_id']}/resumes")
        assert listed.status_code == 200
        assert [row["id"] for row in listed.json()["data"]] == [ids["allowed_resume_id"]]
        assert listed.json()["data"][0]["profile"] == {
            "summary": "企业级 AI 平台负责人",
            "summary_origin": "resume",
            "skills": ["Python", "RAG"],
            "experience": "负责 Agent 平台交付",
            "education": "浙江大学 计算机本科",
            "status": "ready",
            "source": "rules",
        }

        preview = client.get(f"/api/v1/resumes/{ids['allowed_resume_id']}/preview")
        assert preview.status_code == 200
        assert preview.json()["data"]["text"].startswith("个人简介\n企业级 AI 平台负责人")
        assert client.get(f"/api/v1/resumes/{ids['denied_resume_id']}/preview").status_code == 404
        assert client.get(f"/api/v1/resumes/{ids['allowed_resume_id']}/file").status_code == 200
        assert client.get(f"/api/v1/resumes/{ids['denied_resume_id']}/file").status_code == 404
        assert client.post(f"/api/v1/resumes/{ids['denied_resume_id']}/download-tickets", headers=headers).status_code == 404

        first_ticket = client.post(f"/api/v1/resumes/{ids['allowed_resume_id']}/download-tickets", headers=headers)
        assert first_ticket.status_code == 201
        downloaded = client.post("/api/v1/download-tickets/consume", json={"token": first_ticket.json()["data"]["token"]}, headers=headers)
        assert downloaded.status_code == 200 and downloaded.content == b"private-file"

        second_ticket = client.post(f"/api/v1/resumes/{ids['allowed_resume_id']}/download-tickets", headers=headers)
        assert second_ticket.status_code == 201
        with app.state.identity_store.sync_session() as db:
            db.query(JobCollaborator).filter_by(job_id=ids["allowed_job_id"], user_id=recruiter_id).delete()
            db.add(JobCollaborator(organization_id=db.get(User, recruiter_id).organization_id, job_id=ids["denied_job_id"], user_id=recruiter_id, access_role="job_recruiter"))
            db.commit()
        denied_consume = client.post("/api/v1/download-tickets/consume", json={"token": second_ticket.json()["data"]["token"]}, headers=headers)
        assert denied_consume.status_code == 404


def test_candidate_notes_require_and_isolate_by_authorized_application(tmp_path) -> None:
    app = make_app(tmp_path)
    recruiter_id = seed_user(app, "recruiter", "scoped-note@example.test")
    ids = seed_same_candidate_cross_job(app, recruiter_id)

    with TestClient(app) as client:
        headers = login(client, "scoped-note@example.test")
        missing_read = client.get(f"/api/v1/candidates/{ids['candidate_id']}/notes")
        missing_write = client.post(f"/api/v1/candidates/{ids['candidate_id']}/notes", json={"body": "missing application"}, headers=headers)
        assert missing_read.status_code == missing_write.status_code == 422

        denied_read = client.get(f"/api/v1/candidates/{ids['candidate_id']}/notes", params={"application_id": ids["denied_application_id"]})
        denied_write = client.post(f"/api/v1/candidates/{ids['candidate_id']}/notes", json={"application_id": ids["denied_application_id"], "body": "cross-job note"}, headers=headers)
        assert denied_read.status_code == denied_write.status_code == 404

        created = client.post(f"/api/v1/candidates/{ids['candidate_id']}/notes", json={"application_id": ids["allowed_application_id"], "body": "allowed job note"}, headers=headers)
        assert created.status_code == 201
        assert created.json()["data"]["application_id"] == ids["allowed_application_id"]
        listed = client.get(f"/api/v1/candidates/{ids['candidate_id']}/notes", params={"application_id": ids["allowed_application_id"]})
        assert listed.status_code == 200
        assert [(row["application_id"], row["body"]) for row in listed.json()["data"]] == [(ids["allowed_application_id"], "allowed job note")]

        timeline = client.get(f"/api/v1/candidates/{ids['candidate_id']}/timeline")
        assert timeline.status_code == 200
        assert [row["event_type"] for row in timeline.json()["data"]] == ["candidate.note_added"]

        denied_conclusion = client.patch(
            f"/api/v1/applications/{ids['allowed_application_id']}",
            json={"human_conclusion": "越权结论"},
            headers={**headers, "If-Match": '"1"'},
        )
        assert denied_conclusion.status_code == 404


def test_job_owner_can_save_human_conclusion_for_collaborated_job(tmp_path) -> None:
    app = make_app(tmp_path)
    recruiter_id = seed_user(app, "recruiter", "recruiter@example.test")
    with app.state.identity_store.sync_session() as db:
        user = db.get(User, recruiter_id)
        job = Job(organization_id=user.organization_id, title="Recruiter job", owner_id=recruiter_id)
        candidate = Candidate(organization_id=user.organization_id, display_name="Candidate", owner_id=recruiter_id)
        file = FileObject(organization_id=user.organization_id, storage_key="private/recruiter-resume", original_filename="resume.pdf", mime_type="application/pdf", size_bytes=12, sha256="8" * 64, uploaded_by=recruiter_id)
        db.add_all([job, candidate, file]); db.flush()
        resume = Resume(organization_id=user.organization_id, candidate_id=candidate.id, file_object_id=file.id, version_number=1)
        db.add(resume); db.flush()
        application = Application(organization_id=user.organization_id, candidate_id=candidate.id, job_id=job.id, resume_id=resume.id, owner_id=recruiter_id)
        db.add(application); db.flush()
        db.add_all([
            JobCollaborator(organization_id=user.organization_id, job_id=job.id, user_id=recruiter_id, access_role="job_owner"),
            CandidateEvent(organization_id=user.organization_id, candidate_id=candidate.id, actor_user_id=recruiter_id, event_type="application.created", payload={"application_id": str(application.id), "job_id": str(job.id)}),
        ]); db.commit()
        application_id, candidate_id = str(application.id), str(candidate.id)

    with TestClient(app) as client:
        headers = login(client, "recruiter@example.test")
        response = client.patch(f"/api/v1/applications/{application_id}", json={"human_conclusion": "建议推进"}, headers={**headers, "If-Match": '"1"'})

    assert response.status_code == 200
    assert response.json()["data"]["human_conclusion"] == "建议推进"
    with TestClient(app) as client:
        login(client, "recruiter@example.test")
        timeline = client.get(f"/api/v1/candidates/{candidate_id}/timeline")
    assert [event["event_type"] for event in timeline.json()["data"]] == ["application.updated", "application.created"]
    assert [event["summary"] for event in timeline.json()["data"]] == ["Application updated", "Application created"]
    assert all(event["actor_id"] == str(recruiter_id) for event in timeline.json()["data"])
    assert "建议推进" not in timeline.text


def test_application_stage_timeline_summary_includes_safe_transition_reason(tmp_path) -> None:
    app = make_app(tmp_path)
    admin_id = seed_user(app, "recruiting_admin", "admin@example.test")
    recruiter_id = seed_user(app, "recruiter", "recruiter@example.test")
    with app.state.identity_store.sync_session() as db:
        admin = db.get(User, admin_id)
        job = Job(organization_id=admin.organization_id, title="Engineer", owner_id=admin_id)
        candidate = Candidate(organization_id=admin.organization_id, display_name="Candidate", owner_id=recruiter_id)
        file = FileObject(organization_id=admin.organization_id, storage_key="private/stage-resume", original_filename="resume.pdf", mime_type="application/pdf", size_bytes=12, sha256="a" * 64, uploaded_by=admin_id)
        db.add_all([job, candidate, file]); db.flush()
        resume = Resume(organization_id=admin.organization_id, candidate_id=candidate.id, file_object_id=file.id, version_number=1)
        db.add(resume); db.flush()
        application = Application(organization_id=admin.organization_id, candidate_id=candidate.id, job_id=job.id, resume_id=resume.id, owner_id=recruiter_id, stage="review", human_conclusion="建议推进：技术能力符合")
        db.add(application); db.commit()
        application_id, candidate_id = str(application.id), str(candidate.id)

    reason = "岗位核心经验不足"
    with TestClient(app) as client:
        headers = login(client, "admin@example.test")
        changed = client.post(
            f"/api/v1/applications/{application_id}/workflow-actions",
            json={"action": "review_rejected", "reason_text": reason},
            headers={**headers, "If-Match": '"1"', "Idempotency-Key": "reject-with-reason"},
        )
        assert changed.status_code == 200
        timeline = client.get(f"/api/v1/candidates/{candidate_id}/timeline")

    assert timeline.status_code == 200
    stage_event = next(event for event in timeline.json()["data"] if event["event_type"] == "application.stage_changed")
    assert stage_event["summary"] == f"Application stage changed from review to rejected: {reason}"
    with app.state.identity_store.sync_session() as db:
        assert db.query(ApplicationStageEvent).one().payload["reason_text"] == reason
        assert db.get(Application, UUID(application_id)).human_conclusion == "建议推进：技术能力符合"


def test_multi_role_manager_grant_cannot_be_crossed_with_recruiter_actions(tmp_path) -> None:
    app = make_app(tmp_path)
    user_id = seed_user(app, "recruiter", "mixed@example.test")
    with app.state.identity_store.sync_session() as db:
        user = db.get(User, user_id)
        user.roles.append(UserRole(role="hiring_manager"))
        job = Job(organization_id=user.organization_id, title="Managed", owner_id=user_id)
        candidate = Candidate(organization_id=user.organization_id, display_name="Attached", owner_id=None)
        file = FileObject(organization_id=user.organization_id, storage_key="private/resume", original_filename="resume.pdf", mime_type="application/pdf", size_bytes=12, sha256="7" * 64, uploaded_by=user_id)
        db.add_all([job, candidate, file]); db.flush()
        resume = Resume(organization_id=user.organization_id, candidate_id=candidate.id, file_object_id=file.id, version_number=1, parsed_text="preview")
        db.add(resume); db.flush()
        application = Application(organization_id=user.organization_id, candidate_id=candidate.id, job_id=job.id, resume_id=resume.id, owner_id=user_id)
        db.add_all([application, JobCollaborator(organization_id=user.organization_id, job_id=job.id, user_id=user_id, access_role="job_manager")]); db.commit()
        ids = str(job.id), str(candidate.id), str(resume.id), str(application.id)
    job_id, candidate_id, resume_id, application_id = ids

    with TestClient(app) as client:
        headers = login(client, "mixed@example.test")
        assert client.get(f"/api/v1/jobs/{job_id}").status_code == 200
        assert client.get(f"/api/v1/candidates/{candidate_id}").status_code == 200
        preview = client.get(f"/api/v1/resumes/{resume_id}/preview")
        assert preview.status_code == 200 and preview.json()["data"]["text"] == "preview"
        assert client.post(f"/api/v1/candidates/{candidate_id}/notes", json={"application_id": application_id, "body": "Manager comment"}, headers=headers).status_code == 201
        recommendation = client.patch(f"/api/v1/applications/{application_id}", json={"human_conclusion": "recommend"}, headers={**headers, "If-Match": '"1"'})
        assert recommendation.status_code == 200 and recommendation.json()["data"]["human_conclusion"] == "recommend"
        mixed = client.patch(f"/api/v1/applications/{application_id}", json={"owner_id": str(user_id), "human_conclusion": "mixed"}, headers={**headers, "If-Match": '"2"'})
        assert mixed.status_code == 404 and mixed.json()["code"] == "resource_not_found"
        denied = [
            client.patch(f"/api/v1/jobs/{job_id}", json={"title": "Takeover"}, headers={**headers, "If-Match": '"1"'}),
            client.post(f"/api/v1/jobs/{job_id}/transitions", json={"target": "open"}, headers={**headers, "If-Match": '"1"', "Idempotency-Key": "mixed-transition"}),
            client.post(f"/api/v1/jobs/{job_id}/jd-versions", json={"content": {"text": "takeover"}}, headers=headers),
            client.post(f"/api/v1/jobs/{job_id}/applications", json={"candidate_id": candidate_id, "resume_id": resume_id}, headers={**headers, "Idempotency-Key": "mixed-create"}),
            client.post(f"/api/v1/resumes/{resume_id}/download-tickets", headers=headers),
            client.post(f"/api/v1/applications/{application_id}/transitions", json={"target": "review"}, headers={**headers, "If-Match": '"1"', "Idempotency-Key": "mixed-app-transition"}),
        ]
        assert all(response.status_code == 404 for response in denied)


@pytest.mark.parametrize("role", ["hiring_manager", "system_admin", "interviewer"])
def test_non_recruiter_owner_id_never_grants_unassigned_candidate_access(tmp_path, role) -> None:
    app = make_app(tmp_path)
    user_id = seed_user(app, role, f"{role}@example.test")
    with app.state.identity_store.sync_session() as db:
        user = db.get(User, user_id)
        candidate = Candidate(organization_id=user.organization_id, display_name="Legacy owner collision", owner_id=user_id)
        db.add(candidate); db.commit(); candidate_id = str(candidate.id)
    with TestClient(app) as client:
        login(client, f"{role}@example.test")
        response = client.get(f"/api/v1/candidates/{candidate_id}")
        assert response.status_code == 404 and response.json()["code"] == "resource_not_found"


def test_candidate_owner_assignment_requires_active_same_org_recruiter_on_create_and_patch(tmp_path) -> None:
    app = make_app(tmp_path)
    admin_id = seed_user(app, "recruiting_admin", "admin@example.test")
    eligible_id = seed_user(app, "recruiter", "eligible@example.test")
    second_id = seed_user(app, "recruiter", "second@example.test")
    disabled_id = seed_user(app, "recruiter", "disabled@example.test")
    non_recruiter_id = seed_user(app, "hiring_manager", "manager@example.test")
    with app.state.identity_store.sync_session() as db:
        db.get(User, disabled_id).status = UserStatus.DISABLED
        other_org = Organization(slug="other-owner-org", name="Other", status="active")
        cross_org = User(organization=other_org, email="cross@example.test", normalized_email="cross@example.test", display_name="Cross", password_hash=PasswordService().hash("correct horse"))
        cross_org.roles.append(UserRole(role="recruiter")); db.add(cross_org); db.commit(); cross_org_id = cross_org.id
    invalid = [str(disabled_id), str(non_recruiter_id), str(cross_org_id), "00000000-0000-0000-0000-000000000099"]
    with TestClient(app) as client:
        headers = login(client, "admin@example.test")
        for owner_id in invalid:
            denied = client.post("/api/v1/candidates", json={"display_name": "Denied", "owner_id": owner_id}, headers=headers)
            assert denied.status_code == 404 and denied.json()["code"] == "resource_not_found"
        created = client.post("/api/v1/candidates", json={"display_name": "Allowed", "owner_id": str(eligible_id)}, headers=headers)
        assert created.status_code == 201 and created.json()["data"]["owner_id"] == str(eligible_id)
        candidate_id = created.json()["data"]["id"]
        for owner_id in invalid:
            denied = client.patch(f"/api/v1/candidates/{candidate_id}", json={"owner_id": owner_id}, headers={**headers, "If-Match": '"1"'})
            assert denied.status_code == 404 and denied.json()["code"] == "resource_not_found"
        changed = client.patch(f"/api/v1/candidates/{candidate_id}", json={"owner_id": str(second_id)}, headers={**headers, "If-Match": '"1"'})
        assert changed.status_code == 200 and changed.json()["data"]["owner_id"] == str(second_id)


def test_application_owner_assignment_requires_active_same_org_recruiter_on_create_and_patch(tmp_path) -> None:
    app = make_app(tmp_path)
    admin_id = seed_user(app, "recruiting_admin", "admin@example.test")
    eligible_id = seed_user(app, "recruiter", "eligible@example.test")
    second_id = seed_user(app, "recruiter", "second@example.test")
    disabled_id = seed_user(app, "recruiter", "disabled@example.test")
    non_recruiter_id = seed_user(app, "hiring_manager", "manager@example.test")
    with app.state.identity_store.sync_session() as db:
        admin = db.get(User, admin_id)
        db.get(User, disabled_id).status = UserStatus.DISABLED
        other_org = Organization(slug="other-application-owner-org", name="Other", status="active")
        cross_org = User(organization=other_org, email="cross@example.test", normalized_email="cross@example.test", display_name="Cross", password_hash=PasswordService().hash("correct horse"))
        cross_org.roles.append(UserRole(role="recruiter"))
        job = Job(organization_id=admin.organization_id, title="Engineer", owner_id=admin_id)
        candidate = Candidate(organization_id=admin.organization_id, display_name="Candidate", owner_id=eligible_id)
        file = FileObject(organization_id=admin.organization_id, storage_key="private/application-owner", original_filename="resume.pdf", mime_type="application/pdf", size_bytes=12, sha256="9" * 64, uploaded_by=admin_id)
        db.add_all([cross_org, job, candidate, file]); db.flush()
        resume = Resume(organization_id=admin.organization_id, candidate_id=candidate.id, file_object_id=file.id, version_number=1)
        db.add_all([resume, JobCollaborator(organization_id=admin.organization_id, job_id=job.id, user_id=admin_id, access_role="job_owner")]); db.commit()
        job_id, candidate_id, resume_id, cross_org_id = map(str, (job.id, candidate.id, resume.id, cross_org.id))

    with TestClient(app) as client:
        headers = login(client, "admin@example.test")
        payload = {"candidate_id": candidate_id, "resume_id": resume_id, "owner_id": str(disabled_id)}
        denied_create = client.post(f"/api/v1/jobs/{job_id}/applications", json=payload, headers={**headers, "Idempotency-Key": "invalid-application-owner"})
        assert denied_create.status_code == 404 and denied_create.json()["code"] == "resource_not_found"

        payload["owner_id"] = str(eligible_id)
        created = client.post(f"/api/v1/jobs/{job_id}/applications", json=payload, headers={**headers, "Idempotency-Key": "valid-application-owner"})
        assert created.status_code == 201 and created.json()["data"]["owner_id"] == str(eligible_id)
        application_id = created.json()["data"]["id"]

        for owner_id in (str(disabled_id), str(non_recruiter_id), cross_org_id, "00000000-0000-0000-0000-000000000099"):
            denied_patch = client.patch(f"/api/v1/applications/{application_id}", json={"owner_id": owner_id}, headers={**headers, "If-Match": '"1"'})
            assert denied_patch.status_code == 404 and denied_patch.json()["code"] == "resource_not_found"

        changed = client.patch(f"/api/v1/applications/{application_id}", json={"owner_id": str(second_id)}, headers={**headers, "If-Match": '"1"'})
        assert changed.status_code == 200 and changed.json()["data"]["owner_id"] == str(second_id)


@pytest.mark.parametrize("failure", ["open", "mid-read"])
def test_download_storage_failure_leaves_ticket_usable_and_has_no_success_audit(tmp_path, failure) -> None:
    app = make_app(tmp_path, FailingStorage(failure))
    admin_id = seed_user(app, "recruiting_admin", "admin@example.test")
    with TestClient(app) as client:
        headers = login(client, "admin@example.test")
        candidate = client.post("/api/v1/candidates", json={"display_name": "Candidate"}, headers=headers).json()["data"]
        with app.state.identity_store.sync_session() as db:
            user = db.get(User, admin_id)
            job = Job(organization_id=user.organization_id, title="Storage failure job", owner_id=admin_id)
            file = FileObject(organization_id=user.organization_id, storage_key="private/resume", original_filename="resume.pdf", mime_type="application/pdf", size_bytes=12, sha256="6" * 64, uploaded_by=admin_id)
            db.add_all([job, file]); db.flush()
            resume = Resume(organization_id=user.organization_id, candidate_id=UUID(candidate["id"]), file_object_id=file.id, version_number=1, parsed_text="preview")
            db.add(resume); db.flush()
            db.add(Application(organization_id=user.organization_id, candidate_id=UUID(candidate["id"]), job_id=job.id, resume_id=resume.id, owner_id=admin_id))
            db.commit(); resume_id = str(resume.id)
        ticket = client.post(f"/api/v1/resumes/{resume_id}/download-tickets", headers=headers).json()["data"]["token"]
        response = client.post("/api/v1/download-tickets/consume", json={"token": ticket}, headers=headers)
        assert response.status_code == 503 and response.json()["code"] == "attachment_unavailable"
        with app.state.identity_store.sync_session() as db:
            assert db.query(DownloadTicket).one().consumed_at is None
            assert db.query(AuditLog).filter_by(event_type="resume.downloaded").count() == 0


def test_candidate_list_returns_selected_application_and_latest_screening_result(tmp_path) -> None:
    app = make_app(tmp_path)
    admin_id = seed_user(app, "recruiting_admin", "admin-list@example.test")
    first_owner_id = seed_user(app, "recruiter", "first-owner@example.test")
    second_owner_id = seed_user(app, "recruiter", "second-owner@example.test")
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with app.state.identity_store.sync_session() as db:
        admin = db.get(User, admin_id)
        db.get(User, first_owner_id).display_name = "First Owner"
        db.get(User, second_owner_id).display_name = "Second Owner"
        matching_job = Job(organization_id=admin.organization_id, title="Platform Engineer", owner_id=admin_id)
        newer_job = Job(organization_id=admin.organization_id, title="Product Engineer", owner_id=admin_id)
        candidate = Candidate(organization_id=admin.organization_id, display_name="Alice", owner_id=second_owner_id, updated_at=base)
        matching_file = FileObject(organization_id=admin.organization_id, storage_key="private/matching", original_filename="matching.pdf", mime_type="application/pdf", size_bytes=1, sha256="a" * 64, uploaded_by=admin_id)
        newer_file = FileObject(organization_id=admin.organization_id, storage_key="private/newer", original_filename="newer.pdf", mime_type="application/pdf", size_bytes=1, sha256="b" * 64, uploaded_by=admin_id)
        db.add_all([matching_job, newer_job, candidate, matching_file, newer_file]); db.flush()
        matching_resume = Resume(organization_id=admin.organization_id, candidate_id=candidate.id, file_object_id=matching_file.id, version_number=1)
        newer_resume = Resume(organization_id=admin.organization_id, candidate_id=candidate.id, file_object_id=newer_file.id, version_number=2)
        db.add_all([matching_resume, newer_resume]); db.flush()
        matching = Application(organization_id=admin.organization_id, candidate_id=candidate.id, job_id=matching_job.id, resume_id=matching_resume.id, owner_id=first_owner_id, stage="review", source="upload", updated_at=base)
        newer = Application(organization_id=admin.organization_id, candidate_id=candidate.id, job_id=newer_job.id, resume_id=newer_resume.id, owner_id=second_owner_id, stage="new", source="manual", updated_at=base + timedelta(hours=1))
        db.add_all([matching, newer]); db.flush()
        matching_item,matching_results=seed_screening_results(db, matching, matching_file.id, admin_id, [
            ("rule-old", 95, "优先沟通", base),
            ("rule-latest", 81, "可沟通", base + timedelta(minutes=1)),
        ])
        _,newer_results=seed_screening_results(db, newer, newer_file.id, admin_id, [("rule-newer", 60, "暂缓", base)])
        seed_llm_evaluation(db,matching,admin_id,matching_results[-1],72,"建议评审",base+timedelta(minutes=2))
        seed_llm_evaluation(db,newer,admin_id,newer_results[-1],91,"优先评审",base+timedelta(minutes=2))
        seed_terminal_route_audit(db,matching,matching_item,admin_id,created_at=base+timedelta(minutes=3))
        db.commit()
        ids = {
            "candidate": str(candidate.id),
            "matching_application": str(matching.id),
            "newer_application": str(newer.id),
            "matching_job": str(matching_job.id),
            "matching_resume": str(matching_resume.id),
            "first_owner": str(first_owner_id),
        }

    with TestClient(app) as client:
        login(client, "admin-list@example.test")
        unfiltered = client.get("/api/v1/candidates")
        filtered = client.get(
            "/api/v1/candidates",
            params={"job_id": ids["matching_job"], "stage": "review", "source": "upload", "owner_id": ids["first_owner"], "min_score": 70},
        )
        above_latest = client.get("/api/v1/candidates", params={"job_id": ids["matching_job"], "min_score": 80})
        owner_filtered = client.get("/api/v1/candidates", params={"owner_id": ids["first_owner"], "limit": 1})
        history = client.get(f"/api/v1/candidates/{ids['candidate']}/applications")

    assert unfiltered.status_code == filtered.status_code == above_latest.status_code == 200
    assert unfiltered.json()["data"][0]["application"]["id"] == ids["newer_application"]
    assert filtered.json()["data"] == [{
        **{key: value for key, value in filtered.json()["data"][0].items() if key != "application"},
        "application": {
            "id": ids["matching_application"],
            "job_id": ids["matching_job"],
            "job_title": "Platform Engineer",
            "resume_id": ids["matching_resume"],
            "owner_id": ids["first_owner"],
            "owner_name": "First Owner",
            "stage": "review",
            "source": "upload",
            "human_conclusion": None,
            "version": 1,
                "updated_at": base.replace(tzinfo=None).isoformat(),
                "next_interview_round": None,
                "rule_score": 81,
            "recommendation": "可沟通",
            "route_result": "review",
            "ai_score": 72,
            "ai_recommendation": "建议评审",
            "llm_status": "succeeded",
            "llm_error_code": None,
            "llm_evaluation": {
                "score":72,"recommendation":"建议评审","dimensions":filtered.json()["data"][0]["application"]["llm_evaluation"]["dimensions"],
                "summary":"persisted summary","strengths":["persisted strength"],"gaps":["persisted gap"],"risks":["persisted risk"],"questions":["persisted question"],
            },
        },
    }]
    assert filtered.json()["data"][0]["id"] == ids["candidate"]
    assert above_latest.json()["data"] == []
    assert owner_filtered.status_code == 200
    assert owner_filtered.json()["meta"]["owners"] == [
        {"id": ids["first_owner"], "name": "First Owner"},
        {"id": str(second_owner_id), "name": "Second Owner"},
    ]
    matching_history=next(row for row in history.json()["data"] if row["id"]==ids["matching_application"])
    assert matching_history["ai_score"]==72
    assert matching_history["llm_evaluation"]["dimensions"]==filtered.json()["data"][0]["application"]["llm_evaluation"]["dimensions"]
    assert "private prompt" not in history.text and "private-key" not in history.text


def test_candidate_list_never_selects_or_filters_on_unauthorized_applications(tmp_path) -> None:
    app = make_app(tmp_path)
    recruiter_id = seed_user(app, "recruiter", "scoped-list@example.test")
    denied_owner_id = seed_user(app, "recruiter", "denied-owner@example.test")
    base = datetime(2026, 2, 1, tzinfo=timezone.utc)
    with app.state.identity_store.sync_session() as db:
        recruiter = db.get(User, recruiter_id)
        allowed_job = Job(organization_id=recruiter.organization_id, title="Allowed", owner_id=recruiter_id)
        denied_job = Job(organization_id=recruiter.organization_id, title="Denied", owner_id=denied_owner_id)
        candidate = Candidate(organization_id=recruiter.organization_id, display_name="Scoped", owner_id=recruiter_id)
        allowed_file = FileObject(organization_id=recruiter.organization_id, storage_key="private/list-allowed", original_filename="allowed.pdf", mime_type="application/pdf", size_bytes=1, sha256="c" * 64, uploaded_by=recruiter_id)
        denied_file = FileObject(organization_id=recruiter.organization_id, storage_key="private/list-denied", original_filename="denied.pdf", mime_type="application/pdf", size_bytes=1, sha256="d" * 64, uploaded_by=recruiter_id)
        db.add_all([allowed_job, denied_job, candidate, allowed_file, denied_file]); db.flush()
        allowed_resume = Resume(organization_id=recruiter.organization_id, candidate_id=candidate.id, file_object_id=allowed_file.id, version_number=1)
        denied_resume = Resume(organization_id=recruiter.organization_id, candidate_id=candidate.id, file_object_id=denied_file.id, version_number=2)
        db.add_all([allowed_resume, denied_resume]); db.flush()
        allowed = Application(organization_id=recruiter.organization_id, candidate_id=candidate.id, job_id=allowed_job.id, resume_id=allowed_resume.id, owner_id=recruiter_id, updated_at=base)
        denied = Application(organization_id=recruiter.organization_id, candidate_id=candidate.id, job_id=denied_job.id, resume_id=denied_resume.id, owner_id=denied_owner_id, updated_at=base + timedelta(hours=1))
        db.add_all([allowed, denied]); db.flush()
        db.add(JobCollaborator(organization_id=recruiter.organization_id, job_id=allowed_job.id, user_id=recruiter_id, access_role="job_recruiter"))
        seed_screening_results(db, allowed, allowed_file.id, recruiter_id, [("allowed", 70, "可沟通", base)])
        seed_screening_results(db, denied, denied_file.id, denied_owner_id, [("denied", 99, "优先沟通", base)])
        db.commit()
        allowed_id, denied_job_id, denied_owner = str(allowed.id), str(denied_job.id), str(denied_owner_id)

    with TestClient(app) as client:
        login(client, "scoped-list@example.test")
        unfiltered = client.get("/api/v1/candidates")
        by_job = client.get("/api/v1/candidates", params={"job_id": denied_job_id})
        by_owner = client.get("/api/v1/candidates", params={"owner_id": denied_owner})
        by_score = client.get("/api/v1/candidates", params={"min_score": 90})

    assert unfiltered.status_code == by_job.status_code == by_owner.status_code == by_score.status_code == 200
    assert unfiltered.json()["data"][0]["application"]["id"] == allowed_id
    assert unfiltered.json()["meta"]["owners"] == [{"id": str(recruiter_id), "name": "recruiter"}]
    assert by_job.json()["data"] == []
    assert by_owner.json()["data"] == []
    assert by_owner.json()["meta"]["owners"] == [{"id": str(recruiter_id), "name": "recruiter"}]
    assert by_score.json()["data"] == []


def test_candidate_list_min_score_excludes_missing_results_and_validates_range(tmp_path) -> None:
    app = make_app(tmp_path)
    admin_id = seed_user(app, "recruiting_admin", "score-list@example.test")
    with app.state.identity_store.sync_session() as db:
        admin = db.get(User, admin_id)
        job = Job(organization_id=admin.organization_id, title="Scored Job", owner_id=admin_id)
        scored = Candidate(organization_id=admin.organization_id, display_name="Scored")
        missing = Candidate(organization_id=admin.organization_id, display_name="Missing")
        scored_file = FileObject(organization_id=admin.organization_id, storage_key="private/scored", original_filename="scored.pdf", mime_type="application/pdf", size_bytes=1, sha256="e" * 64, uploaded_by=admin_id)
        missing_file = FileObject(organization_id=admin.organization_id, storage_key="private/missing", original_filename="missing.pdf", mime_type="application/pdf", size_bytes=1, sha256="f" * 64, uploaded_by=admin_id)
        db.add_all([job, scored, missing, scored_file, missing_file]); db.flush()
        scored_resume = Resume(organization_id=admin.organization_id, candidate_id=scored.id, file_object_id=scored_file.id, version_number=1)
        missing_resume = Resume(organization_id=admin.organization_id, candidate_id=missing.id, file_object_id=missing_file.id, version_number=1)
        db.add_all([scored_resume, missing_resume]); db.flush()
        scored_app = Application(organization_id=admin.organization_id, candidate_id=scored.id, job_id=job.id, resume_id=scored_resume.id, owner_id=admin_id)
        missing_app = Application(organization_id=admin.organization_id, candidate_id=missing.id, job_id=job.id, resume_id=missing_resume.id, owner_id=admin_id, stage="rejected")
        db.add_all([scored_app, missing_app]); db.flush()
        _,results=seed_screening_results(db, scored_app, scored_file.id, admin_id, [("scored", 0, "暂缓", datetime(2026, 3, 1, tzinfo=timezone.utc))])
        seed_llm_evaluation(db,scored_app,admin_id,results[-1],65,"建议评审",datetime(2026,3,1,0,1,tzinfo=timezone.utc))
        db.commit()
        scored_id, missing_id = str(scored.id), str(missing.id)

    with TestClient(app) as client:
        login(client, "score-list@example.test")
        unfiltered = client.get("/api/v1/candidates")
        minimum = client.get("/api/v1/candidates", params={"min_score": 0})
        llm_minimum = client.get("/api/v1/candidates", params={"min_score": 60})
        below = client.get("/api/v1/candidates", params={"min_score": -1})
        above = client.get("/api/v1/candidates", params={"min_score": 101})

    rows = {row["id"]: row for row in unfiltered.json()["data"]}
    assert rows[missing_id]["application"]["rule_score"] is None
    assert rows[missing_id]["application"]["recommendation"] is None
    assert [row["id"] for row in minimum.json()["data"]] == [scored_id]
    assert [row["id"] for row in llm_minimum.json()["data"]] == [scored_id]
    assert llm_minimum.json()["data"][0]["application"]["rule_score"]==0
    assert llm_minimum.json()["data"][0]["application"]["ai_score"]==65
    assert below.status_code == above.status_code == 422
    assert below.json()["code"] == above.json()["code"] == "validation_failed"


def test_candidate_list_and_detail_project_safe_final_ai_failure(tmp_path) -> None:
    app=make_app(tmp_path); admin_id=seed_user(app,"recruiting_admin","failed-list@example.test")
    with app.state.identity_store.sync_session() as db:
        admin=db.get(User,admin_id); job=Job(organization_id=admin.organization_id,title="Failed AI",owner_id=admin_id)
        candidate=Candidate(organization_id=admin.organization_id,display_name="Failed AI Candidate")
        file=FileObject(organization_id=admin.organization_id,storage_key="private/failed-ai",original_filename="failed.pdf",mime_type="application/pdf",size_bytes=1,sha256="9"*64,uploaded_by=admin_id)
        db.add_all([job,candidate,file]); db.flush(); resume=Resume(organization_id=admin.organization_id,candidate_id=candidate.id,file_object_id=file.id,version_number=1); db.add(resume); db.flush()
        application=Application(organization_id=admin.organization_id,candidate_id=candidate.id,job_id=job.id,resume_id=resume.id,owner_id=admin_id,stage="contact"); db.add(application); db.flush()
        item,results=seed_screening_results(db,application,file.id,admin_id,[("legacy",99,"优先沟通",datetime(2026,4,1,tzinfo=timezone.utc))])
        item.llm_status="failed"; item.llm_safe_error_code="candidate_alice_provider_body"; item.llm_attempts=3
        seed_llm_evaluation(db,application,admin_id,results[-1],65,"建议评审",datetime(2026,4,1,0,1,tzinfo=timezone.utc)); item.llm_status="failed"; item.llm_safe_error_code="candidate_alice_provider_body"
        seed_terminal_route_audit(db,application,item,admin_id,ai_status="failed",score=None,safe_error_code="internal_error"); db.commit(); candidate_id=str(candidate.id)
    with TestClient(app) as client:
        login(client,"failed-list@example.test")
        listing=client.get("/api/v1/candidates")
        history=client.get(f"/api/v1/candidates/{candidate_id}/applications")
    for projection in (listing.json()["data"][0]["application"],history.json()["data"][0]):
        assert projection["route_result"]=="review"
        assert projection["ai_score"] is None
        assert projection["ai_recommendation"]=="AI评分不可用"
        assert projection["llm_evaluation"] is None
        assert projection["llm_error_code"]=="internal_error"
    assert "candidate_alice_provider_body" not in listing.text+history.text

def test_candidate_min_score_excludes_stale_evaluation_after_final_ai_failure(tmp_path) -> None:
    app=make_app(tmp_path); admin_id=seed_user(app,"recruiting_admin","failed-filter@example.test")
    with app.state.identity_store.sync_session() as db:
        admin=db.get(User,admin_id); job=Job(organization_id=admin.organization_id,title="Failed filter",owner_id=admin_id)
        candidate=Candidate(organization_id=admin.organization_id,display_name="Failed filter candidate")
        file=FileObject(organization_id=admin.organization_id,storage_key="private/failed-filter",original_filename="failed-filter.pdf",mime_type="application/pdf",size_bytes=1,sha256="8"*64,uploaded_by=admin_id)
        db.add_all([job,candidate,file]); db.flush(); resume=Resume(organization_id=admin.organization_id,candidate_id=candidate.id,file_object_id=file.id,version_number=1); db.add(resume); db.flush()
        application=Application(organization_id=admin.organization_id,candidate_id=candidate.id,job_id=job.id,resume_id=resume.id,owner_id=admin_id,stage="review"); db.add(application); db.flush()
        item,results=seed_screening_results(db,application,file.id,admin_id,[("failed-filter",50,"暂缓",datetime(2026,4,3,tzinfo=timezone.utc))])
        seed_llm_evaluation(db,application,admin_id,results[-1],65,"建议评审",datetime(2026,4,3,0,1,tzinfo=timezone.utc)); item.llm_status="failed"; item.llm_safe_error_code="provider_unavailable"
        seed_terminal_route_audit(db,application,item,admin_id,ai_status="failed",score=None,safe_error_code="provider_unavailable",created_at=datetime(2026,4,3,0,2,tzinfo=timezone.utc)); db.commit()
    with TestClient(app) as client:
        login(client,"failed-filter@example.test")
        unfiltered=client.get("/api/v1/candidates")
        filtered=client.get("/api/v1/candidates",params={"min_score":60})
    assert unfiltered.json()["data"][0]["application"]["ai_score"] is None
    assert filtered.status_code==200
    assert filtered.json()["data"]==[]

def test_candidate_projection_does_not_infer_route_without_valid_terminal_audit(tmp_path) -> None:
    app=make_app(tmp_path); admin_id=seed_user(app,"recruiting_admin","invalid-route@example.test")
    with app.state.identity_store.sync_session() as db:
        admin=db.get(User,admin_id); job=Job(organization_id=admin.organization_id,title="Invalid route",owner_id=admin_id); db.add(job); db.flush()
        candidate_ids=[]
        for index in range(3):
            candidate=Candidate(organization_id=admin.organization_id,display_name=f"Invalid route {index}")
            file=FileObject(organization_id=admin.organization_id,storage_key=f"private/invalid-route-{index}",original_filename=f"{index}.pdf",mime_type="application/pdf",size_bytes=1,sha256=str(index)*64,uploaded_by=admin_id)
            db.add_all([candidate,file]); db.flush(); resume=Resume(organization_id=admin.organization_id,candidate_id=candidate.id,file_object_id=file.id,version_number=1); db.add(resume); db.flush()
            application=Application(organization_id=admin.organization_id,candidate_id=candidate.id,job_id=job.id,resume_id=resume.id,owner_id=admin_id,stage="review"); db.add(application); db.flush()
            item,results=seed_screening_results(db,application,file.id,admin_id,[(f"invalid-{index}",70,"可沟通",datetime(2026,4,2,tzinfo=timezone.utc))])
            seed_llm_evaluation(db,application,admin_id,results[-1],70,"建议评审",datetime(2026,4,2,0,1,tzinfo=timezone.utc))
            if index:
                audit=seed_terminal_route_audit(db,application,item,admin_id)
                audit.metadata_json={**audit.metadata_json,**({"to_stage":"contact"} if index==1 else {"ai_status":"pending"})}
            candidate_ids.append(str(candidate.id))
        db.commit()
    with TestClient(app) as client:
        login(client,"invalid-route@example.test")
        response=client.get("/api/v1/candidates")
    assert response.status_code==200
    for row in response.json()["data"]:
        assert row["id"] in candidate_ids
        assert row["application"]["route_result"] is None
        assert row["application"]["ai_score"]==70
        assert row["application"]["llm_evaluation"]["score"]==70


def test_candidate_list_searches_profile_and_contact_fields(tmp_path) -> None:
    app = make_app(tmp_path)
    seed_user(app, "recruiting_admin", "search-list@example.test")
    with TestClient(app) as client:
        headers = login(client, "search-list@example.test")
        created = {
            "name": client.post("/api/v1/candidates", json={"display_name": "Name Match"}, headers=headers).json()["data"]["id"],
            "title": client.post("/api/v1/candidates", json={"display_name": "Title Candidate", "current_title": "Distributed Systems Lead"}, headers=headers).json()["data"]["id"],
            "email": client.post("/api/v1/candidates", json={"display_name": "Email Candidate", "contacts": [{"kind": "email", "value": "person@example.com"}]}, headers=headers).json()["data"]["id"],
            "phone": client.post("/api/v1/candidates", json={"display_name": "Phone Candidate", "contacts": [{"kind": "phone", "value": "+86 138 0000 2468"}]}, headers=headers).json()["data"]["id"],
        }

        queries = {
            "name": "Name Match",
            "title": "Distributed Systems",
            "email": "person@example.com",
            "phone": "+86 138 0000 2468",
        }
        results = {
            field: [row["id"] for row in client.get("/api/v1/candidates", params={"q": query}).json()["data"]]
            for field, query in queries.items()
        }

    assert results == {field: [candidate_id] for field, candidate_id in created.items()}


def test_candidate_list_cursor_uses_selected_application_time_and_candidate_id_tiebreaker(tmp_path) -> None:
    app = make_app(tmp_path)
    admin_id = seed_user(app, "recruiting_admin", "cursor-list@example.test")
    base = datetime(2026, 4, 1, tzinfo=timezone.utc)
    with app.state.identity_store.sync_session() as db:
        admin = db.get(User, admin_id)
        job = Job(organization_id=admin.organization_id, title="Cursor Job", owner_id=admin_id)
        db.add(job); db.flush()
        expected_rows = []
        transition_target = None
        application_times = [base + timedelta(hours=2), base + timedelta(hours=1), base + timedelta(hours=1), base]
        for index, application_time in enumerate(application_times):
            candidate = Candidate(organization_id=admin.organization_id, display_name=f"Candidate {index}", updated_at=base + timedelta(days=index))
            file = FileObject(organization_id=admin.organization_id, storage_key=f"private/cursor-{index}", original_filename=f"{index}.pdf", mime_type="application/pdf", size_bytes=1, sha256=str(index) * 64, uploaded_by=admin_id)
            db.add_all([candidate, file]); db.flush()
            resume = Resume(organization_id=admin.organization_id, candidate_id=candidate.id, file_object_id=file.id, version_number=1)
            db.add(resume); db.flush()
            application = Application(organization_id=admin.organization_id, candidate_id=candidate.id, job_id=job.id, resume_id=resume.id, owner_id=admin_id, stage="review" if index == 3 else "new", updated_at=application_time)
            db.add(application); db.flush()
            expected_rows.append((application_time, candidate.id, application.id))
            if index == 3:
                transition_target = (str(candidate.id), str(application.id))
        db.commit()

    expected_rows.sort(key=lambda row: (row[0], row[1]), reverse=True)

    with TestClient(app) as client:
        headers = login(client, "cursor-list@example.test")
        first = client.get("/api/v1/candidates", params={"limit": 2})
        second = client.get("/api/v1/candidates", params={"limit": 2, "cursor": first.json()["meta"]["next_cursor"]})
        third = client.get("/api/v1/candidates", params={"limit": 2, "cursor": second.json()["meta"]["next_cursor"]}) if second.json()["meta"]["next_cursor"] else None
        transitioned = client.post(
            f"/api/v1/applications/{transition_target[1]}/workflow-actions",
            json={"action": "review_approved"},
            headers={**headers, "If-Match": '"1"', "Idempotency-Key": "candidate-list-reorder"},
        )
        refreshed = client.get("/api/v1/candidates", params={"limit": 2})

    responses = [first, second] + ([third] if third is not None else [])
    assert all(response.status_code == 200 for response in responses)
    rows = [row for response in responses for row in response.json()["data"]]
    assert [row["id"] for row in rows] == [str(candidate_id) for _, candidate_id, _ in expected_rows]
    assert [row["application"]["id"] for row in rows] == [str(application_id) for _, _, application_id in expected_rows]
    assert first.json()["meta"]["next_cursor"] is not None
    assert responses[-1].json()["meta"]["next_cursor"] is None
    assert transitioned.status_code == refreshed.status_code == 200
    assert refreshed.json()["data"][0]["id"] == transition_target[0]
    assert refreshed.json()["data"][0]["application"]["id"] == transition_target[1]
    listed_updated_at = datetime.fromisoformat(refreshed.json()["data"][0]["application"]["updated_at"]).replace(tzinfo=timezone.utc)
    transitioned_updated_at = datetime.fromisoformat(transitioned.json()["data"]["updated_at"])
    assert listed_updated_at == transitioned_updated_at


@pytest.mark.parametrize(("publish", "expected_status"), [(False, "draft"), (True, "open")])
def test_create_job_definition_atomically_creates_typed_versions(tmp_path, publish, expected_status) -> None:
    app = make_app(tmp_path)
    seed_user(app, "recruiting_admin", "admin@example.test")
    payload = job_definition_payload(publish=publish)
    with TestClient(app) as client:
        headers = {**login(client, "admin@example.test"), "Idempotency-Key": f"create-{expected_status}"}
        response = client.post("/api/v1/job-definitions", json=payload, headers=headers)
    assert response.status_code == 201
    assert response.headers["etag"] == '"1"'
    data = response.json()["data"]
    assert data["job"]["status"] == expected_status
    assert data["jd"] == {"id": data["jd"]["id"], "version_number": 1, "description": payload["description"], "location": payload["location"], "process_template": payload["process_template"], "workflow_template_id": None, "llm_enabled": payload["llm_enabled"]}
    assert data["rules"] == {"id": data["rules"]["id"], "version_number": 1, "must_have": payload["must_have"], "nice_to_have": payload["nice_to_have"]}
    with app.state.identity_store.sync_session() as db:
        assert db.query(Job).count() == db.query(JobJdVersion).count() == db.query(ScreeningRuleVersion).count() == 1


def test_job_owner_options_and_definition_writes_enforce_active_tenant_hiring_managers(tmp_path) -> None:
    app = make_app(tmp_path)
    admin_id = seed_user(app, "recruiting_admin", "admin@example.test")
    with app.state.identity_store.sync_session() as db:
        admin = db.get(User, admin_id)

        def add_user(email, display_name, *, organization=None, status=UserStatus.ACTIVE, roles=("hiring_manager",)):
            user = User(
                organization=organization or admin.organization,
                email=email,
                normalized_email=email,
                display_name=display_name,
                password_hash=PasswordService().hash("correct horse"),
                status=status,
            )
            user.roles.extend(UserRole(role=role) for role in roles)
            db.add(user)
            db.flush()
            return user.id

        eligible_id = add_user("eligible@example.test", "可选负责人")
        replacement_id = add_user("replacement@example.test", "第二负责人")
        disabled_id = add_user("disabled@example.test", "停用负责人", status=UserStatus.DISABLED)
        wrong_role_id = add_user("recruiter@example.test", "招聘专员", roles=("recruiter",))
        other_organization = Organization(slug="other", name="Other", status="active")
        db.add(other_organization)
        db.flush()
        cross_tenant_id = add_user("other@example.test", "其他组织负责人", organization=other_organization)
        db.commit()

    with TestClient(app) as client:
        headers = login(client, "admin@example.test")
        options = client.get("/api/v1/job-owner-options", headers=headers)
        assert options.status_code == 200
        assert options.json() == {
            "data": [
                {"id": str(admin_id), "name": "recruiting_admin"},
                {"id": str(eligible_id), "name": "可选负责人"},
                {"id": str(replacement_id), "name": "第二负责人"},
            ],
            "meta": {"count": 3},
        }

        invalid_ids = {
            "disabled": disabled_id,
            "wrong-role": wrong_role_id,
            "cross-tenant": cross_tenant_id,
        }
        for key, invalid_id in invalid_ids.items():
            rejected = client.post(
                "/api/v1/job-definitions",
                json=job_definition_payload(hiring_owner_id=str(invalid_id)),
                headers={**headers, "Idempotency-Key": f"invalid-owner-{key}"},
            )
            assert rejected.status_code == 422
            assert rejected.json()["code"] == "hiring_owner_invalid"

        created = client.post(
            "/api/v1/job-definitions",
            json=job_definition_payload(hiring_owner_id=str(admin_id)),
            headers={**headers, "Idempotency-Key": "valid-owner"},
        )
        assert created.status_code == 201
        job_id = UUID(created.json()["data"]["job"]["id"])

        replaced = client.put(
            f"/api/v1/job-definitions/{job_id}",
            json=job_definition_payload(hiring_owner_id=str(replacement_id)),
            headers={**headers, "Idempotency-Key": "replace-owner", "If-Match": '"1"'},
        )
        assert replaced.status_code == 200

    with app.state.identity_store.sync_session() as db:
        managers = db.scalars(select(JobCollaborator).where(
            JobCollaborator.job_id == job_id,
            JobCollaborator.access_role == "job_manager",
        )).all()
        assert [(item.organization_id, item.user_id) for item in managers] == [
            (db.get(User, replacement_id).organization_id, replacement_id),
        ]


def test_get_job_definition_returns_latest_versions_and_legacy_nulls(tmp_path) -> None:
    app = make_app(tmp_path)
    admin_id = seed_user(app, "recruiting_admin", "admin@example.test")
    with app.state.identity_store.sync_session() as db:
        admin = db.get(User, admin_id)
        legacy = Job(organization_id=admin.organization_id, title="Legacy", owner_id=admin.id)
        db.add(legacy); db.commit(); legacy_id = str(legacy.id)
    with TestClient(app) as client:
        headers = login(client, "admin@example.test")
        created = client.post("/api/v1/job-definitions", json=job_definition_payload(), headers={**headers, "Idempotency-Key": "latest-definition"})
        job_id = created.json()["data"]["job"]["id"]
        updated = client.put(f"/api/v1/job-definitions/{job_id}", json=job_definition_payload(description="Latest description", must_have=["Python 3.12"]), headers={**headers, "Idempotency-Key": "replace-definition", "If-Match": '"1"'})
        latest = client.get(f"/api/v1/job-definitions/{job_id}")
        legacy_response = client.get(f"/api/v1/job-definitions/{legacy_id}")
    assert updated.status_code == latest.status_code == legacy_response.status_code == 200
    assert latest.json()["data"]["jd"]["version_number"] == 2
    assert latest.json()["data"]["jd"]["description"] == "Latest description"
    assert latest.json()["data"]["rules"]["version_number"] == 2
    assert legacy_response.json()["data"]["jd"] is None
    assert legacy_response.json()["data"]["rules"] is None


def test_replace_job_definition_appends_history_and_obeys_publish_state_rules(tmp_path) -> None:
    app = make_app(tmp_path)
    seed_user(app, "recruiting_admin", "admin@example.test")
    with TestClient(app) as client:
        headers = login(client, "admin@example.test")
        created = client.post("/api/v1/job-definitions", json=job_definition_payload(), headers={**headers, "Idempotency-Key": "create"})
        job_id = created.json()["data"]["job"]["id"]
        replaced = client.put(f"/api/v1/job-definitions/{job_id}", json=job_definition_payload(title="Principal Engineer", publish=True), headers={**headers, "Idempotency-Key": "replace", "If-Match": '"1"'})
        stale = client.put(f"/api/v1/job-definitions/{job_id}", json=job_definition_payload(title="Stale"), headers={**headers, "Idempotency-Key": "stale", "If-Match": '"1"'})
        invalid_publish = client.put(f"/api/v1/job-definitions/{job_id}", json=job_definition_payload(title="Still open", publish=True), headers={**headers, "Idempotency-Key": "publish-again", "If-Match": '"2"'})
        preserved = client.put(f"/api/v1/job-definitions/{job_id}", json=job_definition_payload(title="Open replacement"), headers={**headers, "Idempotency-Key": "preserve-open", "If-Match": '"2"'})
        replay = client.put(f"/api/v1/job-definitions/{job_id}", json=job_definition_payload(title="Open replacement"), headers={**headers, "Idempotency-Key": "preserve-open", "If-Match": '"2"'})
        replay_conflict = client.put(f"/api/v1/job-definitions/{job_id}", json=job_definition_payload(title="Different replacement"), headers={**headers, "Idempotency-Key": "preserve-open", "If-Match": '"3"'})
    assert replaced.status_code == 200 and replaced.headers["etag"] == '"2"'
    assert replaced.json()["data"]["job"]["status"] == "open"
    assert replaced.json()["data"]["jd"]["version_number"] == replaced.json()["data"]["rules"]["version_number"] == 2
    assert stale.status_code == 409 and stale.json()["code"] == "resource_version_conflict"
    assert invalid_publish.status_code == 409 and invalid_publish.json()["code"] == "invalid_state_transition"
    assert preserved.status_code == replay.status_code == 200
    assert preserved.json() == replay.json()
    assert preserved.headers["etag"] == replay.headers["etag"] == '"3"'
    assert preserved.json()["data"]["job"]["status"] == "open"
    assert replay_conflict.status_code == 409 and replay_conflict.json()["code"] == "idempotency_conflict"
    with app.state.identity_store.sync_session() as db:
        assert [row.version_number for row in db.query(JobJdVersion).order_by(JobJdVersion.version_number)] == [1, 2, 3]
        assert [row.version_number for row in db.query(ScreeningRuleVersion).order_by(ScreeningRuleVersion.version_number)] == [1, 2, 3]


def test_job_definition_idempotency_replays_and_rejects_body_conflicts(tmp_path) -> None:
    app = make_app(tmp_path)
    seed_user(app, "recruiting_admin", "admin@example.test")
    with TestClient(app) as client:
        headers = {**login(client, "admin@example.test"), "Idempotency-Key": "same-create"}
        first = client.post("/api/v1/job-definitions", json=job_definition_payload(), headers=headers)
        replay = client.post("/api/v1/job-definitions", json=job_definition_payload(), headers=headers)
        conflict = client.post("/api/v1/job-definitions", json=job_definition_payload(title="Different"), headers=headers)
    assert first.status_code == replay.status_code == 201
    assert first.json() == replay.json()
    assert first.headers["etag"] == replay.headers["etag"] == '"1"'
    assert conflict.status_code == 409 and conflict.json()["code"] == "idempotency_conflict"
    with app.state.identity_store.sync_session() as db:
        assert db.query(Job).count() == db.query(IdempotencyRecord).count() == 1


def test_job_definition_failure_rolls_back_aggregate_and_idempotency(tmp_path) -> None:
    app = make_app(tmp_path)
    seed_user(app, "recruiting_admin", "admin@example.test")
    def fail_rule_write(mapper, connection, target):
        raise RuntimeError("injected rule write failure")
    event.listen(ScreeningRuleVersion, "before_insert", fail_rule_write)
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.post("/api/v1/job-definitions", json=job_definition_payload(), headers={**login(client, "admin@example.test"), "Idempotency-Key": "will-rollback"})
    finally:
        event.remove(ScreeningRuleVersion, "before_insert", fail_rule_write)
    assert response.status_code == 500
    with app.state.identity_store.sync_session() as db:
        assert db.query(Job).count() == 0
        assert db.query(JobJdVersion).count() == 0
        assert db.query(ScreeningRuleVersion).count() == 0
        assert db.query(IdempotencyRecord).count() == 0


def test_job_definition_access_is_non_disclosing_and_audit_metadata_is_redacted(tmp_path) -> None:
    app = make_app(tmp_path)
    seed_user(app, "recruiting_admin", "admin@example.test")
    seed_user(app, "system_admin", "system@example.test")
    with app.state.identity_store.sync_session() as db:
        other_org = Organization(slug="other", name="Other", status="active")
        other_user = User(organization=other_org, email="other@example.test", normalized_email="other@example.test", display_name="other", password_hash=PasswordService().hash("correct horse"))
        other_user.roles.append(UserRole(role="recruiting_admin"))
        db.add(other_user); db.flush()
        cross_tenant_job = Job(organization_id=other_org.id, title="Other tenant", owner_id=other_user.id)
        db.add(cross_tenant_job); db.commit(); cross_tenant_job_id = str(cross_tenant_job.id)
    secret_description = "secret JD content marker"
    secret_rule = "secret rule content marker"
    with TestClient(app) as client:
        admin_headers = login(client, "admin@example.test")
        denied_post = client.post("/api/v1/job-definitions", json=job_definition_payload(), headers={**login(client, "system@example.test"), "Idempotency-Key": "denied-post"})
        admin_headers = login(client, "admin@example.test")
        created = client.post("/api/v1/job-definitions", json=job_definition_payload(description=secret_description, must_have=[secret_rule]), headers={**admin_headers, "Idempotency-Key": "secure-create"})
        job_id = created.json()["data"]["job"]["id"]
        cross_tenant = client.get(f"/api/v1/job-definitions/{cross_tenant_job_id}")
        cross_tenant_put = client.put(f"/api/v1/job-definitions/{cross_tenant_job_id}", json=job_definition_payload(), headers={**admin_headers, "If-Match": '"1"', "Idempotency-Key": "cross-tenant-put"})
        updated = client.put(f"/api/v1/job-definitions/{job_id}", json=job_definition_payload(description="updated secret JD", must_have=["updated secret rule"]), headers={**admin_headers, "If-Match": '"1"', "Idempotency-Key": "secure-update"})
        system_headers = login(client, "system@example.test")
        denied_get = client.get(f"/api/v1/job-definitions/{job_id}")
        denied_put = client.put(f"/api/v1/job-definitions/{job_id}", json=job_definition_payload(), headers={**system_headers, "If-Match": '"1"', "Idempotency-Key": "denied"})
    assert denied_post.status_code == denied_get.status_code == denied_put.status_code == 404
    assert denied_get.json()["code"] == denied_put.json()["code"] == "resource_not_found"
    assert cross_tenant.status_code == cross_tenant_put.status_code == 404
    assert cross_tenant.json()["code"] == cross_tenant_put.json()["code"] == "resource_not_found"
    assert updated.status_code == 200
    with app.state.identity_store.sync_session() as db:
        audit_text = repr([row.metadata_json for row in db.query(AuditLog).filter(AuditLog.event_type.like("job.%"))])
        assert secret_description not in audit_text
        assert secret_rule not in audit_text
        assert "updated secret JD" not in audit_text
        assert "updated secret rule" not in audit_text


def test_job_definition_preconditions_and_bounded_validation_are_enforced(tmp_path) -> None:
    app = make_app(tmp_path)
    seed_user(app, "recruiting_admin", "admin@example.test")
    with TestClient(app) as client:
        headers = login(client, "admin@example.test")
        missing_key = client.post("/api/v1/job-definitions", json=job_definition_payload(), headers=headers)
        blank_description = client.post("/api/v1/job-definitions", json=job_definition_payload(description="   "), headers={**headers, "Idempotency-Key": "blank-description"})
        blank_rule = client.post("/api/v1/job-definitions", json=job_definition_payload(must_have=["Python", " "]), headers={**headers, "Idempotency-Key": "blank-rule"})
        invalid_priority = client.post("/api/v1/job-definitions", json=job_definition_payload(priority="urgent"), headers={**headers, "Idempotency-Key": "bad-priority"})
        created = client.post("/api/v1/job-definitions", json=job_definition_payload(), headers={**headers, "Idempotency-Key": "valid"})
        job_id = created.json()["data"]["job"]["id"]
        missing_match = client.put(f"/api/v1/job-definitions/{job_id}", json=job_definition_payload(), headers={**headers, "Idempotency-Key": "missing-match"})
        malformed_match = client.put(f"/api/v1/job-definitions/{job_id}", json=job_definition_payload(), headers={**headers, "Idempotency-Key": "malformed-match", "If-Match": "1"})
    assert missing_key.status_code == 428 and missing_key.json()["code"] == "idempotency_key_required"
    assert blank_description.status_code == blank_rule.status_code == invalid_priority.status_code == 422
    assert missing_match.status_code == 428 and missing_match.json()["code"] == "precondition_required"
    assert malformed_match.status_code == 422 and malformed_match.json()["code"] == "validation_failed"


def test_job_definition_rejects_unknown_fields_and_all_size_boundaries(tmp_path) -> None:
    app = make_app(tmp_path)
    seed_user(app, "recruiting_admin", "admin@example.test")
    invalid_payloads = [
        job_definition_payload(unknown="field"),
        job_definition_payload(title="x" * 201),
        job_definition_payload(description="x" * 50_001),
        job_definition_payload(location="x" * 201),
        job_definition_payload(process_template="x" * 101),
        job_definition_payload(must_have=["x" * 101]),
        job_definition_payload(nice_to_have=["x"] * 51),
        job_definition_payload(headcount=0),
        job_definition_payload(headcount=1001),
    ]
    valid_boundary = job_definition_payload(title="x" * 200, description="x" * 50_000, location="x" * 200, process_template="x" * 100, must_have=["x" * 100], nice_to_have=["x"] * 50, headcount=1000)
    with TestClient(app) as client:
        headers = login(client, "admin@example.test")
        rejected = [client.post("/api/v1/job-definitions", json=payload, headers={**headers, "Idempotency-Key": f"invalid-{index}"}) for index, payload in enumerate(invalid_payloads)]
        accepted = client.post("/api/v1/job-definitions", json=valid_boundary, headers={**headers, "Idempotency-Key": "valid-boundary"})
    assert all(response.status_code == 422 and response.json()["code"] == "validation_failed" for response in rejected)
    assert accepted.status_code == 201


def test_job_definition_content_with_injected_version_identity_fails_closed(tmp_path) -> None:
    app = make_app(tmp_path)
    admin_id = seed_user(app, "recruiting_admin", "admin@example.test")
    with app.state.identity_store.sync_session() as db:
        admin = db.get(User, admin_id)
        job = Job(organization_id=admin.organization_id, title="Legacy typed", owner_id=admin.id)
        db.add(job); db.flush()
        jd = JobJdVersion(organization_id=admin.organization_id, job_id=job.id, version_number=1, created_by=admin.id, content={"id": "attacker-jd", "version_number": 999, "description": "Safe", "location": "Remote", "process_template": "standard", "llm_enabled": False})
        rules = ScreeningRuleVersion(organization_id=admin.organization_id, job_id=job.id, version_number=1, created_by=admin.id, content={"id": "attacker-rules", "version_number": 999, "must_have": [], "nice_to_have": []})
        db.add_all([jd, rules]); db.commit(); job_id = str(job.id)
    with TestClient(app) as client:
        login(client, "admin@example.test")
        response = client.get(f"/api/v1/job-definitions/{job_id}")
    assert response.status_code == 409
    assert response.json()["code"] == "job_definition_incompatible"
    assert "attacker-jd" not in response.text and "attacker-rules" not in response.text


def test_job_definition_normalizes_text_only_legacy_content(tmp_path) -> None:
    app = make_app(tmp_path)
    admin_id = seed_user(app, "recruiting_admin", "admin@example.test")
    with app.state.identity_store.sync_session() as db:
        admin = db.get(User, admin_id)
        job = Job(organization_id=admin.organization_id, title="Legacy text", owner_id=admin.id)
        db.add(job); db.flush()
        jd = JobJdVersion(organization_id=admin.organization_id, job_id=job.id, version_number=1, created_by=admin.id, content={"text": "old shape"})
        rules = ScreeningRuleVersion(organization_id=admin.organization_id, job_id=job.id, version_number=1, created_by=admin.id, content={"required_terms": [], "bonus_terms": []})
        db.add_all([jd, rules]); db.commit(); job_id, jd_id, rules_id = str(job.id), str(jd.id), str(rules.id)
    with TestClient(app) as client:
        login(client, "admin@example.test")
        response = client.get(f"/api/v1/job-definitions/{job_id}")
    assert response.status_code == 200
    assert response.json()["data"]["jd"] == {
        "id": jd_id,
        "version_number": 1,
        "description": "old shape",
        "location": "",
        "process_template": "默认招聘流程",
        "workflow_template_id": None,
        "llm_enabled": False,
    }
    assert response.json()["data"]["rules"] == {
        "id": rules_id,
        "version_number": 1,
        "must_have": [],
        "nice_to_have": [],
    }
    with app.state.identity_store.sync_session() as db:
        stored_jd = db.get(JobJdVersion, UUID(jd_id))
        stored_rules = db.get(ScreeningRuleVersion, UUID(rules_id))
        assert stored_jd.content == {"text": "old shape"}
        assert stored_rules.content == {"required_terms": [], "bonus_terms": []}
        assert db.scalar(select(func.count()).select_from(JobJdVersion).where(JobJdVersion.job_id == UUID(job_id))) == 1
        assert db.scalar(select(func.count()).select_from(ScreeningRuleVersion).where(ScreeningRuleVersion.job_id == UUID(job_id))) == 1


@pytest.mark.parametrize("legacy_content", [
    {"legacy": "unsupported shape"},
    {"text": "old shape", "unexpected": "unsupported shape"},
    {"text": "old shape", "description": "typed", "location": "", "process_template": "standard", "llm_enabled": False},
])
def test_job_definition_unsupported_legacy_content_returns_stable_problem(tmp_path, legacy_content) -> None:
    app = make_app(tmp_path)
    admin_id = seed_user(app, "recruiting_admin", "admin@example.test")
    with app.state.identity_store.sync_session() as db:
        admin = db.get(User, admin_id)
        job = Job(organization_id=admin.organization_id, title="Incomplete legacy", owner_id=admin.id)
        db.add(job); db.flush()
        db.add(JobJdVersion(organization_id=admin.organization_id, job_id=job.id, version_number=1, created_by=admin.id, content=legacy_content))
        db.commit(); job_id = str(job.id)
    with TestClient(app, raise_server_exceptions=False) as client:
        login(client, "admin@example.test")
        response = client.get(f"/api/v1/job-definitions/{job_id}")
    assert response.status_code == 409
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["code"] == "job_definition_incompatible"
    assert "unsupported shape" not in response.text
    assert "old shape" not in response.text


def test_job_definition_non_object_legacy_rules_return_stable_problem(tmp_path) -> None:
    app = make_app(tmp_path)
    admin_id = seed_user(app, "recruiting_admin", "admin@example.test")
    with app.state.identity_store.sync_session() as db:
        admin = db.get(User, admin_id)
        job = Job(organization_id=admin.organization_id, title="Malformed legacy rules", owner_id=admin.id)
        db.add(job); db.flush()
        jd = JobJdVersion(organization_id=admin.organization_id, job_id=job.id, version_number=1, created_by=admin.id, content={"description": "Safe", "location": "", "process_template": "standard", "llm_enabled": False})
        rules = ScreeningRuleVersion(organization_id=admin.organization_id, job_id=job.id, version_number=1, created_by=admin.id, content=[])
        db.add_all([jd, rules]); db.commit(); job_id = str(job.id)
    with TestClient(app, raise_server_exceptions=False) as client:
        login(client, "admin@example.test")
        response = client.get(f"/api/v1/job-definitions/{job_id}")
    assert response.status_code == 409
    assert response.json()["code"] == "job_definition_incompatible"


def test_replace_job_definition_failure_rolls_back_every_change(tmp_path) -> None:
    app = make_app(tmp_path)
    seed_user(app, "recruiting_admin", "admin@example.test")
    with TestClient(app) as client:
        headers = login(client, "admin@example.test")
        created = client.post("/api/v1/job-definitions", json=job_definition_payload(), headers={**headers, "Idempotency-Key": "replace-rollback-create"})
        job_id = created.json()["data"]["job"]["id"]

        def fail_rule_write(mapper, connection, target):
            if target.version_number == 2:
                raise RuntimeError("injected replacement rule failure")

        event.listen(ScreeningRuleVersion, "before_insert", fail_rule_write)
        try:
            with TestClient(app, raise_server_exceptions=False) as failing_client:
                failing_headers = login(failing_client, "admin@example.test")
                response = failing_client.put(f"/api/v1/job-definitions/{job_id}", json=job_definition_payload(title="Must rollback", description="rollback JD", must_have=["rollback rule"], publish=True), headers={**failing_headers, "If-Match": '"1"', "Idempotency-Key": "replace-rollback"})
        finally:
            event.remove(ScreeningRuleVersion, "before_insert", fail_rule_write)

    assert response.status_code == 500
    with app.state.identity_store.sync_session() as db:
        job = db.get(Job, UUID(job_id))
        assert (job.title, job.status, job.version) == ("Platform Engineer", "draft", 1)
        assert db.query(JobJdVersion).filter_by(job_id=job.id).count() == 1
        assert db.query(ScreeningRuleVersion).filter_by(job_id=job.id).count() == 1
        assert db.query(IdempotencyRecord).filter_by(idempotency_key="replace-rollback").count() == 0
        assert db.query(AuditLog).filter_by(event_type="job.definition_replaced").count() == 0


def test_published_creation_audit_records_created_open_fact_without_fake_transition(tmp_path) -> None:
    app = make_app(tmp_path)
    seed_user(app, "recruiting_admin", "admin@example.test")
    with TestClient(app) as client:
        response = client.post("/api/v1/job-definitions", json=job_definition_payload(publish=True), headers={**login(client, "admin@example.test"), "Idempotency-Key": "created-open-audit"})
    assert response.status_code == 201
    with app.state.identity_store.sync_session() as db:
        created = db.query(AuditLog).filter_by(event_type="job.definition_created").one()
        assert created.metadata_json["status"] == "open"
        assert db.query(AuditLog).filter_by(event_type="job.published").count() == 0
