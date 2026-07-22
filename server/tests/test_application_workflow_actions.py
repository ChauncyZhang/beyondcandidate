from fastapi.testclient import TestClient
from sqlalchemy import select

from server.app.identity.models import Job, JobCollaborator, User
from server.app.recruiting.models import Application, ApplicationStageEvent, Candidate, FileObject, Resume
from server.tests.test_recruiting_api import login, make_app, seed_user


def seed_workflow_application(app, admin_id, manager_id, stage):
    with app.state.identity_store.sync_session() as db:
        admin = db.get(User, admin_id)
        job = Job(
            organization_id=admin.organization_id,
            title=f"Workflow {stage}",
            owner_id=admin.id,
            hiring_owner_id=manager_id,
            status="open",
        )
        candidate = Candidate(
            organization_id=admin.organization_id,
            display_name=f"Candidate {stage}",
            owner_id=admin.id,
        )
        file = FileObject(
            organization_id=admin.organization_id,
            storage_key=f"private/workflow/{stage}",
            original_filename=f"{stage}.pdf",
            mime_type="application/pdf",
            size_bytes=1,
            sha256=(stage[0] * 64),
            uploaded_by=admin.id,
        )
        db.add_all([job, candidate, file])
        db.flush()
        db.add(JobCollaborator(
            organization_id=admin.organization_id,
            job_id=job.id,
            user_id=manager_id,
            access_role="job_manager",
        ))
        resume = Resume(
            organization_id=admin.organization_id,
            candidate_id=candidate.id,
            file_object_id=file.id,
            version_number=1,
        )
        db.add(resume)
        db.flush()
        application = Application(
            organization_id=admin.organization_id,
            candidate_id=candidate.id,
            job_id=job.id,
            resume_id=resume.id,
            owner_id=admin.id,
            stage=stage,
            source="screening",
        )
        db.add(application)
        db.commit()
        return str(application.id)


def test_hiring_manager_review_action_advances_directly_to_interview_queue(tmp_path):
    app = make_app(tmp_path)
    admin_id = seed_user(app, "recruiting_admin", "admin@example.test")
    manager_id = seed_user(app, "hiring_manager", "manager@example.test")
    application_id = seed_workflow_application(app, admin_id, manager_id, "review")

    with TestClient(app) as client:
        headers = login(client, "manager@example.test")
        response = client.post(
            f"/api/v1/applications/{application_id}/workflow-actions",
            json={"action": "review_approved"},
            headers={**headers, "If-Match": '"1"', "Idempotency-Key": "approve-review"},
        )

    assert response.status_code == 200
    assert response.json()["data"]["stage"] == "interview_pending"
    assert response.json()["data"]["version"] == 3
    with app.state.identity_store.sync_session() as db:
        events = list(db.scalars(select(ApplicationStageEvent).order_by(ApplicationStageEvent.created_at, ApplicationStageEvent.id)))
        assert [(event.payload["from_stage"], event.payload["to_stage"]) for event in events] == [
            ("review", "contact"),
            ("contact", "interview_pending"),
        ]


def test_workflow_actions_require_the_expected_business_stage_and_rejection_reason(tmp_path):
    app = make_app(tmp_path)
    admin_id = seed_user(app, "recruiting_admin", "admin@example.test")
    manager_id = seed_user(app, "hiring_manager", "manager@example.test")
    review_id = seed_workflow_application(app, admin_id, manager_id, "review")
    decision_id = seed_workflow_application(app, admin_id, manager_id, "decision")
    passed_id = seed_workflow_application(app, admin_id, manager_id, "passed")

    with TestClient(app) as client:
        manager_headers = login(client, "manager@example.test")
        missing_reason = client.post(
            f"/api/v1/applications/{review_id}/workflow-actions",
            json={"action": "review_rejected"},
            headers={**manager_headers, "If-Match": '"1"', "Idempotency-Key": "reject-without-reason"},
        )
        approved = client.post(
            f"/api/v1/applications/{decision_id}/workflow-actions",
            json={"action": "hiring_approved"},
            headers={**manager_headers, "If-Match": '"1"', "Idempotency-Key": "approve-hiring"},
        )
        wrong_stage = client.post(
            f"/api/v1/applications/{review_id}/workflow-actions",
            json={"action": "hiring_approved"},
            headers={**manager_headers, "If-Match": '"1"', "Idempotency-Key": "wrong-stage"},
        )
        admin_headers = login(client, "admin@example.test")
        hired = client.post(
            f"/api/v1/applications/{passed_id}/workflow-actions",
            json={"action": "offer_accepted"},
            headers={**admin_headers, "If-Match": '"1"', "Idempotency-Key": "offer-accepted"},
        )

    assert missing_reason.status_code == 409
    assert missing_reason.json()["code"] == "invalid_state_transition"
    assert approved.status_code == 200 and approved.json()["data"]["stage"] == "passed"
    assert wrong_stage.status_code == 409 and wrong_stage.json()["code"] == "invalid_state_transition"
    assert hired.status_code == 200 and hired.json()["data"]["stage"] == "hired"
