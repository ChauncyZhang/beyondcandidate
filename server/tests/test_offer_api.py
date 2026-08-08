from datetime import datetime, timezone
from uuid import UUID

from fastapi.testclient import TestClient
from sqlalchemy import select

from server.app.core.settings import Settings
from server.app.identity.models import Job, JobCollaborator, User
from server.app.main import create_app
from server.app.offers.models import OfferApproval, OfferTemplate
from server.app.recruiting.models import Application, Candidate, FileObject, Resume
from server.tests.test_recruiting_api import login, seed_user


class Probe:
    async def check(self) -> None:
        pass


def make_app(tmp_path):
    app = create_app(
        settings=Settings(
            environment="test",
            database_url=f"sqlite+aiosqlite:///{tmp_path / 'offer-api.db'}",
            cors_origins=["https://hr.example.test"],
        ),
        database_probe=Probe(),
        storage_probe=Probe(),
        initialize_identity_schema=True,
    )
    app.state.identity_store.create_schema()
    return app


def test_offer_api_registers_internal_routes_and_marks_responses_no_store(tmp_path) -> None:
    """Removing the authenticated Offer router must make this contract fail."""
    app = make_app(tmp_path)
    with TestClient(app) as client:
        schema = client.get("/openapi.json").json()
        response = client.get("/api/v1/offers")
        template_response = client.get("/api/v1/offer-templates")

    assert {"post", "get"} <= set(schema["paths"]["/api/v1/offers"])
    assert "get" in schema["paths"]["/api/v1/offers/{offer_id}"]
    assert "post" in schema["paths"]["/api/v1/offers/{offer_id}/approvals"]
    assert "post" in schema["paths"]["/api/v1/offer-approvals/{approval_id}/decisions"]
    assert response.status_code == 401
    assert response.headers["Cache-Control"] == "no-store"
    assert template_response.status_code == 401
    assert template_response.headers["Cache-Control"] == "no-store"


def test_public_offer_routes_are_unauthenticated_but_generic_and_non_cacheable(tmp_path) -> None:
    app = make_app(tmp_path)
    token = "x" * 43
    with TestClient(app) as client:
        get_response = client.get(f"/api/public/v1/offers/{token}")
        pdf_response = client.get(f"/api/public/v1/offers/{token}/pdf")
        response_post = client.post(f"/api/public/v1/offers/{token}/responses", json={"decision": "declined"}, headers={"Origin": "https://evil.example.test"})
    for response in (get_response, pdf_response, response_post):
        assert response.status_code == 404
        assert response.json()["code"] == "offer_link_invalid"
        assert response.headers["Cache-Control"] == "no-store"
        assert response.headers["Referrer-Policy"] == "no-referrer"
        assert "default-src 'none'" in response.headers["Content-Security-Policy"]


def seed_offer_application(app):
    admin_id = seed_user(app, "recruiting_admin", "offer-admin@example.test")
    approver_id = seed_user(app, "hiring_manager", "offer-approver@example.test")
    viewer_id = seed_user(app, "recruiter", "offer-viewer@example.test")
    with app.state.identity_store.sync_session() as db:
        admin = db.get(User, admin_id)
        template = OfferTemplate(organization_id=admin.organization_id, name="职位默认 Offer", content={"body": "默认正文"}, status="active")
        db.add(template); db.flush()
        job = Job(organization_id=admin.organization_id, title="Offer role", owner_id=admin_id, status="open", offer_approver_id=approver_id, offer_template_id=template.id)
        candidate = Candidate(organization_id=admin.organization_id, display_name="Candidate")
        file = FileObject(organization_id=admin.organization_id, storage_key="private/resume", original_filename="resume.pdf", mime_type="application/pdf", size_bytes=1, sha256="0" * 64, uploaded_by=admin_id)
        db.add_all([job, candidate, file]); db.flush()
        resume = Resume(organization_id=admin.organization_id, candidate_id=candidate.id, file_object_id=file.id, version_number=1)
        db.add(resume); db.flush()
        application = Application(organization_id=admin.organization_id, candidate_id=candidate.id, job_id=job.id, resume_id=resume.id, owner_id=admin_id, stage="passed", source="manual")
        db.add_all([application, JobCollaborator(organization_id=admin.organization_id, job_id=job.id, user_id=viewer_id, access_role="job_recruiter")])
        db.commit()
        return {"application_id": str(application.id), "template_id": str(template.id), "admin": "offer-admin@example.test", "approver": "offer-approver@example.test", "viewer": "offer-viewer@example.test"}


def test_offer_lifecycle_is_idempotent_versioned_and_never_auto_sends(tmp_path) -> None:
    """Dropping preconditions, exact approval assignment, or ready_to_send breaks this flow."""
    app = make_app(tmp_path)
    seed = seed_offer_application(app)
    payload = {"application_id": seed["application_id"], "candidate_response_deadline": "2099-08-20T00:00:00Z", "content": {"compensation": "100000"}}
    with TestClient(app) as client:
        admin_headers = {**login(client, seed["admin"]), "Idempotency-Key": "offer-create"}
        created = client.post("/api/v1/offers", json=payload, headers=admin_headers)
        replay = client.post("/api/v1/offers", json=payload, headers=admin_headers)
        assert created.status_code == replay.status_code == 201
        offer = created.json()["data"]
        offer_id = offer["id"]
        missing = client.post(f"/api/v1/offers/{offer_id}/approvals", headers={**login(client, seed["admin"]), "Idempotency-Key": "submit-missing"})
        submitted = client.post(f"/api/v1/offers/{offer_id}/approvals", headers={**login(client, seed["admin"]), "Idempotency-Key": "submit", "If-Match": '"1"'})
        with app.state.identity_store.sync_session() as db:
            approval_id = str(db.scalar(select(OfferApproval.id).where(OfferApproval.offer_id == UUID(offer_id))))
        denied = client.post(f"/api/v1/offer-approvals/{approval_id}/decisions", json={"decision": "approved"}, headers={**login(client, seed["viewer"]), "Idempotency-Key": "not-assignee", "If-Match": '"2"'})
        approver_offer = client.get(f"/api/v1/offers/{offer_id}", headers=login(client, seed["approver"]))
        pending = client.get("/api/v1/offer-approvals/pending", headers=login(client, seed["approver"]))
        approved = client.post(f"/api/v1/offer-approvals/{approval_id}/decisions", json={"decision": "approved"}, headers={**login(client, seed["approver"]), "Idempotency-Key": "approve", "If-Match": '"2"'})
        history = client.get(f"/api/v1/offers/{offer_id}/history", headers=login(client, seed["approver"]))
        redacted = client.get(f"/api/v1/offers/{offer_id}", headers=login(client, seed["viewer"]))
        send = client.post(f"/api/v1/offers/{offer_id}/send", headers={**login(client, seed["admin"]), "Idempotency-Key": "send", "If-Match": '"3"'})

    assert missing.status_code == 428
    assert created.json()["data"]["template_id"] == seed["template_id"]
    assert created.json()["data"]["candidate_name"] == "Candidate"
    assert created.json()["data"]["job_title"] == "Offer role"
    assert created.json()["data"]["allowed_actions"]["send"] is False
    assert submitted.status_code == 200
    assert denied.status_code == 404
    assert approver_offer.status_code == 200
    assert approver_offer.json()["data"]["content"] == {"compensation": "100000"}
    assert approver_offer.json()["data"]["can_view_sensitive_content"] is True
    pending_item = pending.json()["data"][0]
    assert pending_item["candidate_response_deadline"].startswith("2099-08-20T00:00:00")
    assert [{key: value for key, value in item.items() if key != "candidate_response_deadline"} for item in pending.json()["data"]] == [{
        "id": approval_id,
        "offer_id": offer_id,
        "application_id": seed["application_id"],
        "candidate_id": pending.json()["data"][0]["candidate_id"],
        "candidate_name": "Candidate",
        "job_id": pending.json()["data"][0]["job_id"],
        "job_title": "Offer role",
        "offer_status": "pending_approval",
        "offer_version": 2,
        "sequence": 1,
        "round_number": 1,
        "version_number": 1,
    }]
    assert approved.status_code == 200
    assert approved.json()["data"]["status"] == "ready_to_send"
    assert len(history.json()["data"]["versions"]) == 1
    assert history.json()["data"]["versions"][0]["content"] == {"compensation": "100000"}
    assert history.json()["data"]["approvals"][0]["status"] == "approved"
    assert [event["event_type"] for event in history.json()["data"]["events"]] == [
        "offer.created", "offer.submitted", "offer.approval_approved"
    ]
    assert redacted.json()["data"]["content"] == {"redacted": True}
    assert send.status_code == 409
    assert send.json()["code"] == "offer_not_ready_to_send"
    for response in (created, replay, missing, submitted, denied, approver_offer, pending, approved, history, redacted, send):
        assert response.headers["Cache-Control"] == "no-store"


def test_offer_template_and_ordered_special_approver_settings_are_tenant_admin_only(tmp_path) -> None:
    """Removing tenant-admin checks, ETags, or eligible-user validation breaks settings management."""
    app = make_app(tmp_path)
    seed = seed_offer_application(app)
    with TestClient(app) as client:
        admin = login(client, seed["admin"])
        created = client.post("/api/v1/offer-templates", json={"name": "标准 Offer", "content": {"body": "您好"}}, headers={**admin, "Idempotency-Key": "template-create"})
        template_id = created.json()["data"]["id"]
        updated = client.put(f"/api/v1/offer-templates/{template_id}", json={"name": "标准 Offer", "content": {"body": "更新"}, "status": "active"}, headers={**admin, "Idempotency-Key": "template-update", "If-Match": '"1"'})
        special = client.put("/api/v1/settings/offer-special-approvers", json={"approver_ids": [str(UUID(int=999))]}, headers={**admin, "Idempotency-Key": "invalid-special", "If-Match": '"0"'})
        with app.state.identity_store.sync_session() as db:
            approver_id = db.scalar(select(User.id).where(User.email == seed["approver"]))
        configured = client.put("/api/v1/settings/offer-special-approvers", json={"approver_ids": [str(approver_id)]}, headers={**admin, "Idempotency-Key": "special", "If-Match": '"0"'})
        recruiter_templates = client.get("/api/v1/offer-templates", headers=login(client, seed["viewer"]))
        recruiter_create = client.post("/api/v1/offer-templates", json={"name": "越权模板", "content": {"body": "x"}}, headers={**login(client, seed["viewer"]), "Idempotency-Key": "denied-template-create"})

    assert created.status_code == 201 and created.headers["ETag"] == '"1"'
    assert updated.status_code == 200 and updated.headers["ETag"] == '"2"'
    assert special.status_code == 422
    assert configured.status_code == 200
    assert configured.json()["data"]["approver_ids"] == [str(approver_id)]
    assert recruiter_templates.status_code == 200
    assert {item["name"] for item in recruiter_templates.json()["data"]} >= {"职位默认 Offer", "标准 Offer"}
    assert recruiter_create.status_code == 404
