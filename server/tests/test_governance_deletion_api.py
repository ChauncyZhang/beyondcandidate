import re
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from server.app.core.settings import Settings
from server.app.governance import deletion_service
from server.app.governance.deletion_service import build_private_manifest, canonical_manifest_hash
from server.app.governance.deletion_models import DeletionArtifact, DeletionRequest, LegalHold
from server.app.identity.models import AuditLog, Job, JobCollaborator, Organization, User, UserRole
from server.app.identity.policy import Principal
from server.app.identity.security import PasswordService
from server.app.interviews.models import Interview, InterviewFeedback, InterviewParticipant
from server.app.main import create_app
from server.app.queue.models import BackgroundJob
from server.app.queue.payloads import DEFAULT_PAYLOAD_POLICIES, UnsafePayload
from server.app.queue.repository import TERMINAL_CALLBACK_TYPES
from server.app.recruiting.models import (
    Application,
    ApplicationReviewTask,
    ApplicationStageEvent,
    Candidate,
    CandidateContact,
    FileObject,
    IdempotencyRecord,
    Resume,
)
from server.tests.test_recruiting_api import login, seed_user


ORIGIN = "https://hr.example.test"
SENSITIVE = re.compile(
    r"candidate_id|organization_id|storage_key|manifest_hash|display_name|email|phone|"
    r"parsed_text|original_filename|meeting_url|feedback_text|credential|idempotency",
    re.IGNORECASE,
)


class Probe:
    async def check(self) -> None:
        pass


class FixedClock:
    def current_time(self) -> datetime:
        return datetime(2026, 7, 15, 8, 0, tzinfo=timezone.utc)


def make_app(tmp_path):
    app = create_app(
        settings=Settings(
            environment="test",
            database_url=f"sqlite+aiosqlite:///{tmp_path / 'deletion-api.db'}",
            cors_origins=[ORIGIN],
        ),
        database_probe=Probe(),
        storage_probe=Probe(),
        initialize_identity_schema=True,
        clock=FixedClock(),
    )
    app.state.identity_store.create_schema()
    return app


def candidate_for(app, owner_id):
    with app.state.identity_store.sync_session() as db:
        owner = db.get(User, owner_id)
        candidate = Candidate(
            organization_id=owner.organization_id,
            display_name="Never expose this candidate",
            current_title="Private title",
            owner_id=owner.id,
        )
        db.add(candidate)
        db.commit()
        return candidate.id


def request_deletion(client, headers, candidate_id, key="request-1", reason="candidate_request"):
    return client.post(
        f"/api/v1/candidates/{candidate_id}/deletion-requests",
        json={"reason_code": reason},
        headers={**headers, "Idempotency-Key": key},
    )


def approve(
    client,
    headers,
    request_id,
    version,
    key="approve-1",
    *,
    terminate_active_applications=False,
):
    return client.post(
        f"/api/v1/deletion-requests/{request_id}/transitions",
        json={
            "target_status": "approved",
            "terminate_active_applications": terminate_active_applications,
        },
        headers={**headers, "If-Match": f'"{version}"', "Idempotency-Key": key},
    )


def assert_problem(response, status, code):
    assert response.status_code == status
    assert response.json()["code"] == code
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["X-Trace-ID"]
    assert not SENSITIVE.search(response.json()["detail"])


def assert_private_values_absent(value, private_values):
    if isinstance(value, dict):
        for key, item in value.items():
            assert_private_values_absent(key, private_values)
            assert_private_values_absent(item, private_values)
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            assert_private_values_absent(item, private_values)
        return
    rendered = str(value)
    assert all(private not in rendered for private in private_values)


def test_openapi_and_queue_contract_are_exact(tmp_path) -> None:
    app = make_app(tmp_path)
    with TestClient(app) as client:
        paths = client.get("/openapi.json").json()["paths"]
    expected = {
        "/api/v1/candidates/{candidate_id}/deletion-requests",
        "/api/v1/deletion-requests",
        "/api/v1/deletion-requests/{request_id}",
        "/api/v1/deletion-requests/{request_id}/transitions",
        "/api/v1/candidates/{candidate_id}/legal-holds",
        "/api/v1/legal-holds/{hold_id}/releases",
        "/api/v1/candidates/{candidate_id}/governance-status",
    }
    assert expected <= set(paths)
    assert not any("recover" in path for path in paths)

    organization_id = str(uuid4())
    request_id = str(uuid4())
    payload = {
        "organization_id": organization_id,
        "deletion_request_id": request_id,
        "request_version": 2,
    }
    assert DEFAULT_PAYLOAD_POLICIES.validate_job("governance.delete_candidate", payload) == payload
    for invalid in (
        {**payload, "storage_key": "private/object"},
        {**payload, "request_version": 0},
        {"organization_id": organization_id, "deletion_request_id": request_id},
    ):
        with pytest.raises(UnsafePayload):
            DEFAULT_PAYLOAD_POLICIES.validate_job("governance.delete_candidate", invalid)
    assert "governance.delete_candidate" in TERMINAL_CALLBACK_TYPES
    assert "governance.retention_sweep" in TERMINAL_CALLBACK_TYPES
    assert "governance.redelete_after_restore" in TERMINAL_CALLBACK_TYPES


@pytest.mark.parametrize(
    ("role", "owns_candidate", "expected"),
    [
        ("recruiter", True, 201),
        ("recruiting_admin", False, 201),
        ("recruiter", False, 404),
        ("system_admin", True, 404),
        ("hiring_manager", True, 404),
        ("interviewer", True, 404),
    ],
)
def test_request_authorization_is_non_enumerating_for_every_role(
    tmp_path, role, owns_candidate, expected
) -> None:
    app = make_app(tmp_path)
    email = f"{role}-{str(owns_candidate).lower()}@deletion.test"
    actor_id = seed_user(app, role, email)
    owner_id = actor_id if owns_candidate else seed_user(app, "recruiter", f"owner-{role}@deletion.test")
    candidate_id = candidate_for(app, owner_id)
    with TestClient(app) as client:
        headers = login(client, email)
        response = request_deletion(client, headers, candidate_id)
    assert response.status_code == expected
    assert response.headers["Cache-Control"] == "no-store"
    if expected == 404:
        assert_problem(response, 404, "resource_not_found")


def test_request_is_replay_safe_conflict_safe_and_public_projection_is_recursive_safe(tmp_path) -> None:
    app = make_app(tmp_path)
    recruiter_id = seed_user(app, "recruiter", "requester@deletion.test")
    seed_user(app, "system_admin", "redaction-system@deletion.test")
    candidate_id = candidate_for(app, recruiter_id)
    with app.state.identity_store.sync_session() as db:
        candidate = db.get(Candidate, candidate_id)
        contact = CandidateContact(
            organization_id=candidate.organization_id,
            candidate_id=candidate.id,
            kind="email",
            ciphertext=b"UNIQUE_PRIVATE_CONTACT_CIPHERTEXT",
            lookup_hash="a" * 64,
            masked_value="unique-private-contact@example.test",
        )
        db.add(contact)
        file = FileObject(organization_id=candidate.organization_id, storage_key="private/unique-storage-locator", original_filename="unique-private-filename.pdf", mime_type="application/pdf", size_bytes=7, sha256="e" * 64, uploaded_by=recruiter_id)
        db.add(file); db.flush()
        resume = Resume(organization_id=candidate.organization_id, candidate_id=candidate.id, file_object_id=file.id, version_number=1, parsed_text="UNIQUE_PRIVATE_RESUME_TEXT")
        db.add(resume); db.flush()
        job = Job(
            organization_id=candidate.organization_id,
            title="UNIQUE_PRIVATE_JOB_TITLE",
            owner_id=recruiter_id,
            status="open",
        )
        db.add(job); db.flush()
        db.add(
            JobCollaborator(
                organization_id=candidate.organization_id,
                job_id=job.id,
                user_id=recruiter_id,
                access_role="job_recruiter",
            )
        )
        application = Application(
            organization_id=candidate.organization_id,
            candidate_id=candidate.id,
            job_id=job.id,
            resume_id=resume.id,
            owner_id=recruiter_id,
            stage="rejected",
            human_conclusion="UNIQUE_PRIVATE_APPLICATION_TEXT",
        )
        db.add(application); db.flush()
        interview = Interview(
            organization_id=candidate.organization_id,
            application_id=application.id,
            round_name="UNIQUE_PRIVATE_ROUND_NAME",
            method="video",
            timezone="UTC",
            starts_at=FixedClock().current_time(),
            ends_at=FixedClock().current_time() + timedelta(hours=1),
            location="UNIQUE_PRIVATE_INTERVIEW_LOCATION",
            meeting_url="https://unique-private-meeting.example.test/secret",
            status="feedback_completed",
            owner_id=recruiter_id,
            created_by=recruiter_id,
            calendar_organizer={"credential": "UNIQUE_PRIVATE_CREDENTIAL"},
        )
        db.add(interview); db.flush()
        participant = InterviewParticipant(
            organization_id=candidate.organization_id,
            interview_id=interview.id,
            user_id=recruiter_id,
            task_status="completed",
        )
        db.add(participant); db.flush()
        feedback = InterviewFeedback(
            organization_id=candidate.organization_id,
            interview_id=interview.id,
            author_id=recruiter_id,
            status="submitted",
            ratings={"private_dimension": "UNIQUE_PRIVATE_RATING"},
            strengths="UNIQUE_PRIVATE_FEEDBACK_STRENGTH",
            risks="UNIQUE_PRIVATE_FEEDBACK_RISK",
            notes="UNIQUE_PRIVATE_FEEDBACK_NOTE",
            conclusion="recommend",
            submitted_at=FixedClock().current_time(),
        )
        db.add(feedback)
        db.commit()
        private_values = [
            str(candidate.id), str(contact.id), str(file.id), str(resume.id),
            str(application.id), str(interview.id), str(participant.id), str(feedback.id),
            "Never expose this candidate", "Private title",
            "UNIQUE_PRIVATE_CONTACT_CIPHERTEXT", "a" * 64,
            "unique-private-contact@example.test", "private/unique-storage-locator",
            "unique-private-filename.pdf", "UNIQUE_PRIVATE_RESUME_TEXT",
            "UNIQUE_PRIVATE_JOB_TITLE", "UNIQUE_PRIVATE_APPLICATION_TEXT",
            "UNIQUE_PRIVATE_ROUND_NAME", "UNIQUE_PRIVATE_INTERVIEW_LOCATION",
            "https://unique-private-meeting.example.test/secret",
            "UNIQUE_PRIVATE_CREDENTIAL", "UNIQUE_PRIVATE_RATING",
            "UNIQUE_PRIVATE_FEEDBACK_STRENGTH", "UNIQUE_PRIVATE_FEEDBACK_RISK",
            "UNIQUE_PRIVATE_FEEDBACK_NOTE",
        ]

    with TestClient(app) as client:
        headers = login(client, "requester@deletion.test")
        missing = client.post(
            f"/api/v1/candidates/{candidate_id}/deletion-requests",
            json={"reason_code": "candidate_request"},
            headers=headers,
        )
        assert_problem(missing, 428, "idempotency_key_required")
        forbidden_reason = request_deletion(client, headers, candidate_id, "retention", "retention_expired")
        assert_problem(forbidden_reason, 422, "validation_failed")

        first = request_deletion(client, headers, candidate_id)
        replay = request_deletion(client, headers, candidate_id)
        conflict = request_deletion(client, headers, candidate_id, reason="administrator_request")
        second = request_deletion(client, headers, candidate_id, key="request-2")
        with app.state.identity_store.sync_session() as db:
            for event in db.scalars(select(AuditLog).where(AuditLog.category == "governance")):
                event.created_at = FixedClock().current_time() - timedelta(seconds=1)
            db.commit()
        system = login(client, "redaction-system@deletion.test")
        audit_response = client.get("/api/v1/audit-logs", headers=system)

    assert first.status_code == replay.status_code == 201
    assert first.json() == replay.json()
    assert first.headers["ETag"] == replay.headers["ETag"] == '"1"'
    assert_problem(conflict, 409, "idempotency_conflict")
    assert_problem(second, 409, "deletion_request_open")
    data = first.json()["data"]
    assert set(data) == {
        "id", "status", "version", "reason_code", "requested_at", "approved_at",
        "safe_error_code", "active_application_count", "impact",
    }
    assert set(data["impact"]["counts"]) == {
        "contacts", "resumes", "applications", "screening_records", "interviews",
        "feedback_records", "talent_memberships", "resume_objects", "temporary_exports",
    }
    assert data["impact"]["counts"]["contacts"] == 1
    assert not SENSITIVE.search(str(first.json()))
    for surface in (
        first.json(), replay.json(), conflict.json(), second.json(), audit_response.json()
    ):
        assert_private_values_absent(surface, private_values)
    assert audit_response.status_code == 200
    assert any(item["event_type"] == "governance.deletion_requested" for item in audit_response.json()["data"])
    with app.state.identity_store.sync_session() as db:
        assert db.scalar(select(func.count()).select_from(DeletionArtifact)) == 0


def test_create_idempotency_key_is_bound_to_candidate(tmp_path) -> None:
    app = make_app(tmp_path)
    recruiter_id = seed_user(app, "recruiter", "bound@deletion.test")
    first_id = candidate_for(app, recruiter_id)
    second_id = candidate_for(app, recruiter_id)
    with TestClient(app) as client:
        headers = login(client, "bound@deletion.test")
        first = request_deletion(client, headers, first_id, "shared-key")
        second = request_deletion(client, headers, second_id, "shared-key")
    assert first.status_code == 201
    assert_problem(second, 409, "idempotency_conflict")
    with app.state.identity_store.sync_session() as db:
        assert db.scalar(select(func.count()).select_from(DeletionRequest)) == 1


def test_list_read_cursor_and_requester_scope(tmp_path) -> None:
    app = make_app(tmp_path)
    requester_id = seed_user(app, "recruiter", "reader@deletion.test")
    other_id = seed_user(app, "recruiter", "other@deletion.test")
    admin_id = seed_user(app, "system_admin", "system@deletion.test")
    candidate_id = candidate_for(app, requester_id)
    requester_candidate_2 = candidate_for(app, requester_id)
    other_candidate_id = candidate_for(app, other_id)
    with TestClient(app) as client:
        requester = login(client, "reader@deletion.test")
        first = request_deletion(client, requester, candidate_id)
        request_deletion(client, requester, requester_candidate_2, key="reader-second")
        other = login(client, "other@deletion.test")
        second = request_deletion(client, other, other_candidate_id, key="other-request")
        system = login(client, "system@deletion.test")

        page = client.get("/api/v1/deletion-requests?limit=1", headers=system)
        assert page.status_code == 200
        assert len(page.json()["data"]) == 1
        assert page.json()["meta"]["next_cursor"]
        next_page = client.get(
            "/api/v1/deletion-requests",
            params={"limit": 1, "cursor": page.json()["meta"]["next_cursor"]},
            headers=system,
        )
        assert next_page.status_code == 200
        assert next_page.json()["data"][0]["id"] != page.json()["data"][0]["id"]
        assert_problem(
            client.get("/api/v1/deletion-requests?limit=101", headers=system),
            422,
            "validation_failed",
        )
        assert_problem(
            client.get("/api/v1/deletion-requests?status=unknown", headers=system),
            422,
            "validation_failed",
        )
        assert_problem(
            client.get("/api/v1/deletion-requests?cursor=not-signed", headers=system),
            422,
            "validation_failed",
        )

        requester = login(client, "reader@deletion.test")
        own = client.get(f"/api/v1/deletion-requests/{first.json()['data']['id']}", headers=requester)
        denied = client.get(f"/api/v1/deletion-requests/{second.json()['data']['id']}", headers=requester)
        assert own.status_code == 200
        assert_problem(denied, 404, "resource_not_found")
        requester_page = client.get("/api/v1/deletion-requests?limit=1", headers=requester)
        requester_cursor = requester_page.json()["meta"]["next_cursor"]
        assert requester_cursor
        other = login(client, "other@deletion.test")
        assert_problem(
            client.get(
                "/api/v1/deletion-requests",
                params={"limit": 1, "cursor": requester_cursor},
                headers=other,
            ),
            422,
            "validation_failed",
        )


def test_approval_requires_current_preconditions_and_enqueues_once(tmp_path) -> None:
    app = make_app(tmp_path)
    requester_id = seed_user(app, "recruiter", "approval-requester@deletion.test")
    seed_user(app, "system_admin", "approver@deletion.test")
    candidate_id = candidate_for(app, requester_id)
    with TestClient(app) as client:
        requester = login(client, "approval-requester@deletion.test")
        created = request_deletion(client, requester, candidate_id)
        request_id = created.json()["data"]["id"]
        assert_problem(
            client.post(
                f"/api/v1/deletion-requests/{request_id}/transitions",
                json={"target_status": "approved"},
                headers={**requester, "If-Match": '"1"', "Idempotency-Key": "self"},
            ),
            404,
            "resource_not_found",
        )
        approver = login(client, "approver@deletion.test")
        assert_problem(
            client.post(
                f"/api/v1/deletion-requests/{request_id}/transitions",
                json={"target_status": "approved"},
                headers={**approver, "Idempotency-Key": "missing-match"},
            ),
            428,
            "precondition_required",
        )
        assert_problem(approve(client, approver, request_id, 0, "malformed"), 422, "validation_failed")
        assert_problem(approve(client, approver, request_id, 2, "stale"), 409, "resource_version_conflict")
        approved = approve(client, approver, request_id, 1)
        replay = approve(client, approver, request_id, 1)
        different_precondition = approve(client, approver, request_id, 2)
    assert approved.status_code == replay.status_code == 200
    assert approved.json() == replay.json()
    assert approved.json()["data"]["status"] == "approved"
    assert approved.headers["ETag"] == '"2"'
    assert_problem(different_precondition, 409, "idempotency_conflict")
    with app.state.identity_store.sync_session() as db:
        record = db.scalar(
            select(IdempotencyRecord).where(
                IdempotencyRecord.operation == "governance.deletion_request.approve",
                IdempotencyRecord.idempotency_key == "approve-1",
            )
        )
        record.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.commit()
    with TestClient(app) as client:
        approver = login(client, "approver@deletion.test")
        expired = approve(client, approver, request_id, 2)
    assert_problem(expired, 409, "invalid_deletion_state_transition")
    with app.state.identity_store.sync_session() as db:
        jobs = list(db.scalars(select(BackgroundJob).where(BackgroundJob.type == "governance.delete_candidate")))
        assert len(jobs) == 1
        assert jobs[0].payload == {
            "organization_id": str(jobs[0].organization_id),
            "deletion_request_id": request_id,
            "request_version": 2,
        }


def test_active_applications_require_explicit_termination_and_are_withdrawn_atomically(
    tmp_path,
) -> None:
    app = make_app(tmp_path)
    requester_id = seed_user(app, "recruiter", "active-requester@deletion.test")
    seed_user(app, "system_admin", "active-approver@deletion.test")
    candidate_id = candidate_for(app, requester_id)
    with app.state.identity_store.sync_session() as db:
        candidate = db.get(Candidate, candidate_id)
        file = FileObject(
            organization_id=candidate.organization_id,
            storage_key="private/active-deletion-resume",
            original_filename="active.pdf",
            mime_type="application/pdf",
            size_bytes=1,
            sha256="7" * 64,
            uploaded_by=requester_id,
        )
        db.add(file)
        db.flush()
        resume = Resume(
            organization_id=candidate.organization_id,
            candidate_id=candidate.id,
            file_object_id=file.id,
            version_number=1,
            parsed_text="active application resume",
        )
        job = Job(
            organization_id=candidate.organization_id,
            title="Active deletion job",
            owner_id=requester_id,
            status="open",
        )
        db.add_all([resume, job])
        db.flush()
        db.add(
            JobCollaborator(
                organization_id=candidate.organization_id,
                job_id=job.id,
                user_id=requester_id,
                access_role="job_recruiter",
            )
        )
        application = Application(
            organization_id=candidate.organization_id,
            candidate_id=candidate.id,
            job_id=job.id,
            resume_id=resume.id,
            owner_id=requester_id,
            stage="deferred",
        )
        db.add(application)
        db.flush()
        review_task = ApplicationReviewTask(
            organization_id=candidate.organization_id,
            application_id=application.id,
            assignee_id=requester_id,
            status="open",
            ai_status="succeeded",
        )
        db.add(review_task)
        db.commit()
        application_id = application.id
        review_task_id = review_task.id

    with TestClient(app) as client:
        requester = login(client, "active-requester@deletion.test")
        created = request_deletion(client, requester, candidate_id)
        request_id = created.json()["data"]["id"]
        approver = login(client, "active-approver@deletion.test")
        detail = client.get(f"/api/v1/deletion-requests/{request_id}", headers=approver)
        blocked = approve(client, approver, request_id, 1, "blocked-active")
        approved = approve(
            client,
            approver,
            request_id,
            1,
            "terminate-active",
            terminate_active_applications=True,
        )

    assert detail.status_code == 200
    assert detail.json()["data"]["active_application_count"] == 1
    assert_problem(blocked, 409, "active_application_exists")
    assert approved.status_code == 200
    assert approved.json()["data"]["status"] == "approved"
    assert approved.json()["data"]["active_application_count"] == 0
    with app.state.identity_store.sync_session() as db:
        application = db.get(Application, application_id)
        review_task = db.get(ApplicationReviewTask, review_task_id)
        assert application.stage == "withdrawn"
        assert review_task.status == "closed"
        assert db.scalar(
            select(func.count())
            .select_from(ApplicationStageEvent)
            .where(
                ApplicationStageEvent.application_id == application_id,
                ApplicationStageEvent.event_type == "application.stage_changed",
            )
        ) == 1
        approval_audit = db.scalar(
            select(AuditLog).where(
                AuditLog.event_type == "governance.deletion_approved",
                AuditLog.outcome == "success",
            )
        )
        assert approval_audit.metadata_json[
            "terminated_active_application_count"
        ] == 1
        assert db.scalar(
            select(func.count())
            .select_from(BackgroundJob)
            .where(BackgroundJob.type == "governance.delete_candidate")
        ) == 1


def test_stale_manifest_refreshes_without_enqueue_and_increments_version(tmp_path) -> None:
    app = make_app(tmp_path)
    requester_id = seed_user(app, "recruiter", "stale-requester@deletion.test")
    seed_user(app, "system_admin", "stale-approver@deletion.test")
    candidate_id = candidate_for(app, requester_id)
    with TestClient(app) as client:
        requester = login(client, "stale-requester@deletion.test")
        created = request_deletion(client, requester, candidate_id)
        request_id = created.json()["data"]["id"]
        with app.state.identity_store.sync_session() as db:
            candidate = db.get(Candidate, candidate_id)
            candidate.version += 1
            db.commit()
        approver = login(client, "stale-approver@deletion.test")
        stale = approve(client, approver, request_id, 1)
    assert_problem(stale, 409, "stale_manifest")
    assert stale.headers["ETag"] == '"2"'
    assert stale.json()["data"]["impact"]["candidate_version"] == 2
    with app.state.identity_store.sync_session() as db:
        row = db.get(DeletionRequest, UUID(request_id))
        assert row.status == "requested" and row.version == 2
        assert db.scalar(select(func.count()).select_from(BackgroundJob)) == 0


def test_removed_resume_stale_refresh_deletes_only_unstarted_artifacts(tmp_path) -> None:
    app = make_app(tmp_path)
    requester_id = seed_user(app, "recruiter", "artifact-requester@deletion.test")
    seed_user(app, "system_admin", "artifact-approver@deletion.test")
    candidate_id = candidate_for(app, requester_id)
    with app.state.identity_store.sync_session() as db:
        candidate = db.get(Candidate, candidate_id)
        file = FileObject(organization_id=candidate.organization_id, storage_key="private/obsolete-resume", original_filename="private-name.pdf", mime_type="application/pdf", size_bytes=4, sha256="f" * 64, uploaded_by=requester_id)
        db.add(file); db.flush()
        resume = Resume(organization_id=candidate.organization_id, candidate_id=candidate.id, file_object_id=file.id, version_number=1, parsed_text="private resume value")
        db.add(resume); db.commit()
    with TestClient(app) as client:
        requester = login(client, "artifact-requester@deletion.test")
        created = request_deletion(client, requester, candidate_id, "artifact-request")
        request_id = UUID(created.json()["data"]["id"])
        with app.state.identity_store.sync_session() as db:
            row = db.get(DeletionRequest, request_id)
            resume = db.scalar(select(Resume).where(Resume.candidate_id == candidate_id))
            db.delete(resume)
            db.add(DeletionArtifact(organization_id=row.organization_id, request_id=row.id, kind="resume_object", storage_key="private/obsolete-resume", status="pending", attempts=0))
            db.get(Candidate, candidate_id).version += 1
            db.commit()
        approver = login(client, "artifact-approver@deletion.test")
        refreshed = approve(client, approver, str(request_id), 1, "artifact-refresh")
    assert_problem(refreshed, 409, "stale_manifest")
    with app.state.identity_store.sync_session() as db:
        assert db.scalar(select(func.count()).select_from(DeletionArtifact)) == 0


def test_legal_hold_release_and_governance_status_redact_reason_by_role(
    tmp_path, monkeypatch
) -> None:
    app = make_app(tmp_path)
    recruiter_id = seed_user(app, "recruiter", "hold-reader@deletion.test")
    admin_id = seed_user(app, "recruiting_admin", "hold-admin@deletion.test")
    manager_id = seed_user(app, "hiring_manager", "hold-manager@deletion.test")
    system_id = seed_user(app, "system_admin", "hold-system@deletion.test")
    candidate_id = candidate_for(app, recruiter_id)
    with app.state.identity_store.sync_session() as db:
        recruiter_user = db.get(User, recruiter_id)
        organization_id = recruiter_user.organization_id
        job = Job(
            organization_id=organization_id,
            title="Manager-scoped hold job",
            owner_id=recruiter_id,
        )
        file = FileObject(
            organization_id=organization_id,
            storage_key="private/manager-scoped-hold-resume",
            original_filename="manager-scoped.pdf",
            mime_type="application/pdf",
            size_bytes=1,
            sha256="9" * 64,
            uploaded_by=recruiter_id,
        )
        db.add_all([job, file])
        db.flush()
        resume = Resume(
            organization_id=organization_id,
            candidate_id=candidate_id,
            file_object_id=file.id,
            version_number=1,
        )
        db.add(resume)
        db.flush()
        db.add_all(
            [
                Application(
                    organization_id=organization_id,
                    candidate_id=candidate_id,
                    job_id=job.id,
                    resume_id=resume.id,
                    owner_id=recruiter_id,
                ),
                JobCollaborator(
                    organization_id=organization_id,
                    job_id=job.id,
                    user_id=manager_id,
                    access_role="job_manager",
                ),
                JobCollaborator(
                    organization_id=organization_id,
                    job_id=job.id,
                    user_id=recruiter_id,
                    access_role="job_owner",
                ),
            ]
        )
        db.commit()
    with TestClient(app) as client:
        recruiter = login(client, "hold-reader@deletion.test")
        created = request_deletion(client, recruiter, candidate_id)
        admin = login(client, "hold-admin@deletion.test")
        placed = client.post(
            f"/api/v1/candidates/{candidate_id}/legal-holds",
            json={"reason": "Privileged litigation detail"},
            headers={**admin, "Idempotency-Key": "hold-1"},
        )
        assert placed.status_code == 201
        placed_replay = client.post(
            f"/api/v1/candidates/{candidate_id}/legal-holds",
            json={"reason": "Privileged litigation detail"},
            headers={**admin, "Idempotency-Key": "hold-1"},
        )
        placed_conflict = client.post(
            f"/api/v1/candidates/{candidate_id}/legal-holds",
            json={"reason": "Different privileged detail"},
            headers={**admin, "Idempotency-Key": "hold-1"},
        )
        assert placed_replay.status_code == 201 and placed_replay.json() == placed.json()
        assert_problem(placed_conflict, 409, "idempotency_conflict")
        hold_id = placed.json()["data"]["id"]
        assert placed.headers["ETag"] == '"1"'
        admin_status = client.get(f"/api/v1/candidates/{candidate_id}/governance-status", headers=admin)
        recruiter = login(client, "hold-reader@deletion.test")
        recruiter_status = client.get(f"/api/v1/candidates/{candidate_id}/governance-status", headers=recruiter)
        manager = login(client, "hold-manager@deletion.test")
        manager_status = client.get(
            f"/api/v1/candidates/{candidate_id}/governance-status", headers=manager
        )
        system = login(client, "hold-system@deletion.test")
        system_status = client.get(
            f"/api/v1/candidates/{candidate_id}/governance-status", headers=system
        )
        with monkeypatch.context() as context:
            context.setattr(
                app.state.identity_service,
                "principal",
                lambda token: Principal(
                    user_id=system_id,
                    organization_id=organization_id,
                    roles=frozenset({"unknown_role"}),
                    active=True,
                ),
            )
            unknown_status = client.get(
                f"/api/v1/candidates/{candidate_id}/governance-status",
                headers=system,
            )
        assert admin_status.status_code == recruiter_status.status_code == 200
        assert admin_status.headers["Cache-Control"] == "no-store"
        assert recruiter_status.headers["Cache-Control"] == "no-store"
        assert admin_status.json()["data"] == {
            "deletion_status": "requested",
            "deletion_request_id": created.json()["data"]["id"],
            "legal_hold_active": True,
            "legal_hold_reason": "Privileged litigation detail",
            "legal_hold_id": hold_id,
            "legal_hold_version": 1,
        }
        assert "legal_hold_reason" not in recruiter_status.json()["data"]
        assert set(recruiter_status.json()["data"]) == {
            "deletion_status", "deletion_request_id", "legal_hold_active"
        }
        assert manager_status.status_code == 200
        assert manager_status.json()["data"] == {
            "deletion_status": "requested",
            "deletion_request_id": created.json()["data"]["id"],
            "legal_hold_active": True,
        }
        assert_problem(system_status, 404, "resource_not_found")
        assert_problem(unknown_status, 404, "resource_not_found")
        admin = login(client, "hold-admin@deletion.test")
        assert_problem(
            client.post(
                f"/api/v1/legal-holds/{hold_id}/releases",
                json={"reason": "Matter closed"},
                headers={**admin, "Idempotency-Key": "release-missing"},
            ),
            428,
            "precondition_required",
        )
        assert_problem(
            client.post(
                f"/api/v1/legal-holds/{hold_id}/releases",
                json={"reason": "Matter closed"},
                headers={**admin, "If-Match": "1", "Idempotency-Key": "release-malformed"},
            ),
            422,
            "validation_failed",
        )
        assert_problem(
            client.post(
                f"/api/v1/legal-holds/{hold_id}/releases",
                json={"reason": "Matter closed"},
                headers={**admin, "If-Match": '"2"', "Idempotency-Key": "release-stale"},
            ),
            409,
            "resource_version_conflict",
        )
        released = client.post(
            f"/api/v1/legal-holds/{hold_id}/releases",
            json={"reason": "Matter closed"},
            headers={**admin, "If-Match": '"1"', "Idempotency-Key": "release-1"},
        )
        replay = client.post(
            f"/api/v1/legal-holds/{hold_id}/releases",
            json={"reason": "Matter closed"},
            headers={**admin, "If-Match": '"1"', "Idempotency-Key": "release-1"},
        )
        conflict = client.post(
            f"/api/v1/legal-holds/{hold_id}/releases",
            json={"reason": "Different release reason"},
            headers={**admin, "If-Match": '"1"', "Idempotency-Key": "release-1"},
        )
        released_status = client.get(
            f"/api/v1/candidates/{candidate_id}/governance-status", headers=admin
        )
    assert released.status_code == replay.status_code == 200
    assert released.json() == replay.json()
    assert released.headers["ETag"] == '"2"'
    assert_problem(conflict, 409, "idempotency_conflict")
    assert released_status.status_code == 200
    assert released_status.headers["Cache-Control"] == "no-store"
    assert released_status.json()["data"] == {
        "deletion_status": "requested",
        "deletion_request_id": created.json()["data"]["id"],
        "legal_hold_active": False,
    }
    with app.state.identity_store.sync_session() as db:
        assert db.get(LegalHold, UUID(hold_id)).released_at is not None
        events = list(db.scalars(select(AuditLog).where(AuditLog.category == "governance")))
        status_events = [
            event
            for event in events
            if event.event_type == "governance.candidate_status_read"
        ]
        success_status_events = [
            event for event in status_events if event.outcome == "success"
        ]
        denied_status_events = [
            event for event in status_events if event.outcome == "denied"
        ]
        assert len(success_status_events) == 4
        assert {event.actor_user_id for event in success_status_events} == {
            admin_id,
            recruiter_id,
            manager_id,
        }
        assert all(event.metadata_json == {} for event in success_status_events)
        assert len(denied_status_events) == 2
        assert {event.actor_user_id for event in denied_status_events} == {system_id}
        assert all(
            event.metadata_json == {"safe_error_code": "resource_not_found"}
            for event in denied_status_events
        )
        assert any(e.event_type == "governance.legal_hold_placed" and e.outcome == "success" and e.metadata_json == {} for e in events)
        assert any(e.event_type == "governance.legal_hold_released" and e.outcome == "success" and e.metadata_json == {"hold_version": 2} for e in events)
        assert any(e.event_type == "governance.legal_hold_released" and e.outcome == "failure" and e.metadata_json == {"safe_error_code": "precondition_required"} for e in events)
        assert any(e.event_type == "governance.legal_hold_released" and e.outcome == "failure" and e.metadata_json == {"safe_error_code": "resource_version_conflict"} for e in events)


def test_audit_metadata_is_safe_and_expired_idempotency_can_be_reused(tmp_path) -> None:
    app = make_app(tmp_path)
    recruiter_id = seed_user(app, "recruiter", "audit-requester@deletion.test")
    candidate_id = candidate_for(app, recruiter_id)
    with TestClient(app) as client:
        headers = login(client, "audit-requester@deletion.test")
        created = request_deletion(client, headers, candidate_id, key="expiring")
        assert created.status_code == 201
        request_id = created.json()["data"]["id"]
        request_deletion(client, headers, candidate_id, key="conflict", reason="administrator_request")
        with app.state.identity_store.sync_session() as db:
            row = db.scalar(select(IdempotencyRecord).where(IdempotencyRecord.idempotency_key == "expiring"))
            row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            db.commit()
        reused = request_deletion(client, headers, candidate_id, key="expiring")
        assert_problem(reused, 409, "deletion_request_open")
    with app.state.identity_store.sync_session() as db:
        events = list(db.scalars(select(AuditLog).where(AuditLog.category == "governance")))
        assert events
        assert {event.outcome for event in events} >= {"success", "failure"}
        for event in events:
            assert not SENSITIVE.search(str(event.metadata_json))
        assert db.get(DeletionRequest, UUID(request_id)) is not None


def test_requester_cannot_self_approve_after_gaining_system_role(tmp_path) -> None:
    app = make_app(tmp_path)
    requester_id = seed_user(app, "recruiter", "self-approver@deletion.test")
    candidate_id = candidate_for(app, requester_id)
    with TestClient(app) as client:
        requester = login(client, "self-approver@deletion.test")
        created = request_deletion(client, requester, candidate_id)
        request_id = created.json()["data"]["id"]
        with app.state.identity_store.sync_session() as db:
            db.add(UserRole(user_id=requester_id, role="system_admin"))
            db.commit()
        elevated = login(client, "self-approver@deletion.test")
        response = approve(client, elevated, request_id, 1, "self-elevated")
    assert_problem(response, 409, "self_approval_forbidden")
    with app.state.identity_store.sync_session() as db:
        row = db.get(DeletionRequest, UUID(request_id))
        assert row.status == "requested" and row.version == 1
        assert db.scalar(select(func.count()).select_from(BackgroundJob)) == 0


def test_hold_fails_approved_request_cancels_job_and_rejects_executing(tmp_path) -> None:
    app = make_app(tmp_path)
    requester_id = seed_user(app, "recruiter", "hold-race-requester@deletion.test")
    seed_user(app, "system_admin", "hold-race-system@deletion.test")
    seed_user(app, "recruiting_admin", "hold-race-admin@deletion.test")
    candidate_id = candidate_for(app, requester_id)
    with TestClient(app) as client:
        requester = login(client, "hold-race-requester@deletion.test")
        created = request_deletion(client, requester, candidate_id)
        request_id = created.json()["data"]["id"]
        system = login(client, "hold-race-system@deletion.test")
        assert approve(client, system, request_id, 1).status_code == 200
        admin = login(client, "hold-race-admin@deletion.test")
        held = client.post(
            f"/api/v1/candidates/{candidate_id}/legal-holds",
            json={"reason": "Preserve evidence"},
            headers={**admin, "Idempotency-Key": "approved-hold"},
        )
        assert held.status_code == 201
    with app.state.identity_store.sync_session() as db:
        row = db.get(DeletionRequest, UUID(request_id))
        job = db.scalar(select(BackgroundJob).where(BackgroundJob.type == "governance.delete_candidate"))
        assert row.status == "failed" and row.safe_error_code == "legal_hold_active"
        assert row.version == 3 and job.status == "cancelled"

    second_path = tmp_path / "second"
    second_path.mkdir()
    second_app = make_app(second_path)
    requester_id = seed_user(second_app, "recruiter", "executing-requester@deletion.test")
    seed_user(second_app, "recruiting_admin", "executing-admin@deletion.test")
    candidate_id = candidate_for(second_app, requester_id)
    with TestClient(second_app) as client:
        requester = login(client, "executing-requester@deletion.test")
        created = request_deletion(client, requester, candidate_id)
        request_id = created.json()["data"]["id"]
        with second_app.state.identity_store.sync_session() as db:
            row = db.get(DeletionRequest, UUID(request_id))
            row.status = "executing"
            db.commit()
        admin = login(client, "executing-admin@deletion.test")
        rejected = client.post(
            f"/api/v1/candidates/{candidate_id}/legal-holds",
            json={"reason": "Too late"},
            headers={**admin, "Idempotency-Key": "executing-hold"},
        )
    assert_problem(rejected, 409, "deletion_already_executing")
    with second_app.state.identity_store.sync_session() as db:
        assert db.scalar(select(func.count()).select_from(LegalHold)) == 0


def test_audit_and_queue_failures_roll_back_request_job_and_idempotency(
    tmp_path, monkeypatch
) -> None:
    app = make_app(tmp_path)
    requester_id = seed_user(app, "recruiter", "rollback-requester@deletion.test")
    candidate_id = candidate_for(app, requester_id)

    def fail_audit(*args, **kwargs):
        raise RuntimeError("audit unavailable")

    monkeypatch.setattr(deletion_service, "append_audit", fail_audit)
    with TestClient(app) as client:
        requester = login(client, "rollback-requester@deletion.test")
        response = request_deletion(client, requester, candidate_id, "rollback-request")
    assert_problem(response, 500, "internal_error")
    with app.state.identity_store.sync_session() as db:
        assert db.scalar(select(func.count()).select_from(DeletionRequest)) == 0
        assert db.scalar(
            select(func.count()).select_from(IdempotencyRecord).where(
                IdempotencyRecord.operation == "governance.deletion_request.create"
            )
        ) == 0

    monkeypatch.undo()
    seed_user(app, "system_admin", "rollback-system@deletion.test")
    with TestClient(app) as client:
        requester = login(client, "rollback-requester@deletion.test")
        created = request_deletion(client, requester, candidate_id, "rollback-created")
        request_id = created.json()["data"]["id"]

        def fail_enqueue(*args, **kwargs):
            raise RuntimeError("queue unavailable")

        monkeypatch.setattr(deletion_service.QueueRepository, "enqueue", fail_enqueue)
        system = login(client, "rollback-system@deletion.test")
        failed = approve(client, system, request_id, 1, "rollback-approval")
    assert_problem(failed, 500, "internal_error")
    with app.state.identity_store.sync_session() as db:
        row = db.get(DeletionRequest, UUID(request_id))
        assert row.status == "requested" and row.version == 1
        assert db.scalar(select(func.count()).select_from(BackgroundJob)) == 0
        assert db.scalar(
            select(func.count()).select_from(IdempotencyRecord).where(
                IdempotencyRecord.operation == "governance.deletion_request.approve"
            )
        ) == 0


def test_authenticated_route_rejections_are_audited_and_audit_failure_is_503(
    tmp_path, monkeypatch
) -> None:
    app = make_app(tmp_path)
    requester_id = seed_user(app, "recruiter", "route-audit@deletion.test")
    candidate_id = candidate_for(app, requester_id)
    with TestClient(app) as client:
        headers = login(client, "route-audit@deletion.test")
        invalid = client.post(
            f"/api/v1/candidates/{candidate_id}/deletion-requests",
            json={"reason_code": "retention_expired"},
            headers=headers,
        )
        assert_problem(invalid, 422, "validation_failed")
    with app.state.identity_store.sync_session() as db:
        event = db.scalar(
            select(AuditLog).where(AuditLog.event_type == "governance.request_rejected")
        )
        assert event.outcome == "failure"
        assert event.metadata_json == {"safe_error_code": "validation_failed"}

    monkeypatch.setattr(deletion_service, "append_audit", lambda *a, **k: (_ for _ in ()).throw(RuntimeError()))
    with TestClient(app) as client:
        headers = login(client, "route-audit@deletion.test")
        unavailable = client.post(
            f"/api/v1/candidates/{candidate_id}/deletion-requests",
            json={"reason_code": "candidate_request"},
            headers=headers,
        )
    assert_problem(unavailable, 503, "audit_unavailable")


def test_authenticated_list_denial_is_audited(tmp_path, monkeypatch) -> None:
    app = make_app(tmp_path)
    requester_id = seed_user(app, "recruiter", "inactive-list@deletion.test")
    with app.state.identity_store.sync_session() as db:
        user = db.get(User, requester_id)
        organization_id = user.organization_id
    with TestClient(app) as client:
        headers = login(client, "inactive-list@deletion.test")
        monkeypatch.setattr(
            app.state.identity_service,
            "principal",
            lambda token: Principal(
                user_id=requester_id,
                organization_id=organization_id,
                roles=frozenset({"recruiter"}),
                active=False,
            ),
        )
        denied = client.get("/api/v1/deletion-requests", headers=headers)
    assert_problem(denied, 404, "resource_not_found")
    with app.state.identity_store.sync_session() as db:
        events = list(
            db.scalars(
                select(AuditLog).where(
                    AuditLog.event_type == "governance.deletion_requests_listed"
                )
            )
        )
        assert [(event.outcome, event.metadata_json) for event in events] == [
            ("denied", {"safe_error_code": "resource_not_found"})
        ]


def test_all_b2a_endpoints_are_non_enumerating_for_known_cross_tenant_ids(
    tmp_path, monkeypatch
) -> None:
    app = make_app(tmp_path)
    local_users = {}
    for role in (
        "recruiter",
        "recruiting_admin",
        "system_admin",
        "hiring_manager",
        "interviewer",
    ):
        local_users[role] = seed_user(app, role, f"matrix-{role}@deletion.test")
    with app.state.identity_store.sync_session() as db:
        local_organization_id = db.get(User, local_users["system_admin"]).organization_id
        other_org = Organization(slug="other-matrix", name="Other", status="active")
        other_user = User(organization=other_org, email="other@matrix.test", normalized_email="other@matrix.test", display_name="Other private", password_hash=PasswordService().hash("correct horse"))
        other_user.roles.append(UserRole(role="recruiting_admin")); db.add(other_user); db.flush()
        candidate = Candidate(organization_id=other_org.id, display_name="Cross tenant private", owner_id=other_user.id)
        db.add(candidate); db.flush()
        manifest, policy = build_private_manifest(db, candidate, now=FixedClock().current_time())
        deletion = DeletionRequest(organization_id=other_org.id, candidate_id=candidate.id, reason_code="administrator_request", requested_by=other_user.id, impact_manifest=manifest, manifest_hash=canonical_manifest_hash(manifest), policy_version=policy.version, candidate_version=candidate.version)
        hold = LegalHold(organization_id=other_org.id, candidate_id=candidate.id, reason="Cross tenant hold secret", placed_by=other_user.id)
        db.add_all([deletion, hold]); db.commit()
        candidate_id, request_id, hold_id = candidate.id, deletion.id, hold.id
    with app.state.identity_store.sync_session() as db:
        before = (db.scalar(select(func.count()).select_from(DeletionRequest)), db.scalar(select(func.count()).select_from(LegalHold)), db.scalar(select(func.count()).select_from(BackgroundJob)))
    with TestClient(app) as client:
        for role in local_users:
            headers = login(client, f"matrix-{role}@deletion.test")
            responses = [
                request_deletion(client, headers, candidate_id, f"matrix-create-{role}"),
                client.get("/api/v1/deletion-requests", headers=headers),
                client.get(f"/api/v1/deletion-requests/{request_id}", headers=headers),
                client.post(f"/api/v1/deletion-requests/{request_id}/transitions", json={"target_status":"approved"}, headers={**headers,"If-Match":'"1"',"Idempotency-Key":f"matrix-approve-{role}"}),
                client.post(f"/api/v1/candidates/{candidate_id}/legal-holds", json={"reason":"Local attempt"}, headers={**headers,"Idempotency-Key":f"matrix-hold-{role}"}),
                client.post(f"/api/v1/legal-holds/{hold_id}/releases", json={"reason":"Local attempt"}, headers={**headers,"If-Match":'"1"',"Idempotency-Key":f"matrix-release-{role}"}),
                client.get(f"/api/v1/candidates/{candidate_id}/governance-status", headers=headers),
            ]
            assert responses[0].status_code == 404
            assert str(request_id) not in responses[1].text
            assert all(response.status_code == 404 for response in responses[2:])
            governance_status = responses[-1]
            assert str(hold_id) not in governance_status.text
            assert "Cross tenant hold secret" not in governance_status.text
            assert "legal_hold_active" not in governance_status.text
        monkeypatch.setattr(
            app.state.identity_service,
            "principal",
            lambda token: Principal(
                user_id=local_users["system_admin"],
                organization_id=local_organization_id,
                roles=frozenset({"unknown_role"}),
                active=True,
            ),
        )
        unknown_status = client.get(
            f"/api/v1/candidates/{candidate_id}/governance-status", headers=headers
        )
        assert_problem(unknown_status, 404, "resource_not_found")
        assert str(hold_id) not in unknown_status.text
        assert "Cross tenant hold secret" not in unknown_status.text
        assert "legal_hold_active" not in unknown_status.text
    with app.state.identity_store.sync_session() as db:
        after = (db.scalar(select(func.count()).select_from(DeletionRequest)), db.scalar(select(func.count()).select_from(LegalHold)), db.scalar(select(func.count()).select_from(BackgroundJob)))
        assert after == before
        status_events = list(
            db.scalars(
                select(AuditLog).where(
                    AuditLog.event_type == "governance.candidate_status_read"
                )
            )
        )
        assert len(status_events) == len(local_users) + 1
        assert all(event.outcome == "denied" for event in status_events)
        assert all(
            event.metadata_json == {"safe_error_code": "resource_not_found"}
            for event in status_events
        )
