import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from server.app.recruiting.models import Application, ApplicationReviewTask
from server.tests.test_application_workflow_actions import seed_workflow_application
from server.tests.test_recruiting_api import login, make_app, seed_user


def setup_application(tmp_path, stage="review"):
    app = make_app(tmp_path)
    admin = seed_user(app, "recruiting_admin", "terminate@example.test")
    application_id = seed_workflow_application(app, admin, admin, stage)
    return app, admin, application_id


@pytest.mark.parametrize("stage", ["new", "deferred", "review", "contact", "interview_pending", "interviewing", "decision", "passed", "hired"])
def test_terminate_stages_and_idempotency(tmp_path, stage):
    app, admin, application_id = setup_application(tmp_path, stage)
    url = f"/api/v1/applications/{application_id}/termination"
    with TestClient(app) as client:
        headers = login(client, "terminate@example.test")
        preview = client.get(url, headers=headers)
        assert preview.status_code == 200
        assert preview.json()["data"]["termination"] is None
        body = {"reason_text": "  Candidate withdrew  ", "preview_version": preview.json()["data"]["preview_version"], "confirm_not_onboarded": stage == "hired"}
        command_headers = {**headers, "If-Match": '"1"', "Idempotency-Key": "terminate-once"}
        result = client.post(url, headers=command_headers, json=body)
        assert result.status_code == 200, result.text
        data = result.json()["data"]
        assert data["stage"] == "withdrawn"
        assert data["version"] == 2
        assert data["termination"]["source_stage"] == stage
        assert data["termination"]["reason_text"] == "Candidate withdrew"
        assert client.post(url, headers=command_headers, json=body).json() == result.json()
        assert client.post(url, headers=command_headers, json={**body, "reason_text": "changed"}).status_code == 409


def test_hired_confirmation_and_preview_dependency_conflicts(tmp_path):
    app, admin, application_id = setup_application(tmp_path, "hired")
    url = f"/api/v1/applications/{application_id}/termination"
    with TestClient(app) as client:
        headers = {**login(client, "terminate@example.test"), "If-Match": '"1"', "Idempotency-Key": "hired"}
        preview = client.get(url, headers=headers).json()["data"]
        body = {"reason_text": "withdraw", "preview_version": preview["preview_version"]}
        response = client.post(url, headers=headers, json=body)
        assert response.status_code == 422
        assert response.json()["code"] == "not_onboarded_confirmation_required"
        with app.state.identity_store.sync_session() as db:
            application = db.get(Application, uuid.UUID(application_id))
            db.add(ApplicationReviewTask(organization_id=application.organization_id, application_id=application.id, assignee_id=admin, ai_status="succeeded"))
            db.commit()
        response = client.post(url, headers=headers, json={**body, "confirm_not_onboarded": True})
        assert response.status_code == 409
        assert response.json()["code"] == "termination_preview_changed"
        body["preview_version"] = client.get(url, headers=headers).json()["data"]["preview_version"]
        assert client.post(url, headers=headers, json={**body, "confirm_not_onboarded": True}).status_code == 200
        with app.state.identity_store.sync_session() as db:
            assert db.scalar(select(ApplicationReviewTask)).status == "cancelled"


def test_external_unknown_blocks_ack_and_novel_fact_reopens(tmp_path):
    from server.app.recruiting.termination import record_external_fact
    app, admin, application_id = setup_application(tmp_path)
    url = f"/api/v1/applications/{application_id}/termination"
    with TestClient(app) as client:
        headers = login(client, "terminate@example.test")
        preview = client.get(url, headers=headers).json()["data"]
        result = client.post(url, headers={**headers, "If-Match": '"1"', "Idempotency-Key": "stop"}, json={"reason_text": "withdraw", "preview_version": preview["preview_version"]})
        assert result.status_code == 200
        def fact(state, reference=None, generation="g1"):
            with app.state.identity_store.sync_session() as db:
                application = db.get(Application, uuid.UUID(application_id))
                record_external_fact(db, application.organization_id, application.id, kind="onboarding", resource_id="r1", state=state, reference=reference, generation=generation)
                db.commit()
        fact("unknown")
        current = client.get(url, headers=headers).json()["data"]["termination"]
        ack = lambda version, key: client.post(url + "/external-resolution", headers={**headers, "If-Match": f'"{version}"', "Idempotency-Key": key}, json={"reason_text": "Handled in provider"})
        assert ack(current["version"], "ack1").json()["code"] == "external_resolution_pending"
        fact("needs_action", "instance-1")
        current = client.get(url, headers=headers).json()["data"]["termination"]
        acknowledged = ack(current["version"], "ack2")
        assert acknowledged.status_code == 200
        resolved = acknowledged.json()["data"]["termination"]
        assert resolved["external_status"] == "resolved"
        fact("needs_action", "instance-1")
        assert client.get(url, headers=headers).json()["data"]["termination"]["version"] == resolved["version"]
        fact("needs_action", "instance-2", "g2")
        reopened = client.get(url, headers=headers).json()["data"]
        assert reopened["stage"] == "withdrawn"
        assert reopened["termination"]["external_status"] == "pending"
        assert len(reopened["termination"]["resolution_history"]) == 1
        pending = client.get("/api/v1/application-terminations/pending", headers=headers).json()["data"]
        assert [item["application_id"] for item in pending["items"]] == [application_id]


def test_scope_validation_terminal_and_error_cache_contract(tmp_path):
    app, admin, application_id = setup_application(tmp_path)
    seed_user(app, "hiring_manager", "reader@example.test")
    seed_user(app, "recruiter", "unscoped@example.test")
    url = f"/api/v1/applications/{application_id}/termination"
    with TestClient(app) as client:
        for email in ["reader@example.test", "unscoped@example.test"]:
            headers = login(client, email)
            denied = client.get(url, headers=headers)
            assert denied.status_code == 404
            assert denied.headers["Cache-Control"] == "no-store"
            assert client.get("/api/v1/application-terminations/pending", headers=headers).json()["data"] == {"items": [], "total": 0}
        headers = login(client, "terminate@example.test")
        body = {"reason_text": "withdraw", "preview_version": client.get(url, headers=headers).json()["data"]["preview_version"]}
        assert client.post(url, headers={**headers, "Idempotency-Key": "missing-version"}, json=body).status_code == 428
        invalid = client.post(url, headers={**headers, "If-Match": '"1"', "Idempotency-Key": "blank"}, json={**body, "reason_text": "  "})
        assert invalid.status_code == 422
        assert invalid.headers["Cache-Control"] == "no-store"
        stale = client.post(url, headers={**headers, "If-Match": '"2"', "Idempotency-Key": "stale"}, json=body)
        assert stale.status_code == 409 and stale.json()["code"] == "resource_version_conflict"
        with app.state.identity_store.sync_session() as db:
            application = db.get(Application, uuid.UUID(application_id))
            application.stage = "rejected"
            db.commit()
        preview = client.get(url, headers=headers).json()["data"]
        assert preview["allowed_actions"]["terminate"] is False
        terminal = client.post(url, headers={**headers, "If-Match": '"1"', "Idempotency-Key": "terminal"}, json={**body, "preview_version": preview["preview_version"]})
        assert terminal.json()["code"] == "application_already_terminal"


def test_preview_exposes_known_external_items_without_mutation_and_mirrors_stored_items(tmp_path):
    from datetime import datetime, timedelta, timezone
    from server.app.identity.models import Department
    from server.app.integrations.feishu.models import FeishuInterviewSync
    from server.app.interviews.models import Interview
    from server.app.offers.models import OfferResponse
    from server.app.offers.schemas import OfferCommand
    from server.app.offers.service import create_offer
    from server.app.onboarding.models import OnboardingRecord
    from server.app.recruiting.termination_models import ApplicationTermination

    app, admin, application_id = setup_application(tmp_path, "passed")
    timestamp = datetime.now(timezone.utc)
    generation = uuid.uuid4()
    with app.state.identity_store.sync_session() as db:
        application = db.get(Application, uuid.UUID(application_id))
        org = application.organization_id
        offer = create_offer(db, org, admin, OfferCommand(application_id=application.id,
            candidate_response_deadline=timestamp + timedelta(days=1), content={"body": "Offer"}), trace_id="preview-seed")
        response = OfferResponse(organization_id=org, offer_id=offer.id, status="accepted", responded_at=timestamp)
        department = Department(organization_id=org, name="Test department")
        db.add_all([response, department])
        db.flush()
        onboarding = OnboardingRecord(organization_id=org, application_id=application.id,
            candidate_id=application.candidate_id, job_id=application.job_id, offer_response_id=response.id,
            offer_id=offer.id, department_id=department.id, job_title="Role", department_name="Test department",
            expected_start_date=timestamp.date(), pii_ciphertext=b"private-pii-not-for-preview", status="submitted",
            generation=generation, started_by=admin, started_at=timestamp, submitted_at=timestamp,
            feishu_instance_code="known-instance")
        db.add(onboarding)
        interview = Interview(organization_id=org, application_id=application.id, round_name="Technical",
            method="phone", timezone="UTC", starts_at=timestamp, ends_at=timestamp + timedelta(hours=1),
            status="feedback_completed", owner_id=admin, created_by=admin)
        db.add(interview)
        db.flush()
        db.add(FeishuInterviewSync(organization_id=org, interview_id=interview.id,
            external_event_id="known-calendar-event", desired_action="create", sync_status="synced", idempotency_key=generation))
        db.commit()
        onboarding_id = onboarding.id
        interview_id = interview.id
    url = f"/api/v1/applications/{application_id}/termination"
    expected = [{"kind": "feishu_onboarding", "resource_id": str(onboarding_id),
        "state": "needs_action", "reference": "known-instance", "generation": str(generation)},
        {"kind": "calendar", "resource_id": str(interview_id), "state": "needs_action",
        "reference": "known-calendar-event", "generation": str(generation)}]
    with TestClient(app) as client:
        headers = login(client, "terminate@example.test")
        preview = client.get(url, headers=headers)
        assert preview.status_code == 200
        assert preview.json()["data"]["external_items"] == expected
        assert client.get(url, headers=headers).json() == preview.json()
        assert "private-pii" not in preview.text
        with app.state.identity_store.sync_session() as db:
            assert db.scalar(select(ApplicationTermination)) is None
            assert db.get(Application, uuid.UUID(application_id)).stage == "passed"
            row = db.get(OnboardingRecord, onboarding_id)
            assert (row.status, row.version, row.generation, row.pii_ciphertext) == ("submitted", 1, generation, b"private-pii-not-for-preview")
        result = client.post(url, headers={**headers, "If-Match": '"1"', "Idempotency-Key": "external-preview"},
            json={"reason_text": "Withdraw", "preview_version": preview.json()["data"]["preview_version"]})
        assert result.status_code == 200, result.text
        assert result.json()["data"]["external_items"] == result.json()["data"]["termination"]["external_items"] == expected
