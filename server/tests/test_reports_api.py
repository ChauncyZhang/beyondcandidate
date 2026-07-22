import importlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from io import BytesIO
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from server.app.core.settings import Settings
from server.app.identity.models import AuditLog, Job, JobCollaborator, User, UserRole
from server.app.interviews.models import Interview, InterviewFeedback, InterviewParticipant
from server.app.main import create_app
from server.app.queue.models import BackgroundJob
from server.app.recruiting.models import (
    Application,
    ApplicationStageEvent,
    Candidate,
    FileObject,
    JobJdVersion,
    Resume,
    ScreeningRuleVersion,
)
from server.app.screening.models import ScreeningItem, ScreeningResult, ScreeningRun
from server.tests.test_recruiting_api import login, seed_user


NOW = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)


class Probe:
    async def check(self) -> None:
        pass


class FixedClock:
    def current_time(self) -> datetime:
        return NOW


@dataclass
class FakeExportStorage:
    objects: dict[str, bytes] = field(default_factory=dict)
    read_count: int = 0

    def write(self, storage_key: str, content: bytes, content_type: str) -> None:
        assert storage_key.startswith("exports/")
        assert content_type == "text/csv; charset=utf-8"
        self.objects[storage_key] = content

    def open_download(self, storage_key: str, max_bytes: int):
        self.read_count += 1
        content = self.objects[storage_key]
        assert len(content) <= max_bytes
        return BytesIO(content)


def make_app(tmp_path):
    app = create_app(
        settings=Settings(
            environment="test",
            database_url=f"sqlite+aiosqlite:///{tmp_path / 'reports-api.db'}",
            cors_origins=["https://hr.example.test"],
        ),
        database_probe=Probe(),
        storage_probe=Probe(),
        initialize_identity_schema=True,
        clock=FixedClock(),
    )
    app.state.export_storage = FakeExportStorage()
    app.state.identity_store.create_schema()
    return app


def _seed_job_facts(app, role: str = "recruiter"):
    user_id = seed_user(app, role, f"{role}@reports.test")
    with app.state.identity_store.sync_session() as db:
        user = db.get(User, user_id)
        allowed = Job(organization_id=user.organization_id, title="Allowed", owner_id=user_id, status="open")
        empty = Job(organization_id=user.organization_id, title="Empty", owner_id=user_id, status="open")
        denied = Job(organization_id=user.organization_id, title="Denied", owner_id=user_id, status="open")
        candidates = [
            Candidate(organization_id=user.organization_id, display_name=name, owner_id=user_id)
            for name in ("Allowed new", "Allowed review", "Denied candidate")
        ]
        files = [
            FileObject(
                organization_id=user.organization_id,
                storage_key=f"private/{index}",
                original_filename=f"resume-{index}.pdf",
                mime_type="application/pdf",
                size_bytes=10,
                sha256=str(index) * 64,
                uploaded_by=user_id,
            )
            for index in (1, 2, 3)
        ]
        db.add_all([allowed, empty, denied, *candidates, *files])
        db.flush()
        resumes = [
            Resume(
                organization_id=user.organization_id,
                candidate_id=candidate.id,
                file_object_id=file.id,
                version_number=1,
                parsed_text="private resume text",
            )
            for candidate, file in zip(candidates, files)
        ]
        db.add_all(resumes)
        db.flush()
        applications = [
            Application(
                organization_id=user.organization_id,
                candidate_id=candidate.id,
                job_id=job.id,
                resume_id=resume.id,
                owner_id=user_id,
                stage=stage,
                created_at=NOW - timedelta(hours=10),
                updated_at=NOW - timedelta(hours=8) if stage == "review" else NOW,
            )
            for candidate, job, resume, stage in zip(
                candidates,
                (allowed, allowed, denied),
                resumes,
                ("new", "review", "hired"),
            )
        ]
        db.add_all(applications)
        db.flush()
        db.add(
            ApplicationStageEvent(
                organization_id=user.organization_id,
                application_id=applications[1].id,
                actor_user_id=user_id,
                event_type="application.stage_changed",
                payload={"from_stage": "new", "to_stage": "review"},
                created_at=NOW - timedelta(hours=8),
            )
        )
        grant_role = "job_manager" if role == "hiring_manager" else "job_recruiter"
        db.add_all(
            [
                JobCollaborator(
                    organization_id=user.organization_id,
                    job_id=job.id,
                    user_id=user_id,
                    access_role=grant_role,
                )
                for job in (allowed, empty)
            ]
        )
        db.commit()
        return {
            "user_id": user_id,
            "organization_id": user.organization_id,
            "allowed_job_id": allowed.id,
            "empty_job_id": empty.id,
            "denied_job_id": denied.id,
            "allowed_application_id": applications[0].id,
            "denied_application_id": applications[2].id,
            "file_ids": [file.id for file in files],
        }


def _seed_quality_and_interviews(app, seed):
    second_interviewer_id = seed_user(app, "interviewer", "second-interviewer@reports.test")
    with app.state.identity_store.sync_session() as db:
        organization_id = seed["organization_id"]
        user_id = seed["user_id"]
        application = db.get(Application, seed["allowed_application_id"])
        denied_application = db.get(Application, seed["denied_application_id"])

        def add_screening(job_id, application_row, file_ids, llm_statuses, recommendation):
            jd = JobJdVersion(
                organization_id=organization_id,
                job_id=job_id,
                version_number=1,
                content={"text": "JD"},
                created_by=user_id,
            )
            rule = ScreeningRuleVersion(
                organization_id=organization_id,
                job_id=job_id,
                version_number=1,
                content={},
                created_by=user_id,
            )
            db.add_all([jd, rule])
            db.flush()
            run = ScreeningRun(
                organization_id=organization_id,
                job_id=job_id,
                jd_version_id=jd.id,
                rule_version_id=rule.id,
                source="upload",
                status="completed",
                total_count=len(file_ids),
                processed_count=len(file_ids),
                succeeded_count=1,
                failed_count=len(file_ids) - 1,
                created_by=user_id,
                created_at=NOW - timedelta(days=1),
            )
            db.add(run)
            db.flush()
            for index, (file_id, llm_status) in enumerate(zip(file_ids, llm_statuses)):
                item = ScreeningItem(
                    organization_id=organization_id,
                    run_id=run.id,
                    file_object_id=file_id,
                    candidate_id=application_row.candidate_id if index == 0 else None,
                    resume_id=application_row.resume_id if index == 0 else None,
                    application_id=application_row.id if index == 0 else None,
                    status="scored" if index == 0 else "failed",
                    attempts=1,
                    llm_status=llm_status,
                    llm_attempts=1,
                )
                db.add(item)
                db.flush()
                if index == 0:
                    db.add(
                        ScreeningResult(
                            organization_id=organization_id,
                            item_id=item.id,
                            application_id=application_row.id,
                            resume_id=application_row.resume_id,
                            rule_engine_version="rules-v1",
                            rule_score=80,
                            recommendation=recommendation,
                            required_hits=[],
                            required_missing=[],
                            bonus_hits=[],
                            estimated_years=0,
                            risks=[],
                            questions=[],
                        )
                    )

        add_screening(
            seed["allowed_job_id"], application, seed["file_ids"][:2], ("succeeded", "failed"), "可沟通"
        )
        add_screening(seed["denied_job_id"], denied_application, seed["file_ids"][2:], ("succeeded",), "暂缓")

        allowed_interview = Interview(
            organization_id=organization_id,
            application_id=application.id,
            round_name="Round 1",
            method="video",
            timezone="Asia/Shanghai",
            starts_at=NOW - timedelta(hours=4),
            ends_at=NOW - timedelta(hours=3),
            status="pending_feedback",
            owner_id=user_id,
            created_by=user_id,
            calendar_organizer={},
            calendar_attendees=[],
        )
        denied_interview = Interview(
            organization_id=organization_id,
            application_id=denied_application.id,
            round_name="Denied round",
            method="video",
            timezone="Asia/Shanghai",
            starts_at=NOW - timedelta(hours=10),
            ends_at=NOW - timedelta(hours=9),
            status="feedback_completed",
            owner_id=user_id,
            created_by=user_id,
            calendar_organizer={},
            calendar_attendees=[],
        )
        db.add_all([allowed_interview, denied_interview])
        db.flush()
        participants = [
            InterviewParticipant(
                organization_id=organization_id,
                interview_id=allowed_interview.id,
                user_id=user_id,
                role="interviewer",
                required_feedback=True,
            ),
            InterviewParticipant(
                organization_id=organization_id,
                interview_id=allowed_interview.id,
                user_id=second_interviewer_id,
                role="interviewer",
                required_feedback=True,
            ),
            InterviewParticipant(
                organization_id=organization_id,
                interview_id=denied_interview.id,
                user_id=user_id,
                role="interviewer",
                required_feedback=True,
            ),
        ]
        db.add_all(participants)
        db.flush()
        db.add_all(
            [
                InterviewFeedback(
                    organization_id=organization_id,
                    interview_id=allowed_interview.id,
                    author_id=user_id,
                    status="submitted",
                    ratings={},
                    submitted_at=NOW - timedelta(hours=1),
                ),
                InterviewFeedback(
                    organization_id=organization_id,
                    interview_id=denied_interview.id,
                    author_id=user_id,
                    status="submitted",
                    ratings={},
                    submitted_at=NOW - timedelta(hours=8),
                ),
            ]
        )
        db.commit()


def test_reports_openapi_registers_api_without_sensitive_fields(tmp_path) -> None:
    app = make_app(tmp_path)
    with TestClient(app) as client:
        schema = client.get("/openapi.json").json()
    expected = {
        "/api/v1/reports/recruiting-funnel": {"get"},
        "/api/v1/reports/screening-quality": {"get"},
        "/api/v1/exports": {"post"},
        "/api/v1/exports/{export_id}": {"get"},
        "/api/v1/exports/{export_id}/download-tickets": {"post"},
        "/api/v1/export-download-tickets/consume": {"post"},
    }
    assert {path: set(schema["paths"].get(path, {})) for path in expected} == expected
    rendered = str(schema).casefold()
    for forbidden in ("object_key", "storage_key", "token_hash", "parsed_text", "ciphertext", "lookup_hash"):
        assert forbidden not in rendered


def test_authorized_funnel_counts_applications_and_stage_time_without_denied_jobs(tmp_path) -> None:
    app = make_app(tmp_path)
    seed = _seed_job_facts(app)
    with TestClient(app) as client:
        response = client.get(
            f"/api/v1/reports/recruiting-funnel?job_id={seed['allowed_job_id']}",
            headers=login(client, "recruiter@reports.test"),
        )
    assert response.status_code == 200
    assert response.json() == {
        "data": {
            "total_applications": 2,
            "can_export": True,
            "stages": [
                {"stage": "new", "current_count": 1, "average_time_in_stage_seconds": 21600.0},
                {"stage": "review", "current_count": 1, "average_time_in_stage_seconds": 28800.0},
            ],
            "interviews": {
                "count": 0,
                "required_feedback_completed": 0,
                "required_feedback_total": 0,
                "required_feedback_completion_rate": 0.0,
                "average_feedback_turnaround_seconds": 0.0,
            },
        }
    }
    assert "hired" not in response.text


def test_screening_and_interview_metrics_are_scoped_and_zero_safe(tmp_path) -> None:
    app = make_app(tmp_path)
    seed = _seed_job_facts(app)
    _seed_quality_and_interviews(app, seed)
    with TestClient(app) as client:
        headers = login(client, "recruiter@reports.test")
        quality = client.get(
            f"/api/v1/reports/screening-quality?job_id={seed['allowed_job_id']}", headers=headers
        )
        funnel = client.get(
            f"/api/v1/reports/recruiting-funnel?job_id={seed['allowed_job_id']}", headers=headers
        )
        empty = client.get(
            f"/api/v1/reports/screening-quality?job_id={seed['empty_job_id']}", headers=headers
        )
    assert quality.status_code == 200
    assert quality.json()["data"] == {
        "resume_parsing": {"succeeded": 1, "total": 2, "success_rate": 0.5},
        "rule_screening": {"passed": 1, "total": 1, "pass_rate": 1.0},
        "llm": {"succeeded": 1, "total": 2, "success_rate": 0.5},
    }
    assert funnel.json()["data"]["interviews"] == {
        "count": 1,
        "required_feedback_completed": 1,
        "required_feedback_total": 2,
        "required_feedback_completion_rate": 0.5,
        "average_feedback_turnaround_seconds": 7200.0,
    }
    assert empty.json()["data"] == {
        "resume_parsing": {"succeeded": 0, "total": 0, "success_rate": 0.0},
        "rule_screening": {"passed": 0, "total": 0, "pass_rate": 0.0},
        "llm": {"succeeded": 0, "total": 0, "success_rate": 0.0},
    }


def test_hiring_manager_reads_only_granted_reports_and_cannot_export(tmp_path) -> None:
    app = make_app(tmp_path)
    seed = _seed_job_facts(app, role="hiring_manager")
    with TestClient(app) as client:
        headers = login(client, "hiring_manager@reports.test")
        allowed = client.get(
            f"/api/v1/reports/recruiting-funnel?job_id={seed['allowed_job_id']}", headers=headers
        )
        denied = client.get(
            f"/api/v1/reports/recruiting-funnel?job_id={seed['denied_job_id']}", headers=headers
        )
        export = client.post(
            "/api/v1/exports",
            json={"job_id": str(seed["allowed_job_id"])},
            headers={"Idempotency-Key": "manager-export", **headers},
        )
    assert allowed.status_code == 200
    assert allowed.json()["data"]["can_export"] is False
    assert denied.status_code == export.status_code == 404
    assert denied.json()["code"] == export.json()["code"] == "resource_not_found"


def test_mixed_recruiter_manager_role_cannot_export_a_manager_only_job(tmp_path) -> None:
    app = make_app(tmp_path)
    seed = _seed_job_facts(app)
    with app.state.identity_store.sync_session() as db:
        user = db.get(User, seed["user_id"])
        user.roles.append(UserRole(role="hiring_manager"))
        grants = db.scalars(select(JobCollaborator).where(JobCollaborator.user_id == user.id)).all()
        for grant in grants:
            grant.access_role = "job_manager"
        db.commit()

    with TestClient(app) as client:
        headers = login(client, "recruiter@reports.test")
        report = client.get(
            f"/api/v1/reports/recruiting-funnel?job_id={seed['allowed_job_id']}", headers=headers
        )
        export = client.post(
            "/api/v1/exports",
            json={"job_id": str(seed["allowed_job_id"])},
            headers={"Idempotency-Key": "manager-only-export", **headers},
        )

    assert report.status_code == 200
    assert report.json()["data"]["can_export"] is False
    assert export.status_code == 404


@pytest.mark.parametrize("prefix", ["=", "+", "-", "@", "\t", "\r"])
def test_csv_cells_neutralize_every_dangerous_prefix(prefix) -> None:
    csv_module = importlib.import_module("server.app.reports.csv_export")
    assert csv_module.sanitize_csv_cell(prefix + "payload") == "'" + prefix + "payload"


def test_export_is_persistent_audited_idempotent_and_downloaded_through_one_time_ticket(tmp_path) -> None:
    app = make_app(tmp_path)
    seed = _seed_job_facts(app)
    headers = {"Idempotency-Key": "reports-export-1"}
    with TestClient(app) as client:
        headers.update(login(client, "recruiter@reports.test"))
        first = client.post(
            "/api/v1/exports", json={"job_id": str(seed["allowed_job_id"])}, headers=headers
        )
        replay = client.post(
            "/api/v1/exports", json={"job_id": str(seed["allowed_job_id"])}, headers=headers
        )
        assert first.status_code == replay.status_code == 201
        assert first.json() == replay.json()
        export_id = first.json()["data"]["id"]

        reports_models = importlib.import_module("server.app.reports.models")
        reports_service = importlib.import_module("server.app.reports.service")
        with app.state.identity_store.sync_session() as db:
            export = db.get(reports_models.ExportRecord, UUID(export_id))
            assert export is not None
            assert db.scalar(select(BackgroundJob).where(BackgroundJob.id == export.background_job_id)) is not None
            audits = db.scalars(
                select(AuditLog).where(AuditLog.event_type == "report_export.created")
            ).all()
            assert len(audits) == 1
            assert set(audits[0].metadata_json) == {"export_id", "job_count"}
            reports_service.generate_export(db, export.id, app.state.export_storage)
            db.commit()

        status = client.get(f"/api/v1/exports/{export_id}", headers=headers)
        ticket = client.post(f"/api/v1/exports/{export_id}/download-tickets", headers=headers)
        assert status.status_code == 200
        assert status.json()["data"]["status"] == "succeeded"
        assert "object" not in status.text.casefold()
        assert ticket.status_code == 201
        download = client.post(
            "/api/v1/export-download-tickets/consume",
            json={"token": ticket.json()["data"]["token"]},
            headers=headers,
        )
        replay_download = client.post(
            "/api/v1/export-download-tickets/consume",
            json={"token": ticket.json()["data"]["token"]},
            headers=headers,
        )
    assert download.status_code == 200
    assert download.headers["content-type"].startswith("text/csv")
    assert "Denied candidate" not in download.text
    assert "private resume text" not in download.text
    assert replay_download.status_code == 404
    assert app.state.export_storage.read_count == 1
    with app.state.identity_store.sync_session() as db:
        audits = db.scalars(select(AuditLog).where(AuditLog.event_type.like("report_export.%"))).all()
        assert [audit.event_type for audit in audits] == ["report_export.created", "report_export.downloaded"]
        forbidden = {"contact", "resume", "row", "object", "storage", "session", "provider", "secret"}
        for audit in audits:
            rendered = str(audit.metadata_json).casefold()
            assert not any(value in rendered for value in forbidden)


def test_expired_export_ticket_is_rejected_before_private_storage_read(tmp_path) -> None:
    app = make_app(tmp_path)
    seed = _seed_job_facts(app)
    with TestClient(app) as client:
        headers = {"Idempotency-Key": "reports-expired-ticket", **login(client, "recruiter@reports.test")}
        created = client.post(
            "/api/v1/exports", json={"job_id": str(seed["allowed_job_id"])}, headers=headers
        )
        export_id = UUID(created.json()["data"]["id"])
        reports_models = importlib.import_module("server.app.reports.models")
        reports_service = importlib.import_module("server.app.reports.service")
        with app.state.identity_store.sync_session() as db:
            export = db.get(reports_models.ExportRecord, export_id)
            reports_service.generate_export(db, export.id, app.state.export_storage)
            db.commit()
        issued = client.post(f"/api/v1/exports/{export_id}/download-tickets", headers=headers)
        token = issued.json()["data"]["token"]
        with app.state.identity_store.sync_session() as db:
            ticket = db.scalar(select(reports_models.ExportDownloadTicket))
            ticket.expires_at = NOW - timedelta(seconds=1)
            db.commit()

        denied = client.post(
            "/api/v1/export-download-tickets/consume", json={"token": token}, headers=headers
        )

    assert denied.status_code == 404
    assert app.state.export_storage.read_count == 0


def test_export_generation_rejects_row_limit_before_storage_write(tmp_path, monkeypatch) -> None:
    app = make_app(tmp_path)
    seed = _seed_job_facts(app)
    reports_models = importlib.import_module("server.app.reports.models")
    reports_service = importlib.import_module("server.app.reports.service")
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/exports",
            json={"job_id": str(seed["allowed_job_id"])},
            headers={"Idempotency-Key": "reports-row-limit", **login(client, "recruiter@reports.test")},
        )
    export_id = UUID(response.json()["data"]["id"])
    monkeypatch.setattr(reports_service, "MAX_EXPORT_ROWS", 1)

    with app.state.identity_store.sync_session() as db:
        with pytest.raises(reports_service.ExportLimitExceeded):
            reports_service.generate_export(db, export_id, app.state.export_storage)

    assert app.state.export_storage.objects == {}


def test_export_generation_rejects_byte_limit_before_storage_write(tmp_path, monkeypatch) -> None:
    app = make_app(tmp_path)
    seed = _seed_job_facts(app)
    reports_service = importlib.import_module("server.app.reports.service")
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/exports",
            json={"job_id": str(seed["allowed_job_id"])},
            headers={"Idempotency-Key": "reports-byte-limit", **login(client, "recruiter@reports.test")},
        )
    export_id = UUID(response.json()["data"]["id"])
    monkeypatch.setattr(reports_service, "MAX_EXPORT_BYTES", 16)

    with app.state.identity_store.sync_session() as db:
        with pytest.raises(reports_service.ExportLimitExceeded):
            reports_service.generate_export(db, export_id, app.state.export_storage)

    assert app.state.export_storage.objects == {}


@pytest.mark.parametrize("role", ["interviewer", "system_admin"])
def test_report_and_export_denials_do_not_reveal_job_or_export_existence(tmp_path, role) -> None:
    app = make_app(tmp_path)
    seed = _seed_job_facts(app)
    seed_user(app, role, f"{role}@denied.test")
    with TestClient(app) as client:
        owner_headers = {"Idempotency-Key": "owner-export", **login(client, "recruiter@reports.test")}
        created = client.post(
            "/api/v1/exports", json={"job_id": str(seed["allowed_job_id"])}, headers=owner_headers
        )
        export_id = created.json()["data"]["id"]
        denied_headers = {"Idempotency-Key": "denied-export", **login(client, f"{role}@denied.test")}
        responses = [
            client.get(
                f"/api/v1/reports/recruiting-funnel?job_id={seed['allowed_job_id']}",
                headers=denied_headers,
            ),
            client.get(
                "/api/v1/reports/recruiting-funnel?job_id=00000000-0000-0000-0000-000000000000",
                headers=denied_headers,
            ),
            client.post(
                "/api/v1/exports", json={"job_id": str(seed["allowed_job_id"])}, headers=denied_headers
            ),
            client.get(f"/api/v1/exports/{export_id}", headers=denied_headers),
            client.get(
                "/api/v1/exports/00000000-0000-0000-0000-000000000000", headers=denied_headers
            ),
        ]
    assert {(response.status_code, response.json()["code"]) for response in responses} == {
        (404, "resource_not_found")
    }
