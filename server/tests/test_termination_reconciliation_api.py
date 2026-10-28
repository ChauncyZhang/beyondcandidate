"""Scoped, versioned and idempotent HR rechecks use only original ledger IDs."""

from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from server.app.identity.models import AuditLog, Department
from server.app.offers.models import Offer, OfferResponse, OfferVersion
from server.app.onboarding.models import OnboardingRecord
from server.app.queue.models import OutboxEvent
from server.app.queue.repository import QueueRepository
from server.app.recruiting import termination
from server.app.recruiting.models import Application
from server.app.recruiting.termination_models import ApplicationTermination
from server.tests.test_application_termination import setup_application
from server.tests.test_recruiting_api import login, seed_user


@pytest.fixture
def pending(tmp_path):
    app, actor, application_id = setup_application(tmp_path, "hired")
    now = datetime.now(timezone.utc)
    generation = uuid4()
    with app.state.identity_store.sync_session() as db:
        application = db.get(Application, UUID(application_id))
        org = application.organization_id
        department = Department(organization_id=org, name="Team")
        db.add(department)
        version_id = uuid4()
        offer = Offer(organization_id=org, application_id=application.id, job_id=application.job_id,
            status="accepted", current_version_id=version_id, candidate_response_deadline=now)
        db.add(offer)
        db.flush()
        db.add(OfferVersion(id=version_id, organization_id=org, offer_id=offer.id, version_number=1,
            content={}, is_special=False, candidate_response_deadline=now, created_by=actor))
        db.flush()
        response = OfferResponse(organization_id=org, offer_id=offer.id, status="accepted", responded_at=now)
        db.add(response)
        db.flush()
        onboarding = OnboardingRecord(organization_id=org, application_id=application.id,
            candidate_id=application.candidate_id, job_id=application.job_id, offer_response_id=response.id,
            offer_id=offer.id, department_id=department.id, job_title="Role", department_name="Team",
            expected_start_date=now.date(), pii_ciphertext=b"not-decrypted-by-reconciliation-command",
            status="failed", generation=generation, started_by=actor, started_at=now)
        db.add(onboarding)
        db.flush()
        original = QueueRepository(db).append_outbox(org, "feishu.approval.onboarding.create",
            "feishu_onboarding", onboarding.id, {"organization_id": str(org),
                "onboarding_id": str(onboarding.id), "generation": str(generation)})
        original.payload = {**original.payload, "submission_state": "unknown", "submission_fingerprint": "f" * 64}
        original.status = "published"
        db.flush()
        termination.terminate_application(db, application, expected_version=application.version,
            expected_preview=termination.preview_version(application, termination.dependent_resources(db, application)),
            reason_text="Withdraw", confirm_not_onboarded=True, actor_user_id=actor, trace_id="api-seed")
        db.commit()
        return app, application_id, org, onboarding.id, generation, original.id


def test_hr_recheck_is_versioned_idempotent_and_queues_original_ids(pending):
    app, application_id, org, record_id, generation, original_id = pending
    url = f"/api/v1/applications/{application_id}/termination"
    with TestClient(app) as client:
        headers = login(client, "terminate@example.test")
        before = client.get(url, headers=headers).json()["data"]
        version = before["termination"]["version"]
        assert before["allowed_actions"]["reconcile_external"] is True
        body = {"reason_text": "\u6838\u5bf9\u98de\u4e66\u539f\u8bf7\u6c42\u7ed3\u679c"}
        command_headers = {**headers, "If-Match": f'"{version}"', "Idempotency-Key": "hr-recheck"}
        result = client.post(url + "/reconcile-external", json=body, headers=command_headers)
        assert result.status_code == 200, result.text
        data = result.json()["data"]
        assert data["termination"]["version"] == version + 1
        assert result.headers["ETag"] == f'"{version + 1}"'
        assert data["stage"] == "withdrawn" and data["termination"]["external_status"] == "pending"
        assert client.post(url + "/reconcile-external", json=body, headers=command_headers).json() == result.json()
        assert client.post(url + "/reconcile-external", json={"reason_text": "changed"}, headers=command_headers).json()["code"] == "idempotency_conflict"
        stale = client.post(url + "/reconcile-external", json=body,
            headers={**command_headers, "Idempotency-Key": "stale-recheck"})
        assert stale.status_code == 409 and stale.json()["code"] == "resource_version_conflict"
    with app.state.identity_store.sync_session() as db:
        events = list(db.scalars(select(OutboxEvent).where(OutboxEvent.topic == "feishu.approval.onboarding.reconcile")))
        assert len(events) == 1
        assert events[0].payload == {"organization_id": str(org), "onboarding_id": str(record_id), "generation": str(generation)}
        assert events[0].aggregate_id == record_id and events[0].organization_id == org
        assert db.get(OutboxEvent, original_id).status == "published"
        audit = db.scalar(select(AuditLog).where(AuditLog.event_type == "application.external_reconciliation_requested"))
        assert audit.metadata_json == {"reason_text": body["reason_text"], "request_count": 1}


def test_recheck_requires_scope_preconditions_and_rejects_foreign_identifiers(pending):
    app, application_id, org, record_id, generation, original_id = pending
    seed_user(app, "hiring_manager", "reader@example.test")
    seed_user(app, "recruiter", "unscoped@example.test")
    url = f"/api/v1/applications/{application_id}/termination/reconcile-external"
    body = {"reason_text": "Check original request"}
    with TestClient(app) as client:
        for email in ("reader@example.test", "unscoped@example.test"):
            denied = client.post(url, json=body, headers={**login(client, email),
                "If-Match": '"2"', "Idempotency-Key": "denied"})
            assert denied.status_code == 404
        headers = login(client, "terminate@example.test")
        current = client.get(url.removesuffix("/reconcile-external"), headers=headers).json()["data"]["termination"]["version"]
        assert client.post(url, json=body, headers={**headers, "Idempotency-Key": "missing"}).status_code == 428
        assert client.post(url, json=body, headers={**headers, "If-Match": f'"{current}"'}).status_code == 422
        base = {**headers, "If-Match": f'"{current}"', "Idempotency-Key": "bad"}
        for changed in ({"reason_text": " "}, {**body, "onboarding_id": str(uuid4())}, {**body, "generation": str(uuid4())}):
            assert client.post(url, json=changed, headers=base).status_code == 422
        foreign = url.replace(application_id, str(uuid4()))
        assert client.post(foreign, json=body, headers=base).status_code == 404
        with app.state.identity_store.sync_session() as db:
            original = db.get(OutboxEvent, original_id)
            original.payload = {**original.payload, "organization_id": str(uuid4())}
            db.commit()
        response = client.post(url, json=body, headers=base)
        assert response.status_code == 409 and response.json()["code"] == "external_reconciliation_unavailable"
    with app.state.identity_store.sync_session() as db:
        assert db.scalar(select(OutboxEvent).where(OutboxEvent.topic == "feishu.approval.onboarding.reconcile")) is None


def test_recheck_derives_all_unknown_original_generations_not_just_current(pending):
    app, application_id, org, record_id, generation, original_id = pending
    older_generation = uuid4()
    with app.state.identity_store.sync_session() as db:
        event = QueueRepository(db).append_outbox(org, "feishu.approval.onboarding.create",
            "feishu_onboarding", record_id, {"organization_id": str(org),
                "onboarding_id": str(record_id), "generation": str(older_generation)})
        event.payload = {**event.payload, "submission_state": "unknown"}
        termination.record_external_fact(db, org, UUID(application_id), kind="feishu_onboarding",
            resource_id=record_id, state="unknown", generation=older_generation)
        db.commit()
    base = f"/api/v1/applications/{application_id}/termination"
    with TestClient(app) as client:
        headers = login(client, "terminate@example.test")
        version = client.get(base, headers=headers).json()["data"]["termination"]["version"]
        result = client.post(base + "/reconcile-external", json={"reason_text": "Check every original request"},
            headers={**headers, "If-Match": f'"{version}"', "Idempotency-Key": "all-generations"})
        assert result.status_code == 200, result.text
    with app.state.identity_store.sync_session() as db:
        queued = list(db.scalars(select(OutboxEvent).where(OutboxEvent.topic == "feishu.approval.onboarding.reconcile")))
        assert {event.payload["generation"] for event in queued} == {str(generation), str(older_generation)}
        assert all(event.aggregate_id == record_id for event in queued)
