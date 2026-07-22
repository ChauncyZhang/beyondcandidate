from datetime import datetime, timedelta, timezone
from tempfile import SpooledTemporaryFile
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from server.app.core.settings import Settings
from server.app.identity.models import AuditLog, Job, JobCollaborator, Organization, User, UserRole, UserStatus
from server.app.identity.security import PasswordService
from server.app.interviews.models import (
    Interview,
    InterviewEvent,
    InterviewFeedback,
    InterviewFeedbackRevision,
    InterviewParticipant,
)
from server.app.main import create_app
from server.app.recruiting.models import Application, Candidate, FileObject, JobJdVersion, Resume
from server.app.recruiting.storage import MAX_PREVIEW_BYTES
from server.app.screening.models import ScreeningResult
from server.tests.test_recruiting_api import login, seed_screening_results, seed_user


class Probe:
    async def check(self) -> None:
        pass


def make_app(tmp_path):
    app = create_app(
        settings=Settings(
            environment="test",
            database_url=f"sqlite+aiosqlite:///{tmp_path / 'interview-api.db'}",
            cors_origins=["https://hr.example.test"],
        ),
        database_probe=Probe(),
        storage_probe=Probe(),
        initialize_identity_schema=True,
    )
    app.state.identity_store.create_schema()
    return app


def test_interview_openapi_registers_the_phase_4_contract(tmp_path) -> None:
    app = make_app(tmp_path)
    with TestClient(app) as client:
        schema = client.get("/openapi.json").json()

    expected = {
        "/api/v1/applications/{application_id}/interview-participant-options": {"get"},
        "/api/v1/interview-conflicts": {"post"},
        "/api/v1/interview-availability": {"get"},
        "/api/v1/interviews": {"get", "post"},
        "/api/v1/interviews/{interview_id}": {"get", "patch"},
        "/api/v1/interviews/{interview_id}/conflicts": {"post"},
        "/api/v1/interviews/{interview_id}/transitions": {"post"},
        "/api/v1/interviews/{interview_id}/calendar-file": {"get"},
        "/api/v1/interviews/{interview_id}/feedbacks": {"get"},
        "/api/v1/interviews/{interview_id}/materials": {"get"},
        "/api/v1/interviews/{interview_id}/resume-file": {"get"},
        "/api/v1/interviews/{interview_id}/my-feedback": {"get", "put"},
        "/api/v1/interviews/{interview_id}/my-feedback/submit": {"post"},
        "/api/v1/interview-feedback/{feedback_id}/amendments": {"post"},
        "/api/v1/me/tasks": {"get"},
    }
    assert {path: set(schema["paths"].get(path, {})) for path in expected} == expected


def test_interview_availability_is_privacy_safe_and_honors_exclude_and_buffer(tmp_path) -> None:
    app = make_app(tmp_path)
    seed = seed_application(app)
    start = datetime(2099, 7, 20, 8, 0, tzinfo=timezone.utc)
    with TestClient(app) as client:
        created, headers = create_interview(client, seed, payload=interview_payload(seed, starts_at=start))
        interview_id = created.json()["data"]["id"]
        params = {
            "from": (start - timedelta(hours=1)).isoformat(),
            "to": (start + timedelta(hours=2)).isoformat(),
            "participant_ids": str(seed["interviewer_id"]),
            "timezone": "Asia/Shanghai",
            "buffer": 15,
        }
        response = client.get("/api/v1/interview-availability", params=params, headers=headers)
        excluded = client.get("/api/v1/interview-availability", params={**params, "exclude": interview_id}, headers=headers)

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert response.json()["data"]["participants"] == [{
        "participant_id": str(seed["interviewer_id"]),
        "status": "confirmed",
        "busy": [{"starts_at": start.isoformat(), "ends_at": (start + timedelta(minutes=45)).isoformat()}],
    }]
    assert response.json()["data"]["buffer_minutes"] == 15
    assert excluded.json()["data"]["participants"][0]["busy"] == []
    assert "candidate" not in response.text.lower()
    assert "round" not in response.text.lower()


def test_interview_availability_rejects_unknown_participants_and_invalid_ranges(tmp_path) -> None:
    app = make_app(tmp_path)
    seed = seed_application(app)
    with TestClient(app) as client:
        headers = login(client, "interview-admin@example.test")
        unknown = client.get("/api/v1/interview-availability", params={
            "from": "2026-07-20T08:00:00Z", "to": "2026-07-20T09:00:00Z",
            "participant_ids": "99999999-9999-4999-8999-999999999999", "timezone": "Asia/Shanghai", "buffer": 15,
        }, headers=headers)
        invalid = client.get("/api/v1/interview-availability", params={
            "from": "2026-07-20T09:00:00Z", "to": "2026-07-20T08:00:00Z",
            "participant_ids": str(seed["interviewer_id"]), "timezone": "Asia/Shanghai", "buffer": 15,
        }, headers=headers)

    assert unknown.status_code == 422
    assert invalid.status_code == 422


def test_interview_availability_provider_failure_is_not_reported_as_free(tmp_path) -> None:
    class FailingProvider:
        def availability(self, **_kwargs):
            raise RuntimeError("calendar provider unavailable")

    app = make_app(tmp_path)
    seed = seed_application(app)
    app.state.interview_availability_provider = FailingProvider()
    with TestClient(app) as client:
        response = client.get("/api/v1/interview-availability", params={
            "from": "2026-07-20T08:00:00Z", "to": "2026-07-20T09:00:00Z",
            "participant_ids": str(seed["interviewer_id"]), "timezone": "Asia/Shanghai", "buffer": 15,
        }, headers=login(client, "interview-admin@example.test"))

    assert response.status_code == 503
    assert response.json()["code"] == "availability_unavailable"
    assert "available" not in response.json()


def test_interview_availability_strips_provider_event_details(tmp_path) -> None:
    class DetailedProvider:
        def availability(self, **kwargs):
            participant_id = kwargs["participant_ids"][0]
            return [{
                "participant_id": str(participant_id),
                "status": "confirmed",
                "display_name": "Secret Person",
                "busy": [{
                    "starts_at": "2026-07-20T08:00:00+00:00",
                    "ends_at": "2026-07-20T09:00:00+00:00",
                    "title": "Candidate interview secret",
                }],
            }]

    app = make_app(tmp_path)
    seed = seed_application(app)
    app.state.interview_availability_provider = DetailedProvider()
    with TestClient(app) as client:
        response = client.get("/api/v1/interview-availability", params={
            "from": "2026-07-20T08:00:00Z", "to": "2026-07-20T10:00:00Z",
            "participant_ids": str(seed["interviewer_id"]), "timezone": "Asia/Shanghai", "buffer": 15,
        }, headers=login(client, "interview-admin@example.test"))

    assert response.status_code == 200
    assert "Secret Person" not in response.text
    assert "Candidate interview secret" not in response.text
    assert response.json()["data"]["participants"][0]["busy"] == [{
        "starts_at": "2026-07-20T08:00:00+00:00",
        "ends_at": "2026-07-20T09:00:00+00:00",
    }]

class InterviewResumeStorage:
    def __init__(self, content: bytes = b"%PDF interview resume") -> None:
        self.content = content
        self.opened = []
        self.last_spool = None

    def open_download(self, storage_key: str, max_bytes: int):
        self.opened.append((storage_key, max_bytes))
        spool = SpooledTemporaryFile(max_size=1024, mode="w+b")
        spool.write(self.content)
        spool.seek(0)
        self.last_spool = spool
        return spool


def test_interview_participant_options_return_only_active_tenant_users_with_eligible_roles(tmp_path) -> None:
    app = make_app(tmp_path)
    seed = seed_application(app)
    system_user_id = seed_user(app, "system_admin", "system-only@example.test")
    with app.state.identity_store.sync_session() as database:
        admin = database.get(User, seed["admin_id"])
        admin.roles.append(UserRole(role="system_admin"))
        database.get(User, seed["other_interviewer_id"]).status = UserStatus.DISABLED
        other_organization = Organization(slug="other", name="Other", status="active")
        other_user = User(
            organization=other_organization,
            email="other-interviewer@example.test",
            normalized_email="other-interviewer@example.test",
            display_name="Other interviewer",
            password_hash="not-used",
        )
        other_user.roles.append(UserRole(role="interviewer"))
        database.add(other_user)
        database.commit()

    with TestClient(app) as client:
        response = client.get(
            f"/api/v1/applications/{seed['application_id']}/interview-participant-options",
            headers=login(client, "interview-admin@example.test"),
        )

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert response.json() == {
        "data": [
            {
                "id": str(seed["interviewer_id"]),
                "display_name": "interviewer",
                "roles": ["interviewer"],
            },
            {
                "id": str(seed["admin_id"]),
                "display_name": "recruiting_admin",
                "roles": ["recruiting_admin"],
            },
        ],
        "meta": {"count": 2},
    }
    assert str(system_user_id) not in response.text
    assert "email" not in response.text
    assert "other-interviewer@example.test" not in response.text


def test_interview_participant_options_require_authentication_and_scheduling_scope(tmp_path) -> None:
    app = make_app(tmp_path)
    seed = seed_application(app)
    path = f"/api/v1/applications/{seed['application_id']}/interview-participant-options"

    with TestClient(app) as client:
        unauthenticated = client.get(path)
        interviewer_headers = login(client, "assigned@example.test")
        unauthorized = client.get(path, headers=interviewer_headers)

    assert unauthenticated.status_code == 401
    assert unauthenticated.json()["code"] == "authentication_required"
    assert unauthorized.status_code == 404
    assert unauthorized.json()["code"] == "resource_not_found"


def seed_application(app):
    admin_id = seed_user(app, "recruiting_admin", "interview-admin@example.test")
    interviewer_id = seed_user(app, "interviewer", "assigned@example.test")
    other_interviewer_id = seed_user(app, "interviewer", "unassigned@example.test")
    with app.state.identity_store.sync_session() as database:
        admin = database.get(User, admin_id)
        job = Job(
            organization_id=admin.organization_id,
            title="AI Engineer",
            owner_id=admin_id,
            status="open",
        )
        candidate = Candidate(
            organization_id=admin.organization_id,
            display_name="李嘉明",
            current_title="AI 算法工程师",
            owner_id=admin_id,
        )
        file_object = FileObject(
            organization_id=admin.organization_id,
            storage_key="interviews/resume.pdf",
            original_filename="李嘉明_简历.pdf",
            mime_type="application/pdf",
            size_bytes=10,
            sha256="a" * 64,
            uploaded_by=admin_id,
        )
        database.add_all([job, candidate, file_object])
        database.flush()
        resume = Resume(
            organization_id=admin.organization_id,
            candidate_id=candidate.id,
            file_object_id=file_object.id,
            version_number=1,
            parsed_text="Python RAG Agent",
        )
        database.add(resume)
        database.flush()
        application = Application(
            organization_id=admin.organization_id,
            candidate_id=candidate.id,
            job_id=job.id,
            resume_id=resume.id,
            owner_id=admin_id,
            stage="interview_pending",
        )
        database.add_all(
            [
                application,
                JobCollaborator(
                    organization_id=admin.organization_id,
                    job_id=job.id,
                    user_id=admin_id,
                    access_role="job_owner",
                ),
            ]
        )
        database.commit()
        return {
            "admin_id": admin_id,
            "interviewer_id": interviewer_id,
            "other_interviewer_id": other_interviewer_id,
            "application_id": application.id,
            "candidate_id": candidate.id,
            "job_id": job.id,
        }


def interview_payload(seed, *, starts_at=None):
    start = starts_at or datetime(2099, 7, 20, 8, 0, tzinfo=timezone.utc)
    return {
        "application_id": str(seed["application_id"]),
        "round_name": "一面",
        "method": "video",
        "timezone": "Asia/Shanghai",
        "starts_at": start.isoformat(),
        "ends_at": (start + timedelta(minutes=45)).isoformat(),
        "meeting_url": "https://meeting.example.test/room",
        "participants": [
            {
                "user_id": str(seed["interviewer_id"]),
                "role": "interviewer",
                "required_feedback": True,
            }
        ],
    }


def create_interview(client, seed, *, key="create-interview", payload=None):
    headers = {
        **login(client, "interview-admin@example.test"),
        "Idempotency-Key": key,
    }
    response = client.post("/api/v1/interviews", json=payload or interview_payload(seed), headers=headers)
    assert response.status_code == 201
    return response, headers


def seed_feedback_summary(app):
    seed = seed_application(app)
    recruiter_id = seed_user(app, "recruiter", "application-owner@example.test")
    unrelated_recruiter_id = seed_user(app, "recruiter", "unrelated-recruiter@example.test")
    manager_id = seed_user(app, "hiring_manager", "job-manager@example.test")
    unassigned_interviewer_id = seed_user(app, "interviewer", "unassigned-summary@example.test")
    system_admin_id = seed_user(app, "system_admin", "summary-system@example.test")
    payload = interview_payload(seed)
    payload["participants"].append(
        {
            "user_id": str(seed["other_interviewer_id"]),
            "role": "interviewer",
            "required_feedback": True,
        }
    )
    with TestClient(app) as client:
        created, _ = create_interview(client, seed, key="feedback-summary-create", payload=payload)
    interview_id = UUID(created.json()["data"]["id"])
    submitted_at = datetime(2026, 7, 20, 10, 0, tzinfo=timezone.utc)
    with app.state.identity_store.sync_session() as database:
        application = database.get(Application, seed["application_id"])
        application.owner_id = recruiter_id
        submitted_author = database.get(User, seed["interviewer_id"])
        submitted_author.display_name = "Submitted Author"
        database.get(User, seed["other_interviewer_id"]).display_name = "Draft Author"
        database.add(
            JobCollaborator(
                organization_id=application.organization_id,
                job_id=seed["job_id"],
                user_id=manager_id,
                access_role="job_manager",
            )
        )
        submitted = InterviewFeedback(
            organization_id=application.organization_id,
            interview_id=interview_id,
            author_id=seed["interviewer_id"],
            status="submitted",
            ratings=feedback_payload()["ratings"],
            strengths="Strong evidence",
            risks="Scaling depth",
            conclusion="recommend",
            notes="Proceed",
            version=2,
            submitted_at=submitted_at,
        )
        draft = InterviewFeedback(
            organization_id=application.organization_id,
            interview_id=interview_id,
            author_id=seed["other_interviewer_id"],
            status="draft",
            ratings={"professional_ability": 1},
            strengths="SECRET DRAFT",
            version=1,
        )
        cross_organization = Organization(slug="feedback-other", name="Feedback Other", status="active")
        cross_tenant_user = User(
            organization=cross_organization,
            email="cross-tenant@example.test",
            normalized_email="cross-tenant@example.test",
            display_name="Cross tenant",
            password_hash=PasswordService().hash("cross tenant password"),
        )
        cross_tenant_user.roles.append(UserRole(role="recruiting_admin"))
        database.add_all([submitted, draft, cross_tenant_user])
        database.commit()
        return {
            **seed,
            "interview_id": interview_id,
            "submitted_feedback_id": submitted.id,
            "submitted_at": submitted_at,
            "recruiter_id": recruiter_id,
            "unrelated_recruiter_id": unrelated_recruiter_id,
            "manager_id": manager_id,
            "unassigned_interviewer_id": unassigned_interviewer_id,
            "system_admin_id": system_admin_id,
        }


def test_interview_feedbacks_return_submitted_results_without_drafts_or_contacts(tmp_path) -> None:
    app = make_app(tmp_path)
    seed = seed_feedback_summary(app)

    with TestClient(app) as client:
        response = client.get(
            f"/api/v1/interviews/{seed['interview_id']}/feedbacks",
            headers=login(client, "interview-admin@example.test"),
        )

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert response.json() == {
        "data": [
            {
                "id": str(seed["submitted_feedback_id"]),
                "interview_id": str(seed["interview_id"]),
                "author": {
                    "id": str(seed["interviewer_id"]),
                    "display_name": "Submitted Author",
                },
                "status": "submitted",
                "ratings": feedback_payload()["ratings"],
                "strengths": "Strong evidence",
                "risks": "Scaling depth",
                "conclusion": "recommend",
                "notes": "Proceed",
                "submitted_at": seed["submitted_at"].isoformat(),
                "version": 2,
            }
        ],
        "meta": {"count": 1},
    }
    assert "SECRET DRAFT" not in response.text
    assert "email" not in response.text


def test_interview_feedbacks_enforce_summary_read_permission_matrix(tmp_path) -> None:
    app = make_app(tmp_path)
    seed = seed_feedback_summary(app)
    path = f"/api/v1/interviews/{seed['interview_id']}/feedbacks"

    with TestClient(app) as client:
        for email in (
            "interview-admin@example.test",
            "application-owner@example.test",
            "job-manager@example.test",
            "assigned@example.test",
            "unassigned@example.test",
        ):
            assert client.get(path, headers=login(client, email)).status_code == 200

        for email in (
            "unrelated-recruiter@example.test",
            "unassigned-summary@example.test",
            "summary-system@example.test",
        ):
            denied = client.get(path, headers=login(client, email))
            assert denied.status_code == 404
            assert denied.json()["code"] == "resource_not_found"

        cross_login = client.post(
            "/api/v1/auth/login",
            json={
                "organization_slug": "feedback-other",
                "email": "cross-tenant@example.test",
                "password": "cross tenant password",
            },
            headers={"Origin": "https://hr.example.test"},
        )
        assert cross_login.status_code == 200
        cross_tenant = client.get(
            path,
            headers={
                "Origin": "https://hr.example.test",
                "X-CSRF-Token": cross_login.headers["X-CSRF-Token"],
            },
        )
        assert cross_tenant.status_code == 404
        assert cross_tenant.json()["code"] == "resource_not_found"


def seed_interview_materials(app):
    seed = seed_application(app)
    with TestClient(app) as client:
        created, _ = create_interview(client, seed, key="materials-create")
    interview_id = UUID(created.json()["data"]["id"])
    older_at = datetime(2026, 7, 19, 8, 0, tzinfo=timezone.utc)
    latest_at = datetime(2026, 7, 19, 9, 0, tzinfo=timezone.utc)
    with app.state.identity_store.sync_session() as database:
        application = database.get(Application, seed["application_id"])
        resume = database.get(Resume, application.resume_id)
        resume.parsed_text = (
            "姓名：李嘉明\n"
            "邮箱 candidate@example.test\n"
            "电话 +86 138 0013 8000\n"
            "地址：上海市敏感地址\n"
            "Python FastAPI Kubernetes"
        )
        seed_screening_results(
            database,
            application,
            resume.file_object_id,
            seed["admin_id"],
            [
                ("rules-v1", 60, "可沟通", older_at),
                ("rules-v2", 80, "优先沟通", latest_at),
            ],
        )
        database.flush()
        latest_result = database.scalar(
            select(ScreeningResult)
            .where(
                ScreeningResult.organization_id == application.organization_id,
                ScreeningResult.application_id == application.id,
            )
            .order_by(ScreeningResult.created_at.desc(), ScreeningResult.id.desc())
        )
        latest_result.required_missing = ["System design"]
        latest_result.risks = ["Contact candidate@example.test"]
        latest_result.questions = ["How would 李嘉明 scale FastAPI?"]
        database.add(
            JobJdVersion(
                organization_id=application.organization_id,
                job_id=application.job_id,
                version_number=2,
                content={"description": "Latest AI Engineer description"},
                created_by=seed["admin_id"],
            )
        )
        database.commit()
        return {
            **seed,
            "interview_id": interview_id,
            "resume_id": resume.id,
            "latest_result_id": latest_result.id,
        }


def test_interview_materials_allow_assigned_interviewer_and_return_redacted_minimal_projection(tmp_path) -> None:
    app = make_app(tmp_path)
    seed = seed_interview_materials(app)

    with TestClient(app) as client:
        response = client.get(
            f"/api/v1/interviews/{seed['interview_id']}/materials",
            headers=login(client, "assigned@example.test"),
        )

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert response.json() == {
        "data": {
            "interview_id": str(seed["interview_id"]),
            "candidate": {
                "id": str(seed["candidate_id"]),
                "display_name": "李嘉明",
                "current_title": "AI 算法工程师",
                "location": None,
            },
            "job": {"id": str(seed["job_id"]), "title": "AI Engineer"},
            "jd": {
                "version_number": 2,
                "description": "Latest AI Engineer description",
            },
            "resume": {
                "id": str(seed["resume_id"]),
                "preview_text": (
                    "姓名：[REDACTED]\n"
                    "邮箱 [REDACTED_EMAIL]\n"
                    "电话 [REDACTED_PHONE]\n"
                    "地址：[REDACTED]\n"
                    "Python FastAPI Kubernetes"
                ),
            },
            "screening": {
                "id": str(seed["latest_result_id"]),
                "required_missing": ["System design"],
                "risks": ["Contact [REDACTED_EMAIL]"],
                "questions": ["How would [REDACTED_NAME] scale FastAPI?"],
            },
        }
    }
    for forbidden in (
        "candidate@example.test",
        "138 0013 8000",
        "上海市敏感地址",
        "original_filename",
        "storage_key",
        "file_object_id",
        "contacts",
        "notes",
        "download",
    ):
        assert forbidden not in response.text

    with app.state.identity_store.sync_session() as database:
        audit = database.scalar(
            select(AuditLog).where(
                AuditLog.event_type == "interview.materials_viewed",
                AuditLog.actor_user_id == seed["interviewer_id"],
            )
        )
        assert audit is not None
        assert audit.outcome == "success"
        assert audit.metadata_json == {"interview_id": str(seed["interview_id"])}


def test_interview_materials_enforce_job_or_active_assignment_scope(tmp_path) -> None:
    app = make_app(tmp_path)
    seed = seed_interview_materials(app)
    path = f"/api/v1/interviews/{seed['interview_id']}/materials"
    with app.state.identity_store.sync_session() as database:
        other_organization = Organization(slug="materials-other", name="Materials Other", status="active")
        other_admin = User(
            organization=other_organization,
            email="materials-other@example.test",
            normalized_email="materials-other@example.test",
            display_name="Materials Other Admin",
            password_hash=PasswordService().hash("materials other password"),
        )
        other_admin.roles.append(UserRole(role="recruiting_admin"))
        database.add(other_admin)
        database.commit()

    with TestClient(app) as client:
        assert client.get(path, headers=login(client, "interview-admin@example.test")).status_code == 200

        unrelated = client.get(path, headers=login(client, "unassigned@example.test"))
        assert unrelated.status_code == 404
        assert unrelated.json()["code"] == "resource_not_found"

        cross_login = client.post(
            "/api/v1/auth/login",
            json={
                "organization_slug": "materials-other",
                "email": "materials-other@example.test",
                "password": "materials other password",
            },
            headers={"Origin": "https://hr.example.test"},
        )
        cross_tenant = client.get(
            path,
            headers={
                "Origin": "https://hr.example.test",
                "X-CSRF-Token": cross_login.headers["X-CSRF-Token"],
            },
        )
        assert cross_tenant.status_code == 404
        assert cross_tenant.json()["code"] == "resource_not_found"

    with app.state.identity_store.sync_session() as database:
        participant = database.scalar(
            select(InterviewParticipant).where(
                InterviewParticipant.interview_id == seed["interview_id"],
                InterviewParticipant.user_id == seed["interviewer_id"],
            )
        )
        participant.task_status = "cancelled"
        database.commit()

    with TestClient(app) as client:
        cancelled = client.get(path, headers=login(client, "assigned@example.test"))
        assert cancelled.status_code == 404
        assert cancelled.json()["code"] == "resource_not_found"


def test_interview_resume_file_allows_scoped_hr_and_active_assigned_interviewer(tmp_path) -> None:
    app = make_app(tmp_path)
    storage = InterviewResumeStorage()
    app.state.resume_storage = storage
    seed = seed_interview_materials(app)
    path = f"/api/v1/interviews/{seed['interview_id']}/resume-file"

    with TestClient(app) as client:
        interviewer = client.get(path, headers=login(client, "assigned@example.test"))
        hr_download = client.get(
            f"{path}?download=true",
            headers=login(client, "interview-admin@example.test"),
        )
        outsider = client.get(path, headers=login(client, "unassigned@example.test"))

    assert interviewer.status_code == 200
    assert interviewer.content == b"%PDF interview resume"
    assert interviewer.headers["content-type"] == "application/pdf"
    assert interviewer.headers["Content-Disposition"].startswith("inline;")
    assert interviewer.headers["Cache-Control"] == "no-store"
    assert interviewer.headers["X-Content-Type-Options"] == "nosniff"
    assert hr_download.status_code == 200
    assert hr_download.headers["Content-Disposition"].startswith("attachment;")
    assert outsider.status_code == 404
    assert outsider.json()["code"] == "resource_not_found"
    assert storage.opened == [
        ("interviews/resume.pdf", 10 * 1024 * 1024),
        ("interviews/resume.pdf", 10 * 1024 * 1024),
    ]
    assert storage.last_spool.closed

    with app.state.identity_store.sync_session() as database:
        audits = database.scalars(
            select(AuditLog)
            .where(AuditLog.event_type == "interview.resume_file_accessed")
            .order_by(AuditLog.created_at)
        ).all()
        assert [audit.metadata_json["disposition"] for audit in audits] == ["inline", "attachment"]
        assert all(audit.metadata_json["interview_id"] == str(seed["interview_id"]) for audit in audits)


def test_interview_resume_file_revokes_cancelled_assignment(tmp_path) -> None:
    app = make_app(tmp_path)
    app.state.resume_storage = InterviewResumeStorage()
    seed = seed_interview_materials(app)
    with app.state.identity_store.sync_session() as database:
        participant = database.scalar(
            select(InterviewParticipant).where(
                InterviewParticipant.interview_id == seed["interview_id"],
                InterviewParticipant.user_id == seed["interviewer_id"],
            )
        )
        participant.task_status = "cancelled"
        database.commit()

    with TestClient(app) as client:
        response = client.get(
            f"/api/v1/interviews/{seed['interview_id']}/resume-file",
            headers=login(client, "assigned@example.test"),
        )

    assert response.status_code == 404
    assert response.json()["code"] == "resource_not_found"


def test_interview_materials_reject_oversized_resume_preview_without_audit(tmp_path) -> None:
    app = make_app(tmp_path)
    seed = seed_interview_materials(app)
    with app.state.identity_store.sync_session() as database:
        database.get(Resume, seed["resume_id"]).parsed_text = "x" * (MAX_PREVIEW_BYTES + 1)
        database.commit()

    with TestClient(app) as client:
        response = client.get(
            f"/api/v1/interviews/{seed['interview_id']}/materials",
            headers=login(client, "assigned@example.test"),
        )

    assert response.status_code == 422
    assert response.json()["code"] == "preview_too_large"
    with app.state.identity_store.sync_session() as database:
        assert database.scalar(
            select(AuditLog).where(AuditLog.event_type == "interview.materials_viewed")
        ) is None


def test_create_interview_is_idempotent_checks_conflicts_and_scopes_interviewer_reads(tmp_path) -> None:
    app = make_app(tmp_path)
    seed = seed_application(app)
    payload = interview_payload(seed)
    with TestClient(app) as client:
        created, headers = create_interview(
            client,
            seed,
            key="create-first-interview",
            payload=payload,
        )
        interview = created.json()["data"]
        assert interview["status"] == "scheduled"
        assert interview["notification_status"] == "not_sent"
        assert interview["candidate"]["display_name"] == "李嘉明"
        assert created.headers["ETag"] == '"1"'

        replay = client.post("/api/v1/interviews", json=payload, headers=headers)
        assert replay.status_code == 201
        assert replay.json()["data"]["id"] == interview["id"]

        conflict = client.post(
            "/api/v1/interviews",
            json=payload,
            headers={**headers, "Idempotency-Key": "overlapping-interview"},
        )
        assert conflict.status_code == 409
        assert conflict.json()["code"] == "schedule_hard_conflict"

        assigned_headers = login(client, "assigned@example.test")
        assigned = client.get(f"/api/v1/interviews/{interview['id']}", headers=assigned_headers)
        assert assigned.status_code == 200
        assert assigned.json()["data"]["candidate"] == {
            "id": str(seed["candidate_id"]),
            "display_name": "李嘉明",
            "current_title": "AI 算法工程师",
        }

        unassigned_headers = login(client, "unassigned@example.test")
        denied = client.get(f"/api/v1/interviews/{interview['id']}", headers=unassigned_headers)
        assert denied.status_code == 404
        assert denied.json()["code"] == "resource_not_found"

    with app.state.identity_store.sync_session() as database:
        application = database.get(Application, seed["application_id"])
        assert application.stage == "interviewing"
        interview_id = UUID(interview["id"])
        assert database.scalar(select(Interview).where(Interview.id == interview_id)) is not None
        assert database.scalar(select(InterviewParticipant).where(InterviewParticipant.interview_id == interview_id)) is not None


def test_create_interview_rejects_an_application_that_has_not_completed_review(tmp_path) -> None:
    app = make_app(tmp_path)
    seed = seed_application(app)
    with app.state.identity_store.sync_session() as database:
        application = database.get(Application, seed["application_id"])
        application.stage = "new"
        database.commit()

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/interviews",
            json=interview_payload(seed),
            headers={
                **login(client, "interview-admin@example.test"),
                "Idempotency-Key": "create-from-new-application",
            },
        )

    assert response.status_code == 409
    assert response.json()["code"] == "invalid_state_transition"
    with app.state.identity_store.sync_session() as database:
        application = database.get(Application, seed["application_id"])
        assert application.stage == "new"
        assert database.scalar(
            select(Interview).where(Interview.application_id == seed["application_id"])
        ) is None


def test_revoked_recruiting_role_removes_historical_assignment_access(tmp_path) -> None:
    app = make_app(tmp_path)
    seed = seed_application(app)
    with TestClient(app) as client:
        created, _ = create_interview(client, seed, key="role-revocation-create")
        interview_id = created.json()["data"]["id"]

    with app.state.identity_store.sync_session() as database:
        database.execute(delete(UserRole).where(UserRole.user_id == seed["interviewer_id"]))
        database.add(UserRole(user_id=seed["interviewer_id"], role="system_admin"))
        database.commit()

    with TestClient(app) as client:
        headers = login(client, "assigned@example.test")
        assert client.get(f"/api/v1/interviews/{interview_id}", headers=headers).status_code == 404
        assert client.get(f"/api/v1/interviews/{interview_id}/calendar-file", headers=headers).status_code == 404
        assert client.get(f"/api/v1/interviews/{interview_id}/my-feedback", headers=headers).status_code == 404
        assert client.put(
            f"/api/v1/interviews/{interview_id}/my-feedback",
            json=feedback_payload(),
            headers={**headers, "If-Match": '"0"'},
        ).status_code == 404
        assert client.get("/api/v1/interviews", headers=headers).json() == {
            "data": [],
            "meta": {"limit": 50, "next_cursor": None},
        }
        assert client.get("/api/v1/me/tasks", headers=headers).json() == {"data": [], "meta": {"count": 0}}


def test_create_rejects_same_candidate_overlap_with_different_interviewer(tmp_path) -> None:
    app = make_app(tmp_path)
    seed = seed_application(app)
    start = datetime(2099, 7, 20, 8, 0, tzinfo=timezone.utc)
    with TestClient(app) as client:
        create_interview(client, seed, key="candidate-conflict-first", payload=interview_payload(seed, starts_at=start))
        overlapping = interview_payload(seed, starts_at=start)
        overlapping["participants"] = [
            {
                "user_id": str(seed["other_interviewer_id"]),
                "role": "interviewer",
                "required_feedback": True,
            }
        ]
        response = client.post(
            "/api/v1/interviews",
            json=overlapping,
            headers={
                **login(client, "interview-admin@example.test"),
                "Idempotency-Key": "candidate-conflict-second",
            },
        )

    assert response.status_code == 409
    assert response.json()["code"] == "schedule_hard_conflict"


def test_create_interview_rejects_a_past_start_time(tmp_path) -> None:
    app = make_app(tmp_path)
    seed = seed_application(app)
    past_start = datetime.now(timezone.utc) - timedelta(minutes=5)

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/interviews",
            json=interview_payload(seed, starts_at=past_start),
            headers={
                **login(client, "interview-admin@example.test"),
                "Idempotency-Key": "past-interview-create",
            },
        )

    assert response.status_code == 422
    assert response.json()["code"] == "interview_time_in_past"


def test_new_interview_conflicts_report_hard_candidate_overlap(tmp_path) -> None:
    app = make_app(tmp_path)
    seed = seed_application(app)
    start = datetime(2099, 7, 20, 8, 0, tzinfo=timezone.utc)
    with TestClient(app) as client:
        existing, headers = create_interview(
            client,
            seed,
            key="preflight-hard-existing",
            payload=interview_payload(seed, starts_at=start),
        )
        response = client.post(
            "/api/v1/interview-conflicts",
            json={
                "application_id": str(seed["application_id"]),
                "starts_at": start.isoformat(),
                "ends_at": (start + timedelta(minutes=45)).isoformat(),
                "participant_ids": [str(seed["other_interviewer_id"])],
                "buffer_minutes": 15,
            },
            headers=headers,
        )

    assert response.status_code == 200
    assert response.json()["data"]["hard"] == [existing.json()["data"]["id"]]
    assert response.json()["data"]["soft"] == []


def test_new_interview_conflicts_report_soft_adjacent_participant_booking(tmp_path) -> None:
    app = make_app(tmp_path)
    seed = seed_application(app)
    start = datetime(2099, 7, 20, 8, 0, tzinfo=timezone.utc)
    with TestClient(app) as client:
        existing, headers = create_interview(
            client,
            seed,
            key="preflight-soft-existing",
            payload=interview_payload(seed, starts_at=start),
        )
        adjacent_start = start + timedelta(minutes=55)
        response = client.post(
            "/api/v1/interview-conflicts",
            json={
                "application_id": str(seed["application_id"]),
                "starts_at": adjacent_start.isoformat(),
                "ends_at": (adjacent_start + timedelta(minutes=45)).isoformat(),
                "participant_ids": [str(seed["interviewer_id"])],
                "buffer_minutes": 15,
            },
            headers=headers,
        )

    assert response.status_code == 200
    assert response.json()["data"]["hard"] == []
    assert response.json()["data"]["soft"] == [existing.json()["data"]["id"]]


def test_new_interview_conflicts_return_empty_when_schedule_is_available(tmp_path) -> None:
    app = make_app(tmp_path)
    seed = seed_application(app)
    start = datetime(2099, 7, 20, 8, 0, tzinfo=timezone.utc)
    with TestClient(app) as client:
        _, headers = create_interview(
            client,
            seed,
            key="preflight-clear-existing",
            payload=interview_payload(seed, starts_at=start),
        )
        available_start = start + timedelta(hours=4)
        response = client.post(
            "/api/v1/interview-conflicts",
            json={
                "application_id": str(seed["application_id"]),
                "starts_at": available_start.isoformat(),
                "ends_at": (available_start + timedelta(minutes=45)).isoformat(),
                "participant_ids": [str(seed["interviewer_id"])],
                "buffer_minutes": 15,
            },
            headers=headers,
        )

    assert response.status_code == 200
    assert response.json()["data"]["hard"] == []
    assert response.json()["data"]["soft"] == []


def test_new_interview_conflicts_require_application_scope_and_tenant_participants(tmp_path) -> None:
    app = make_app(tmp_path)
    seed = seed_application(app)
    payload = {
        "application_id": str(seed["application_id"]),
        "starts_at": datetime(2099, 7, 20, 8, 0, tzinfo=timezone.utc).isoformat(),
        "ends_at": datetime(2099, 7, 20, 8, 45, tzinfo=timezone.utc).isoformat(),
        "participant_ids": [str(seed["interviewer_id"])],
        "buffer_minutes": 15,
    }
    with app.state.identity_store.sync_session() as database:
        other_organization = Organization(slug="conflict-other", name="Conflict Other", status="active")
        other_admin = User(
            organization=other_organization,
            email="conflict-other@example.test",
            normalized_email="conflict-other@example.test",
            display_name="Conflict Other Admin",
            password_hash=PasswordService().hash("conflict other password"),
        )
        other_admin.roles.append(UserRole(role="recruiting_admin"))
        database.add(other_admin)
        database.commit()
        other_admin_id = other_admin.id

    with TestClient(app) as client:
        anonymous = client.post("/api/v1/interview-conflicts", json=payload)
        assert anonymous.status_code == 403
        assert anonymous.json()["code"] == "csrf_validation_failed"

        unauthorized = client.post(
            "/api/v1/interview-conflicts",
            json=payload,
            headers=login(client, "assigned@example.test"),
        )
        assert unauthorized.status_code == 404
        assert unauthorized.json()["code"] == "resource_not_found"

        cross_login = client.post(
            "/api/v1/auth/login",
            json={
                "organization_slug": "conflict-other",
                "email": "conflict-other@example.test",
                "password": "conflict other password",
            },
            headers={"Origin": "https://hr.example.test"},
        )
        cross_tenant = client.post(
            "/api/v1/interview-conflicts",
            json=payload,
            headers={
                "Origin": "https://hr.example.test",
                "X-CSRF-Token": cross_login.headers["X-CSRF-Token"],
            },
        )
        assert cross_tenant.status_code == 404
        assert cross_tenant.json()["code"] == "resource_not_found"

        foreign_participant = client.post(
            "/api/v1/interview-conflicts",
            json={**payload, "participant_ids": [str(other_admin_id)]},
            headers=login(client, "interview-admin@example.test"),
        )
        assert foreign_participant.status_code == 404
        assert foreign_participant.json()["code"] == "resource_not_found"


def test_reschedule_rejects_same_candidate_overlap_with_different_interviewer(tmp_path) -> None:
    app = make_app(tmp_path)
    seed = seed_application(app)
    first_start = datetime(2099, 7, 20, 8, 0, tzinfo=timezone.utc)
    second_start = datetime(2099, 7, 20, 12, 0, tzinfo=timezone.utc)
    second_payload = interview_payload(seed, starts_at=second_start)
    second_payload["participants"] = [
        {
            "user_id": str(seed["other_interviewer_id"]),
            "role": "interviewer",
            "required_feedback": True,
        }
    ]
    with TestClient(app) as client:
        create_interview(client, seed, key="candidate-reschedule-first", payload=interview_payload(seed, starts_at=first_start))
        second, admin_headers = create_interview(
            client,
            seed,
            key="candidate-reschedule-second",
            payload=second_payload,
        )
        response = client.patch(
            f"/api/v1/interviews/{second.json()['data']['id']}",
            json={
                "starts_at": first_start.isoformat(),
                "ends_at": (first_start + timedelta(minutes=45)).isoformat(),
            },
            headers={**admin_headers, "If-Match": '"1"'},
        )

    assert response.status_code == 409
    assert response.json()["code"] == "schedule_hard_conflict"


def test_reschedule_rejects_a_past_start_time(tmp_path) -> None:
    app = make_app(tmp_path)
    seed = seed_application(app)
    past_start = datetime.now(timezone.utc) - timedelta(minutes=5)

    with TestClient(app) as client:
        created, admin_headers = create_interview(client, seed, key="past-reschedule-create")
        response = client.patch(
            f"/api/v1/interviews/{created.json()['data']['id']}",
            json={
                "starts_at": past_start.isoformat(),
                "ends_at": (past_start + timedelta(minutes=45)).isoformat(),
            },
            headers={**admin_headers, "If-Match": '"1"'},
        )

    assert response.status_code == 422
    assert response.json()["code"] == "interview_time_in_past"


def test_conflict_preflights_reject_a_past_start_time(tmp_path) -> None:
    app = make_app(tmp_path)
    seed = seed_application(app)
    past_start = datetime.now(timezone.utc) - timedelta(minutes=5)

    with TestClient(app) as client:
        created, admin_headers = create_interview(client, seed, key="past-preflight-create")
        schedule = {
            "starts_at": past_start.isoformat(),
            "ends_at": (past_start + timedelta(minutes=45)).isoformat(),
            "participant_ids": [str(seed["interviewer_id"])],
            "buffer_minutes": 15,
        }
        new_interview = client.post(
            "/api/v1/interview-conflicts",
            json={**schedule, "application_id": str(seed["application_id"])},
            headers=admin_headers,
        )
        existing_interview = client.post(
            f"/api/v1/interviews/{created.json()['data']['id']}/conflicts",
            json=schedule,
            headers=admin_headers,
        )

    for response in (new_interview, existing_interview):
        assert response.status_code == 422
        assert response.json()["code"] == "interview_time_in_past"


def test_reschedule_preserves_history_and_transition_calendar_versions(tmp_path) -> None:
    app = make_app(tmp_path)
    seed = seed_application(app)
    with TestClient(app) as client:
        created, headers = create_interview(client, seed)
        interview = created.json()["data"]
        interview_id = interview["id"]
        new_start = datetime(2099, 7, 21, 9, 30, tzinfo=timezone.utc)

        stale = client.patch(
            f"/api/v1/interviews/{interview_id}",
            json={"starts_at": new_start.isoformat(), "ends_at": (new_start + timedelta(minutes=60)).isoformat()},
            headers={**headers, "If-Match": '"9"'},
        )
        assert stale.status_code == 409
        assert stale.json()["code"] == "resource_version_conflict"

        rescheduled = client.patch(
            f"/api/v1/interviews/{interview_id}",
            json={"starts_at": new_start.isoformat(), "ends_at": (new_start + timedelta(minutes=60)).isoformat()},
            headers={**headers, "If-Match": '"1"'},
        )
        assert rescheduled.status_code == 200
        assert rescheduled.json()["data"]["status"] == "rescheduled"
        assert rescheduled.json()["data"]["version"] == 2
        assert rescheduled.json()["data"]["calendar_sequence"] == 1

        calendar = client.get(f"/api/v1/interviews/{interview_id}/calendar-file", headers=headers)
        assert calendar.status_code == 200
        assert calendar.headers["content-type"].startswith("text/calendar")
        assert b"SEQUENCE:1\r\n" in calendar.content
        assert b"DTSTART:20990721T093000Z\r\n" in calendar.content
        assert b"mailto:interview-admin@example.test\r\n" in calendar.content
        assert b"mailto:assigned@example.test\r\n" in calendar.content

        confirmed = client.post(
            f"/api/v1/interviews/{interview_id}/transitions",
            json={"target": "confirmed"},
            headers={**headers, "If-Match": '"2"', "Idempotency-Key": "confirm-interview"},
        )
        assert confirmed.status_code == 200
        assert confirmed.json()["data"]["status"] == "confirmed"
        assert confirmed.json()["data"]["version"] == 3

        completed = client.post(
            f"/api/v1/interviews/{interview_id}/transitions",
            json={"target": "completed"},
            headers={**headers, "If-Match": '"3"', "Idempotency-Key": "complete-interview"},
        )
        assert completed.status_code == 200
        assert completed.json()["data"]["status"] == "pending_feedback"

    with app.state.identity_store.sync_session() as database:
        events = database.scalars(
            select(InterviewEvent)
            .where(InterviewEvent.interview_id == UUID(interview_id))
            .order_by(InterviewEvent.created_at)
        ).all()
        reschedule_event = next(item for item in events if item.event_type == "interview.rescheduled")
        assert reschedule_event.payload["previous"]["starts_at"] == interview["starts_at"]
        assert reschedule_event.payload["current"]["starts_at"] == new_start.isoformat()


def test_calendar_cancel_reuses_persisted_request_contacts_after_user_changes(tmp_path) -> None:
    app = make_app(tmp_path)
    seed = seed_application(app)
    with TestClient(app) as client:
        created, admin_headers = create_interview(client, seed, key="calendar-contact-create")
        interview_id = created.json()["data"]["id"]
        invitation = client.get(f"/api/v1/interviews/{interview_id}/calendar-file", headers=admin_headers)
        assert invitation.status_code == 200

        with app.state.identity_store.sync_session() as database:
            admin = database.get(User, seed["admin_id"])
            admin.email = "renamed-admin@example.test"
            admin.normalized_email = admin.email
            admin.display_name = "Renamed recruiter"
            interviewer = database.get(User, seed["interviewer_id"])
            interviewer.email = "renamed-interviewer@example.test"
            interviewer.normalized_email = interviewer.email
            interviewer.display_name = "Renamed interviewer"
            database.commit()

        cancelled = client.post(
            f"/api/v1/interviews/{interview_id}/transitions",
            json={"target": "cancelled", "reason": "Role closed"},
            headers={**admin_headers, "If-Match": '"1"', "Idempotency-Key": "calendar-contact-cancel"},
        )
        assert cancelled.status_code == 200
        cancellation = client.get(f"/api/v1/interviews/{interview_id}/calendar-file", headers=admin_headers)
        assert cancellation.status_code == 200

    def contacts(payload):
        return {
            line
            for line in payload.content.decode("utf-8").split("\r\n")
            if line.startswith(("ORGANIZER", "ATTENDEE"))
        }

    assert contacts(invitation) == contacts(cancellation)
    assert b"METHOD:REQUEST\r\n" in invitation.content
    assert b"METHOD:CANCEL\r\n" in cancellation.content


def feedback_payload(conclusion="recommend"):
    return {
        "ratings": {
            "professional_ability": 4,
            "problem_solving": 3,
            "communication": 4,
            "role_fit": 4,
        },
        "strengths": "RAG 与 Agent 项目经验完整",
        "risks": "大规模推理成本经验需要确认",
        "conclusion": conclusion,
        "notes": "建议进入下一轮",
    }


def test_application_waits_for_all_active_interview_rounds_before_decision(tmp_path) -> None:
    app = make_app(tmp_path)
    seed = seed_application(app)
    with TestClient(app) as client:
        first, admin_headers = create_interview(
            client,
            seed,
            key="decision-gate-first",
            payload=interview_payload(
                seed,
                starts_at=datetime(2099, 7, 20, 8, 0, tzinfo=timezone.utc),
            ),
        )
        second, _ = create_interview(
            client,
            seed,
            key="decision-gate-second",
            payload=interview_payload(
                seed,
                starts_at=datetime(2099, 7, 20, 12, 0, tzinfo=timezone.utc),
            ),
        )
        first_id = first.json()["data"]["id"]
        second_id = second.json()["data"]["id"]
        admin_headers = login(client, "interview-admin@example.test")
        assert client.post(
            f"/api/v1/interviews/{first_id}/transitions",
            json={"target": "confirmed"},
            headers={**admin_headers, "If-Match": '"1"', "Idempotency-Key": "decision-gate-confirm"},
        ).status_code == 200
        assert client.post(
            f"/api/v1/interviews/{first_id}/transitions",
            json={"target": "completed"},
            headers={**admin_headers, "If-Match": '"2"', "Idempotency-Key": "decision-gate-complete"},
        ).status_code == 200
        interviewer_headers = login(client, "assigned@example.test")
        assert client.put(
            f"/api/v1/interviews/{first_id}/my-feedback",
            json=feedback_payload(),
            headers={**interviewer_headers, "If-Match": '"0"'},
        ).status_code == 200
        assert client.post(
            f"/api/v1/interviews/{first_id}/my-feedback/submit",
            headers={**interviewer_headers, "Idempotency-Key": "decision-gate-submit"},
        ).status_code == 200

        with app.state.identity_store.sync_session() as database:
            assert database.get(Application, seed["application_id"]).stage == "interviewing"

        admin_headers = login(client, "interview-admin@example.test")
        cancelled = client.post(
            f"/api/v1/interviews/{second_id}/transitions",
            json={"target": "cancelled", "reason": "Second round no longer required"},
            headers={**admin_headers, "If-Match": '"1"', "Idempotency-Key": "decision-gate-cancel"},
        )
        assert cancelled.status_code == 200

    with app.state.identity_store.sync_session() as database:
        assert database.get(Application, seed["application_id"]).stage == "decision"


def test_interview_without_required_feedback_advances_application_on_completion(tmp_path) -> None:
    app = make_app(tmp_path)
    seed = seed_application(app)
    payload = interview_payload(seed)
    payload["participants"][0]["required_feedback"] = False
    with TestClient(app) as client:
        created, admin_headers = create_interview(client, seed, key="zero-feedback-create", payload=payload)
        interview_id = created.json()["data"]["id"]
        assert client.post(
            f"/api/v1/interviews/{interview_id}/transitions",
            json={"target": "confirmed"},
            headers={**admin_headers, "If-Match": '"1"', "Idempotency-Key": "zero-feedback-confirm"},
        ).status_code == 200
        completed = client.post(
            f"/api/v1/interviews/{interview_id}/transitions",
            json={"target": "completed"},
            headers={**admin_headers, "If-Match": '"2"', "Idempotency-Key": "zero-feedback-complete"},
        )
        assert completed.status_code == 200
        assert completed.json()["data"]["status"] == "feedback_completed"

    with app.state.identity_store.sync_session() as database:
        assert database.get(Application, seed["application_id"]).stage == "decision"


def test_feedback_is_private_idempotent_and_advances_only_after_all_required_submit(tmp_path) -> None:
    app = make_app(tmp_path)
    seed = seed_application(app)
    payload = interview_payload(seed)
    payload["participants"].append(
        {
            "user_id": str(seed["other_interviewer_id"]),
            "role": "interviewer",
            "required_feedback": True,
        }
    )
    with TestClient(app) as client:
        created, admin_headers = create_interview(client, seed, payload=payload)
        interview = created.json()["data"]
        interview_id = interview["id"]
        confirmed = client.post(
            f"/api/v1/interviews/{interview_id}/transitions",
            json={"target": "confirmed"},
            headers={**admin_headers, "If-Match": '"1"', "Idempotency-Key": "feedback-confirm"},
        )
        assert confirmed.status_code == 200
        completed = client.post(
            f"/api/v1/interviews/{interview_id}/transitions",
            json={"target": "completed"},
            headers={**admin_headers, "If-Match": '"2"', "Idempotency-Key": "feedback-complete"},
        )
        assert completed.status_code == 200
        assert completed.json()["data"]["status"] == "pending_feedback"

        first_headers = login(client, "assigned@example.test")
        empty = client.get(f"/api/v1/interviews/{interview_id}/my-feedback", headers=first_headers)
        assert empty.status_code == 200
        assert empty.json()["data"] == {"status": "draft", "version": 0}

        saved = client.put(
            f"/api/v1/interviews/{interview_id}/my-feedback",
            json=feedback_payload(),
            headers={**first_headers, "If-Match": '"0"'},
        )
        assert saved.status_code == 200
        assert saved.json()["data"]["status"] == "draft"
        assert saved.headers["ETag"] == '"1"'

        submit_headers = {**first_headers, "Idempotency-Key": "first-feedback-submit"}
        submitted = client.post(
            f"/api/v1/interviews/{interview_id}/my-feedback/submit",
            headers=submit_headers,
        )
        assert submitted.status_code == 200
        assert submitted.json()["data"]["status"] == "submitted"
        replay = client.post(
            f"/api/v1/interviews/{interview_id}/my-feedback/submit",
            headers=submit_headers,
        )
        assert replay.status_code == 200
        assert replay.json()["data"]["id"] == submitted.json()["data"]["id"]

        second_headers = login(client, "unassigned@example.test")
        second_saved = client.put(
            f"/api/v1/interviews/{interview_id}/my-feedback",
            json=feedback_payload("strong_recommend"),
            headers={**second_headers, "If-Match": '"0"'},
        )
        assert second_saved.status_code == 200
        second_submitted = client.post(
            f"/api/v1/interviews/{interview_id}/my-feedback/submit",
            headers={**second_headers, "Idempotency-Key": "second-feedback-submit"},
        )
        assert second_submitted.status_code == 200

    with app.state.identity_store.sync_session() as database:
        application = database.get(Application, seed["application_id"])
        stored_interview = database.get(Interview, UUID(interview_id))
        feedbacks = database.scalars(
            select(InterviewFeedback).where(InterviewFeedback.interview_id == UUID(interview_id))
        ).all()
        assert application.stage == "decision"
        assert stored_interview.status == "feedback_completed"
        assert len(feedbacks) == 2
        assert all(item.status == "submitted" for item in feedbacks)


@pytest.mark.parametrize("interview_status", ["scheduled", "rescheduled", "confirmed"])
def test_assigned_participant_can_save_and_submit_feedback_for_a_future_interview(
    tmp_path, interview_status
) -> None:
    app = make_app(tmp_path)
    seed = seed_application(app)
    manager_id = seed_user(app, "hiring_manager", "feedback-manager@example.test")
    start = datetime.now(timezone.utc) + timedelta(hours=2)
    payload = interview_payload(seed, starts_at=start)
    payload["participants"] = [
        {
            "user_id": str(manager_id),
            "role": "interviewer",
            "required_feedback": True,
        }
    ]
    with TestClient(app) as client:
        created, _ = create_interview(
            client,
            seed,
            key=f"future-{interview_status}-interview-create",
            payload=payload,
        )
        interview_id = created.json()["data"]["id"]
        admin_headers = login(client, "interview-admin@example.test")
        if interview_status == "rescheduled":
            changed_start = start + timedelta(hours=1)
            rescheduled = client.patch(
                f"/api/v1/interviews/{interview_id}",
                json={
                    "starts_at": changed_start.isoformat(),
                    "ends_at": (changed_start + timedelta(minutes=45)).isoformat(),
                },
                headers={**admin_headers, "If-Match": '"1"'},
            )
            assert rescheduled.status_code == 200
            assert rescheduled.json()["data"]["status"] == "rescheduled"
        elif interview_status == "confirmed":
            confirmed = client.post(
                f"/api/v1/interviews/{interview_id}/transitions",
                json={"target": "confirmed"},
                headers={
                    **admin_headers,
                    "If-Match": '"1"',
                    "Idempotency-Key": "future-feedback-confirm",
                },
            )
            assert confirmed.status_code == 200
            assert confirmed.json()["data"]["status"] == "confirmed"
        participant_headers = login(client, "feedback-manager@example.test")
        created_feedback = client.put(
            f"/api/v1/interviews/{interview_id}/my-feedback",
            json=feedback_payload(),
            headers={**participant_headers, "If-Match": '"0"'},
        )
        with app.state.identity_store.sync_session() as database:
            assert database.get(Interview, UUID(interview_id)).status == interview_status
        if interview_status == "scheduled":
            admin_headers = login(client, "interview-admin@example.test")
            changed_start = start + timedelta(hours=1)
            rescheduled_after_draft = client.patch(
                f"/api/v1/interviews/{interview_id}",
                json={
                    "starts_at": changed_start.isoformat(),
                    "ends_at": (changed_start + timedelta(minutes=45)).isoformat(),
                },
                headers={**admin_headers, "If-Match": '"1"'},
            )
            assert rescheduled_after_draft.status_code == 200
            assert rescheduled_after_draft.json()["data"]["status"] == "rescheduled"
            participant_headers = login(client, "feedback-manager@example.test")
        saved = client.put(
            f"/api/v1/interviews/{interview_id}/my-feedback",
            json=feedback_payload("strong_recommend"),
            headers={**participant_headers, "If-Match": '"1"'},
        )
        submitted = client.post(
            f"/api/v1/interviews/{interview_id}/my-feedback/submit",
            headers={
                **participant_headers,
                "Idempotency-Key": f"future-{interview_status}-feedback-submit",
            },
        )

    assert created_feedback.status_code == 200
    assert saved.status_code == 200
    assert saved.json()["data"]["version"] == 2
    assert submitted.status_code == 200
    assert submitted.json()["data"]["status"] == "submitted"
    with app.state.identity_store.sync_session() as database:
        interview = database.get(Interview, UUID(interview_id))
        application = database.get(Application, seed["application_id"])
        assert interview.status == "feedback_completed"
        assert application.stage == "decision"
        assert database.scalar(
            select(InterviewEvent).where(
                InterviewEvent.interview_id == UUID(interview_id),
                InterviewEvent.event_type == "interview.feedback_opened",
            )
        ) is not None
        assert database.scalar(
            select(AuditLog).where(
                AuditLog.actor_user_id == manager_id,
                AuditLog.event_type == "interview.feedback_opened",
            )
        ) is not None


def test_submitted_feedback_amendment_requires_its_author_reason_and_version(tmp_path) -> None:
    app = make_app(tmp_path)
    seed = seed_application(app)
    with TestClient(app) as client:
        created, admin_headers = create_interview(client, seed)
        interview_id = created.json()["data"]["id"]
        assert client.post(
            f"/api/v1/interviews/{interview_id}/transitions",
            json={"target": "confirmed"},
            headers={**admin_headers, "If-Match": '"1"', "Idempotency-Key": "amend-confirm"},
        ).status_code == 200
        assert client.post(
            f"/api/v1/interviews/{interview_id}/transitions",
            json={"target": "completed"},
            headers={**admin_headers, "If-Match": '"2"', "Idempotency-Key": "amend-complete"},
        ).status_code == 200

        author_headers = login(client, "assigned@example.test")
        saved = client.put(
            f"/api/v1/interviews/{interview_id}/my-feedback",
            json=feedback_payload(),
            headers={**author_headers, "If-Match": '"0"'},
        )
        submitted = client.post(
            f"/api/v1/interviews/{interview_id}/my-feedback/submit",
            headers={**author_headers, "Idempotency-Key": "amend-submit"},
        )
        feedback = submitted.json()["data"]
        assert feedback["version"] == 2

        outsider_headers = login(client, "unassigned@example.test")
        denied = client.post(
            f"/api/v1/interview-feedback/{feedback['id']}/amendments",
            json={**feedback_payload(), "reason": "Not my feedback"},
            headers={**outsider_headers, "If-Match": '"2"'},
        )
        assert denied.status_code == 404

        author_headers = login(client, "assigned@example.test")
        amendment_payload = {
            **feedback_payload("strong_recommend"),
            "notes": "补充核实了线上吞吐数据",
            "reason": "候选人补充了量化证据",
        }
        amended = client.post(
            f"/api/v1/interview-feedback/{feedback['id']}/amendments",
            json=amendment_payload,
            headers={**author_headers, "If-Match": '"2"'},
        )
        assert amended.status_code == 200
        assert amended.json()["data"]["status"] == "amended"
        assert amended.json()["data"]["version"] == 3
        assert amended.headers["ETag"] == '"3"'

        stale = client.post(
            f"/api/v1/interview-feedback/{feedback['id']}/amendments",
            json=amendment_payload,
            headers={**author_headers, "If-Match": '"2"'},
        )
        assert stale.status_code == 409

    with app.state.identity_store.sync_session() as database:
        revision = database.scalar(
            select(InterviewFeedbackRevision).where(
                InterviewFeedbackRevision.feedback_id == UUID(feedback["id"])
            )
        )
        assert revision.reason == "候选人补充了量化证据"
        assert revision.previous_payload["notes"] == "建议进入下一轮"
        assert revision.new_payload["notes"] == "补充核实了线上吞吐数据"


def test_interview_list_uses_stable_signed_cursor_pagination(tmp_path) -> None:
    app = make_app(tmp_path)
    seed = seed_application(app)
    starts_at = datetime(2099, 7, 20, 8, 0, tzinfo=timezone.utc)
    with app.state.identity_store.sync_session() as database:
        application = database.get(Application, seed["application_id"])
        interviews = [
            Interview(
                organization_id=application.organization_id,
                application_id=application.id,
                round_name=f"Round {index}",
                method="video",
                timezone="Asia/Shanghai",
                starts_at=starts_at if index < 2 else starts_at + timedelta(hours=1),
                ends_at=(starts_at if index < 2 else starts_at + timedelta(hours=1)) + timedelta(minutes=45),
                meeting_url="https://meeting.example.test/room",
                owner_id=seed["admin_id"],
                created_by=seed["admin_id"],
                status="scheduled",
            )
            for index in range(3)
        ]
        database.add_all(interviews)
        database.commit()
        expected_ids = [str(item.id) for item in sorted(interviews, key=lambda item: (item.starts_at, item.id))]

    with TestClient(app) as client:
        headers = login(client, "interview-admin@example.test")
        first = client.get("/api/v1/interviews", params={"limit": 2}, headers=headers)
        second = client.get(
            "/api/v1/interviews",
            params={"limit": 2, "cursor": first.json()["meta"]["next_cursor"]},
            headers=headers,
        )

    assert first.status_code == second.status_code == 200
    assert first.json()["meta"]["limit"] == second.json()["meta"]["limit"] == 2
    assert first.json()["meta"]["next_cursor"] is not None
    assert second.json()["meta"]["next_cursor"] is None
    assert [item["id"] for item in first.json()["data"] + second.json()["data"]] == expected_ids


def test_interview_list_rejects_invalid_cross_filter_and_cross_tenant_cursors(tmp_path) -> None:
    app = make_app(tmp_path)
    seed = seed_application(app)
    other_organization = Organization(slug="cursor-other", name="Cursor Other", status="active")
    other_admin = User(
        organization=other_organization,
        email="cursor-other@example.test",
        normalized_email="cursor-other@example.test",
        display_name="Cursor Other Admin",
        password_hash=PasswordService().hash("cursor other password"),
    )
    other_admin.roles.append(UserRole(role="recruiting_admin"))
    with app.state.identity_store.sync_session() as database:
        database.add(other_admin)
        database.commit()

    with TestClient(app) as client:
        _, headers = create_interview(client, seed, key="cursor-binding-first")
        create_interview(
            client,
            seed,
            key="cursor-binding-second",
            payload=interview_payload(seed, starts_at=datetime(2099, 7, 20, 10, 0, tzinfo=timezone.utc)),
        )
        first = client.get(
            "/api/v1/interviews",
            params={"status": "scheduled", "limit": 1},
            headers=headers,
        )
        token = first.json()["meta"]["next_cursor"]
        tampered_token = ("A" if token[0] != "A" else "B") + token[1:]
        invalid = client.get("/api/v1/interviews", params={"cursor": tampered_token}, headers=headers)
        mismatch = client.get(
            "/api/v1/interviews",
            params={"status": "confirmed", "cursor": token},
            headers=headers,
        )
        too_small = client.get("/api/v1/interviews", params={"limit": 0}, headers=headers)
        too_large = client.get("/api/v1/interviews", params={"limit": 101}, headers=headers)

        other_login = client.post(
            "/api/v1/auth/login",
            json={
                "organization_slug": "cursor-other",
                "email": "cursor-other@example.test",
                "password": "cursor other password",
            },
            headers={"Origin": "https://hr.example.test"},
        )
        cross_tenant = client.get(
            "/api/v1/interviews",
            params={"status": "scheduled", "cursor": token},
            headers={
                "Origin": "https://hr.example.test",
                "X-CSRF-Token": other_login.headers["X-CSRF-Token"],
            },
        )

    assert first.status_code == 200
    assert token is not None
    for response in (invalid, mismatch, cross_tenant, too_small, too_large):
        assert response.status_code == 422
        assert response.json()["code"] == "validation_failed"


def test_calendar_download_commits_safe_audit_before_returning_file(tmp_path) -> None:
    app = make_app(tmp_path)
    seed = seed_application(app)
    with TestClient(app) as client:
        created, headers = create_interview(client, seed, key="calendar-download-audit")
        interview_id = created.json()["data"]["id"]
        response = client.get(f"/api/v1/interviews/{interview_id}/calendar-file", headers=headers)

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["content-type"].startswith("text/calendar")
    with app.state.identity_store.sync_session() as database:
        audit = database.scalar(
            select(AuditLog).where(AuditLog.event_type == "interview.calendar_downloaded")
        )
        assert audit is not None
        assert audit.organization_id == database.get(User, seed["admin_id"]).organization_id
        assert audit.actor_user_id == seed["admin_id"]
        assert audit.outcome == "success"
        assert audit.metadata_json == {"interview_id": interview_id}


def test_interview_list_conflicts_and_my_tasks_share_assignment_scope(tmp_path) -> None:
    app = make_app(tmp_path)
    seed = seed_application(app)
    first_start = datetime(2099, 7, 20, 8, 0, tzinfo=timezone.utc)
    second_start = datetime(2099, 7, 20, 12, 0, tzinfo=timezone.utc)
    with TestClient(app) as client:
        first, admin_headers = create_interview(
            client,
            seed,
            key="scope-first",
            payload=interview_payload(seed, starts_at=first_start),
        )
        second, admin_headers = create_interview(
            client,
            seed,
            key="scope-second",
            payload=interview_payload(seed, starts_at=second_start),
        )
        first_id = first.json()["data"]["id"]
        second_id = second.json()["data"]["id"]

        conflict = client.post(
            f"/api/v1/interviews/{first_id}/conflicts",
            json={
                "starts_at": second_start.isoformat(),
                "ends_at": (second_start + timedelta(minutes=30)).isoformat(),
                "participant_ids": [str(seed["interviewer_id"])],
                "buffer_minutes": 15,
            },
            headers=admin_headers,
        )
        assert conflict.status_code == 200
        assert conflict.json()["data"]["hard"] == [second_id]
        assert conflict.json()["data"]["soft"] == []

        admin_list = client.get("/api/v1/interviews", headers=admin_headers)
        assert admin_list.status_code == 200
        assert admin_list.json()["meta"] == {"limit": 50, "next_cursor": None}

        assigned_headers = login(client, "assigned@example.test")
        assigned_list = client.get("/api/v1/interviews", headers=assigned_headers)
        assert assigned_list.status_code == 200
        assert assigned_list.json()["meta"] == {"limit": 50, "next_cursor": None}

        outsider_headers = login(client, "unassigned@example.test")
        outsider_list = client.get("/api/v1/interviews", headers=outsider_headers)
        assert outsider_list.status_code == 200
        assert outsider_list.json() == {"data": [], "meta": {"limit": 50, "next_cursor": None}}

        admin_headers = login(client, "interview-admin@example.test")
        assert client.post(
            f"/api/v1/interviews/{first_id}/transitions",
            json={"target": "confirmed"},
            headers={**admin_headers, "If-Match": '"1"', "Idempotency-Key": "scope-confirm"},
        ).status_code == 200
        assert client.post(
            f"/api/v1/interviews/{first_id}/transitions",
            json={"target": "completed"},
            headers={**admin_headers, "If-Match": '"2"', "Idempotency-Key": "scope-complete"},
        ).status_code == 200

        assigned_headers = login(client, "assigned@example.test")
        tasks = client.get("/api/v1/me/tasks", headers=assigned_headers)
        assert tasks.status_code == 200
        feedback_task = next(item for item in tasks.json()["data"] if item["type"] == "interview_feedback")
        assert feedback_task["interview_id"] == first_id
        assert feedback_task["candidate"]["display_name"] == "李嘉明"
