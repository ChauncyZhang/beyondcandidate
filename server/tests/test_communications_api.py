import uuid

from fastapi.testclient import TestClient
from sqlalchemy import select

from server.app.communications.models import EmailDelivery, EmailProviderConfig, EmailTemplate
from server.app.queue.models import BackgroundJob
from server.tests.test_screening_api import app_and_seed, login


def test_email_config_is_encrypted_masked_versioned_and_role_scoped(tmp_path):
    app, _, _ = app_and_seed(tmp_path)
    payload = {"host": "smtp.example.test", "port": 587, "tls_mode": "starttls", "username": "mailer@example.test", "password": "smtp-private", "enabled": True}
    with TestClient(app) as client:
        system = login(client, "system@example.test")
        saved = client.put("/api/v1/settings/email", json=payload, headers={**system, "If-Match": '"0"', "Idempotency-Key": "email-config-v1"})
        assert saved.status_code == 200, saved.text
        assert saved.headers["Cache-Control"] == "no-store"
        assert saved.json()["data"] == {"configured": True, "host": "smtp.example.test", "port": 587, "tls_mode": "starttls", "username": "mailer@example.test", "password_masked": "********", "enabled": True, "version": 1}
        assert "smtp-private" not in saved.text
        stale = client.put("/api/v1/settings/email", json={**payload, "password": None}, headers={**system, "If-Match": '"0"', "Idempotency-Key": "email-config-stale"})
        assert stale.status_code == 409 and stale.json()["code"] == "resource_version_conflict"
        invalid_tls = client.put("/api/v1/settings/email", json={**payload, "tls_mode": "none"}, headers={**system, "If-Match": '"1"', "Idempotency-Key": "email-config-none"})
        assert invalid_tls.status_code == 422
        client.post("/api/v1/auth/logout", headers=system)
        recruiting = login(client, "admin@example.test")
        assert client.get("/api/v1/settings/email", headers=recruiting).status_code == 200
        denied = client.put("/api/v1/settings/email", json=payload, headers={**recruiting, "If-Match": '"1"', "Idempotency-Key": "denied"})
        assert denied.status_code == 404
    with app.state.identity_store.sync_session() as db:
        config = db.scalar(select(EmailProviderConfig))
        assert config is not None and b"smtp-private" not in config.encrypted_password


def test_templates_enforce_allowlist_missing_variables_and_header_safety(tmp_path):
    app, _, _ = app_and_seed(tmp_path)
    with TestClient(app) as client:
        headers = login(client, "admin@example.test")
        unknown = client.put("/api/v1/email-templates/interview_invitation", json={"subject_template": "Interview {{secret}}", "body_template": "Hello {{candidate_name}}", "allowed_variables": ["candidate_name"], "enabled": True}, headers={**headers, "If-Match": '"0"', "Idempotency-Key": "unknown"})
        assert unknown.status_code == 422 and unknown.json()["code"] == "template_variables_invalid"
        injected = client.put("/api/v1/email-templates/interview_invitation", json={"subject_template": "Interview\r\nBcc: victim@example.test", "body_template": "Hello {{candidate_name}}", "allowed_variables": ["candidate_name"], "enabled": True}, headers={**headers, "If-Match": '"0"', "Idempotency-Key": "injected"})
        assert injected.status_code == 422
        saved = client.put("/api/v1/email-templates/interview_invitation", json={"subject_template": "Interview for {{job_title}}", "body_template": "Hello {{candidate_name}}", "allowed_variables": ["candidate_name", "job_title"], "enabled": True}, headers={**headers, "If-Match": '"0"', "Idempotency-Key": "template-ok"})
        assert saved.status_code == 200, saved.text
        assert saved.json()["data"]["version"] == 1
    with app.state.identity_store.sync_session() as db:
        template = db.scalar(select(EmailTemplate))
        assert template.variable_allowlist == ["candidate_name", "job_title"]


def test_test_send_history_and_resend_use_saved_config_and_opaque_jobs(tmp_path):
    app, _, _ = app_and_seed(tmp_path)
    with TestClient(app) as client:
        system = login(client, "system@example.test")
        saved = client.put("/api/v1/settings/email", json={"host": "smtp.example.test", "port": 465, "tls_mode": "tls", "username": "mailer", "password": "smtp-private", "enabled": True}, headers={**system, "If-Match": '"0"', "Idempotency-Key": "config"})
        assert saved.status_code == 200
        sent = client.post("/api/v1/settings/email/test", json={"recipient": "responsible.hr@example.com", "reply_to_email": "responsible.hr@example.com", "reply_to_name": "Responsible HR"}, headers={**system, "Idempotency-Key": "fresh-test-send"})
        assert sent.status_code == 202, sent.text
        delivery_id = uuid.UUID(sent.json()["data"]["id"])
        override = client.post("/api/v1/settings/email/test", json={"recipient": "hr@example.com", "host": "evil.example.test"}, headers={**system, "Idempotency-Key": "override"})
        assert override.status_code == 422
        client.post("/api/v1/auth/logout", headers=system)
        admin = login(client, "admin@example.test")
        history = client.get("/api/v1/email-deliveries?limit=20", headers=admin)
        assert history.status_code == 200 and history.headers["Cache-Control"] == "no-store"
        assert history.json()["data"][0]["recipient"] == "r************r@example.com"
        assert "responsible.hr@example.com" not in history.text
        resent = client.post(f"/api/v1/email-deliveries/{delivery_id}/resend", headers={**admin, "Idempotency-Key": "resend-once"})
        replay = client.post(f"/api/v1/email-deliveries/{delivery_id}/resend", headers={**admin, "Idempotency-Key": "resend-once"})
        assert resent.status_code == 202 and replay.json() == resent.json()
        first_page = client.get("/api/v1/email-deliveries?limit=1", headers=admin).json()
        second_page = client.get(f"/api/v1/email-deliveries?limit=1&cursor={first_page['meta']['next_cursor']}", headers=admin).json()
        assert first_page["meta"]["next_cursor"] is not None
        assert second_page["meta"]["next_cursor"] is None
        assert second_page["data"][0]["id"] != first_page["data"][0]["id"]
    with app.state.identity_store.sync_session() as db:
        original = db.get(EmailDelivery, delivery_id)
        jobs = db.scalars(select(BackgroundJob).where(BackgroundJob.type == "communications.send_email")).all()
        assert original.recipient_masked == "r************r@example.com"
        assert b"responsible.hr@example.com" not in original.recipient_ciphertext
        assert original.sender_email == app.state.settings.email_from_address
        assert len(db.scalars(select(EmailDelivery)).all()) == 2
        assert jobs[0].payload == {"organization_id": str(original.organization_id), "delivery_id": str(original.id)}
        assert jobs[0].max_attempts == 3
        assert "responsible.hr@example.com" not in str(jobs[0].payload)
