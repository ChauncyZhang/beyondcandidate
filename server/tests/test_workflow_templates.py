from datetime import datetime, timedelta, timezone
from uuid import UUID

from fastapi.testclient import TestClient
from sqlalchemy import select

from server.app.identity.models import AuditLog, Job, Organization, WorkflowTemplate
from server.app.interviews.models import Interview, InterviewEvent
from server.app.recruiting.models import Application, JobJdVersion
from server.tests.test_interview_api import (
    create_interview,
    feedback_payload,
    interview_payload,
    seed_application,
)
from server.tests.test_recruiting_api import (
    job_definition_payload,
    login,
    make_app,
    seed_user,
)


def test_workflow_template_defaults_crud_permissions_and_versions(tmp_path) -> None:
    app = make_app(tmp_path)
    seed_user(app, "recruiting_admin", "template-admin@example.test")
    seed_user(app, "system_admin", "template-system@example.test")
    seed_user(app, "recruiter", "template-reader@example.test")
    seed_user(app, "hiring_manager", "template-manager@example.test")

    with TestClient(app) as client:
        reader_headers = login(client, "template-reader@example.test")
        first = client.get("/api/v1/settings/workflow-templates", headers=reader_headers)
        second = client.get("/api/v1/settings/workflow-templates", headers=reader_headers)
        assert first.status_code == second.status_code == 200
        defaults = {item["name"]: item["rounds"] for item in first.json()["data"]}
        assert defaults == {
            "标准社招流程": ["一面"],
            "技术岗位流程": ["一面", "二面"],
        }
        assert len(second.json()["data"]) == 2

        denied_create = client.post(
            "/api/v1/settings/workflow-templates",
            json={"name": "Recruiter cannot write", "rounds": ["一面"]},
            headers=reader_headers,
        )
        assert denied_create.status_code == 403
        manager_headers = login(client, "template-manager@example.test")
        assert client.get(
            "/api/v1/settings/workflow-templates", headers=manager_headers
        ).status_code == 403

        admin_headers = login(client, "template-admin@example.test")
        invalid = client.post(
            "/api/v1/settings/workflow-templates",
            json={"name": "Invalid", "rounds": ["   "]},
            headers=admin_headers,
        )
        assert invalid.status_code == 422
        created = client.post(
            "/api/v1/settings/workflow-templates",
            json={"name": "管理岗位流程", "rounds": ["业务面", "终面"]},
            headers=admin_headers,
        )
        assert created.status_code == 201
        template = created.json()["data"]
        assert set(template) == {
            "id",
            "organization_id",
            "name",
            "rounds",
            "status",
            "version",
            "created_at",
            "updated_at",
        }
        assert template["version"] == 1
        assert created.headers["ETag"] == '"1"'

        missing_match = client.patch(
            f"/api/v1/settings/workflow-templates/{template['id']}",
            json={"status": "inactive"},
            headers=admin_headers,
        )
        assert missing_match.status_code == 428
        updated = client.patch(
            f"/api/v1/settings/workflow-templates/{template['id']}",
            json={"rounds": ["业务初面", "业务复面"], "status": "inactive"},
            headers={**admin_headers, "If-Match": '"1"'},
        )
        assert updated.status_code == 200
        assert updated.json()["data"]["version"] == 2
        assert updated.json()["data"]["status"] == "inactive"
        assert updated.headers["ETag"] == '"2"'
        stale = client.patch(
            f"/api/v1/settings/workflow-templates/{template['id']}",
            json={"status": "active"},
            headers={**admin_headers, "If-Match": '"1"'},
        )
        assert stale.status_code == 409

        system_headers = login(client, "template-system@example.test")
        assert client.post(
            "/api/v1/settings/workflow-templates",
            json={"name": "系统管理员流程", "rounds": ["终面"]},
            headers=system_headers,
        ).status_code == 201

    with app.state.identity_store.sync_session() as db:
        events = db.scalars(select(AuditLog.event_type).where(
            AuditLog.resource_type == "workflow_template"
        )).all()
        assert events.count("organization.workflow_template_default_created") == 2
        assert "organization.workflow_template_created" in events
        assert "organization.workflow_template_updated" in events


def test_job_definition_validates_and_projects_workflow_template_binding(tmp_path) -> None:
    app = make_app(tmp_path)
    seed_user(app, "recruiting_admin", "job-template-admin@example.test")
    with app.state.identity_store.sync_session() as db:
        organization_id = db.scalar(select(Organization.id).where(Organization.slug == "acme"))
        active = WorkflowTemplate(
            organization_id=organization_id,
            name="绑定流程",
            rounds=["一面", "二面"],
        )
        inactive = WorkflowTemplate(
            organization_id=organization_id,
            name="停用流程",
            rounds=["一面"],
            status="inactive",
        )
        other_organization = Organization(slug="template-other", name="Template Other")
        db.add_all([active, inactive, other_organization])
        db.flush()
        cross_tenant = WorkflowTemplate(
            organization_id=other_organization.id,
            name="Other tenant",
            rounds=["一面"],
        )
        db.add(cross_tenant)
        db.commit()

    with TestClient(app) as client:
        headers = login(client, "job-template-admin@example.test")
        for template_id, key in ((inactive.id, "inactive"), (cross_tenant.id, "cross-tenant")):
            rejected = client.post(
                "/api/v1/job-definitions",
                json=job_definition_payload(workflow_template_id=str(template_id)),
                headers={**headers, "Idempotency-Key": key},
            )
            assert rejected.status_code == 422
            assert rejected.json()["code"] == "workflow_template_invalid"

        created = client.post(
            "/api/v1/job-definitions",
            json=job_definition_payload(workflow_template_id=str(active.id)),
            headers={**headers, "Idempotency-Key": "active-template"},
        )
        assert created.status_code == 201
        data = created.json()["data"]
        assert data["job"]["workflow_template_id"] == str(active.id)
        assert data["jd"]["workflow_template_id"] == str(active.id)
        job_id = UUID(data["job"]["id"])

        with app.state.identity_store.sync_session() as db:
            db.get(WorkflowTemplate, active.id).status = "inactive"
            db.commit()
        rejected_update = client.put(
            f"/api/v1/job-definitions/{job_id}",
            json=job_definition_payload(workflow_template_id=str(active.id)),
            headers={
                **headers,
                "Idempotency-Key": "inactive-template-update",
                "If-Match": '"1"',
            },
        )
        assert rejected_update.status_code == 422

    with app.state.identity_store.sync_session() as db:
        job = db.get(Job, job_id)
        jd = db.scalar(select(JobJdVersion).where(JobJdVersion.job_id == job_id))
        assert job.workflow_template_id == active.id
        assert jd.content["workflow_template_id"] == str(active.id)


def test_multi_round_feedback_advances_to_next_round_then_decision(tmp_path) -> None:
    app = make_app(tmp_path)
    seed = seed_application(app)
    with app.state.identity_store.sync_session() as db:
        job = db.get(Job, seed["job_id"])
        template = WorkflowTemplate(
            organization_id=job.organization_id,
            name="技术岗位流程",
            rounds=["技术一面", "技术二面"],
        )
        db.add(template)
        db.flush()
        job.workflow_template_id = template.id
        db.commit()

    def finish_round(client: TestClient, round_name: str, start: datetime, key: str) -> UUID:
        payload = interview_payload(seed, starts_at=start)
        payload["round_name"] = round_name
        created, admin_headers = create_interview(client, seed, key=f"{key}-create", payload=payload)
        interview_id = UUID(created.json()["data"]["id"])
        assert client.post(
            f"/api/v1/interviews/{interview_id}/transitions",
            json={"target": "confirmed"},
            headers={**admin_headers, "If-Match": '"1"', "Idempotency-Key": f"{key}-confirm"},
        ).status_code == 200
        assert client.post(
            f"/api/v1/interviews/{interview_id}/transitions",
            json={"target": "completed"},
            headers={**admin_headers, "If-Match": '"2"', "Idempotency-Key": f"{key}-complete"},
        ).status_code == 200
        interviewer_headers = login(client, "assigned@example.test")
        assert client.put(
            f"/api/v1/interviews/{interview_id}/my-feedback",
            json=feedback_payload(),
            headers={**interviewer_headers, "If-Match": '"0"'},
        ).status_code == 200
        assert client.post(
            f"/api/v1/interviews/{interview_id}/my-feedback/submit",
            headers={**interviewer_headers, "Idempotency-Key": f"{key}-submit"},
        ).status_code == 200
        return interview_id

    start = datetime.now(timezone.utc) + timedelta(hours=2)
    with TestClient(app) as client:
        first_id = finish_round(client, "技术一面", start, "technical-first")
        with app.state.identity_store.sync_session() as db:
            assert db.get(Application, seed["application_id"]).stage == "interview_pending"

        admin_headers = login(client, "interview-admin@example.test")
        candidates = client.get("/api/v1/candidates", headers=admin_headers)
        assert candidates.status_code == 200
        candidate = next(item for item in candidates.json()["data"] if item["id"] == str(seed["candidate_id"]))
        assert candidate["application"]["next_interview_round"] == "技术二面"
        applications = client.get(
            f"/api/v1/candidates/{seed['candidate_id']}/applications",
            headers=admin_headers,
        )
        assert applications.status_code == 200
        application = next(
            item for item in applications.json()["data"]
            if item["id"] == str(seed["application_id"])
        )
        assert application["next_interview_round"] == "技术二面"
        workbench = client.get("/api/v1/workbench", headers=admin_headers)
        pending_items = workbench.json()["data"]["tasks"]["interview_pending"]["items"]
        assert next(item for item in pending_items if item["application_id"] == str(seed["application_id"]))["next_interview_round"] == "技术二面"

        second_id = finish_round(
            client, "技术二面", start + timedelta(hours=2), "technical-second"
        )

    with app.state.identity_store.sync_session() as db:
        assert db.get(Application, seed["application_id"]).stage == "decision"
        first_event = db.scalar(select(InterviewEvent).where(
            InterviewEvent.interview_id == first_id,
            InterviewEvent.event_type == "interview.feedback_completed",
        ))
        second_event = db.scalar(select(InterviewEvent).where(
            InterviewEvent.interview_id == second_id,
            InterviewEvent.event_type == "interview.feedback_completed",
        ))
        assert first_event.payload["application_advanced"] is True
        assert first_event.payload["next_round_name"] == "技术二面"
        assert second_event.payload["application_advanced"] is True
        assert second_event.payload["next_round_name"] is None


def test_create_interview_enforces_expected_template_round_and_legacy_compatibility(tmp_path) -> None:
    app = make_app(tmp_path)
    seed = seed_application(app)
    with app.state.identity_store.sync_session() as db:
        job = db.get(Job, seed["job_id"])
        template = WorkflowTemplate(
            organization_id=job.organization_id,
            name="Strict rounds",
            rounds=["技术一面", "技术二面"],
        )
        db.add(template)
        db.flush()
        job.workflow_template_id = template.id
        db.commit()

    start = datetime.now(timezone.utc) + timedelta(hours=3)
    wrong_round = interview_payload(seed, starts_at=start)
    wrong_round["round_name"] = "技术二面"
    expected_round = interview_payload(seed, starts_at=start)
    expected_round["round_name"] = "技术一面"
    legacy_round = interview_payload(seed, starts_at=start)
    legacy_round["round_name"] = "Legacy custom round"

    with TestClient(app) as client:
        headers = login(client, "interview-admin@example.test")
        invalid_round = client.post(
            "/api/v1/interviews",
            json=wrong_round,
            headers={**headers, "Idempotency-Key": "wrong-template-round"},
        )
        assert invalid_round.status_code == 422
        assert invalid_round.json()["code"] == "interview_round_invalid"

        with app.state.identity_store.sync_session() as db:
            db.get(Application, seed["application_id"]).stage = "decision"
            db.commit()
        additional_round = dict(expected_round)
        additional_round["round_name"] = "三面"
        additional_round["starts_at"] = (start + timedelta(hours=1)).isoformat()
        additional_round["ends_at"] = (start + timedelta(hours=2)).isoformat()
        appended = client.post(
            "/api/v1/interviews",
            json=additional_round,
            headers={**headers, "Idempotency-Key": "additional-round"},
        )
        assert appended.status_code == 201
        assert appended.json()["data"]["round_name"] == "三面"
        with app.state.identity_store.sync_session() as db:
            assert db.get(Application, seed["application_id"]).stage == "interviewing"

        with app.state.identity_store.sync_session() as db:
            db.get(Application, seed["application_id"]).stage = "interview_pending"
            db.get(Job, seed["job_id"]).workflow_template_id = None
            db.commit()
        legacy_round["starts_at"] = (start + timedelta(hours=3)).isoformat()
        legacy_round["ends_at"] = (start + timedelta(hours=4)).isoformat()
        legacy = client.post(
            "/api/v1/interviews",
            json=legacy_round,
            headers={**headers, "Idempotency-Key": "legacy-custom-round"},
        )
        assert legacy.status_code == 201
        assert legacy.json()["data"]["round_name"] == "Legacy custom round"

    with app.state.identity_store.sync_session() as db:
        interviews = db.scalars(select(Interview).where(
            Interview.application_id == seed["application_id"]
        )).all()
        assert [interview.round_name for interview in interviews] == ["三面", "Legacy custom round"]
