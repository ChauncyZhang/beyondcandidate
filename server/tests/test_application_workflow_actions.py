from uuid import UUID

from fastapi.testclient import TestClient
from sqlalchemy import select

from server.app.identity.models import AuditLog, Job, JobCollaborator, User
from server.app.recruiting.models import Application, ApplicationReviewTask, ApplicationStageEvent, Candidate, FileObject, Resume
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


def test_hr_reassigns_open_review_task_with_assignee_etag_and_only_new_assignee_can_complete(tmp_path):
    app = make_app(tmp_path)
    admin_id = seed_user(app, "recruiting_admin", "admin-reassign@example.test")
    manager_id = seed_user(app, "hiring_manager", "manager-reassign@example.test")
    next_manager_id = seed_user(app, "hiring_manager", "next-manager@example.test")
    application_id = seed_workflow_application(app, admin_id, manager_id, "review")
    with app.state.identity_store.sync_session() as db:
        application = db.get(Application, UUID(application_id))
        db.add(JobCollaborator(
            organization_id=application.organization_id,
            job_id=application.job_id,
            user_id=next_manager_id,
            access_role="job_manager",
        ))
        task = ApplicationReviewTask(
            organization_id=application.organization_id,
            application_id=application.id,
            assignee_id=manager_id,
            status="open",
            ai_status="succeeded",
        )
        db.add(task)
        db.commit()
        task_id = task.id

    with TestClient(app) as client:
        admin_headers = login(client, "admin-reassign@example.test")
        reassigned = client.put(
            f"/api/v1/review-tasks/{task_id}/assignee",
            json={"assignee_id": str(next_manager_id)},
            headers={**admin_headers, "If-Match": f'"{manager_id}"'},
        )
        stale = client.put(
            f"/api/v1/review-tasks/{task_id}/assignee",
            json={"assignee_id": str(manager_id)},
            headers={**admin_headers, "If-Match": f'"{manager_id}"'},
        )
        old_headers = login(client, "manager-reassign@example.test")
        old_assignee = client.post(
            f"/api/v1/applications/{application_id}/workflow-actions",
            json={"action": "review_approved"},
            headers={**old_headers, "If-Match": '"1"', "Idempotency-Key": "old-assignee"},
        )
        new_headers = login(client, "next-manager@example.test")
        new_assignee = client.post(
            f"/api/v1/applications/{application_id}/workflow-actions",
            json={"action": "review_approved"},
            headers={**new_headers, "If-Match": '"1"', "Idempotency-Key": "new-assignee"},
        )

    assert reassigned.status_code == 200
    assert reassigned.headers["etag"] == f'"{next_manager_id}"'
    assert stale.status_code == 409 and stale.json()["code"] == "review_task_assignee_conflict"
    assert old_assignee.status_code == 404
    assert new_assignee.status_code == 200
    with app.state.identity_store.sync_session() as db:
        audit = db.query(AuditLog).filter_by(event_type="review_task.reassigned").one()
        assert audit.metadata_json["from_assignee_id"] == str(manager_id)
        assert audit.metadata_json["to_assignee_id"] == str(next_manager_id)


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
