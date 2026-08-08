import uuid
import hashlib
import json

from fastapi.testclient import TestClient
from sqlalchemy import select

from server.app.communications.models import EmailDelivery, EmailProviderConfig, EmailTemplate
from server.app.queue.models import BackgroundJob
from server.app.recruiting.models import IdempotencyRecord
from server.tests.test_recruiting_api import seed_user
from server.tests.test_screening_api import app_and_seed, login


def test_email_config_is_encrypted_masked_versioned_and_role_scoped(tmp_path):
    app, _, _ = app_and_seed(tmp_path)
    payload = {"host": "smtp.example.test", "port": 587, "tls_mode": "starttls", "username": "mailer@example.test", "password": "smtp-private", "default_reply_to_email": "recruiting@example.com", "default_reply_to_name": "Recruiting Team", "enabled": True}
    with TestClient(app) as client:
        system = login(client, "system@example.test")
        initial = client.get("/api/v1/settings/email", headers=system)
        assert initial.json()["data"]["sender_address"] == app.state.settings.email_from_address
        assert initial.json()["data"]["sender_name"] == app.state.settings.email_from_name
        assert initial.json()["data"]["sender_source"] == "process"
        saved = client.put("/api/v1/settings/email", json=payload, headers={**system, "If-Match": '"0"', "Idempotency-Key": "email-config-v1"})
        assert saved.status_code == 200, saved.text
        assert saved.headers["Cache-Control"] == "no-store"
        assert saved.json()["data"] == {"configured": True, "host": "smtp.example.test", "port": 587, "tls_mode": "starttls", "username": "mailer@example.test", "password_masked": "********", "default_reply_to_email": "recruiting@example.com", "default_reply_to_name": "Recruiting Team", "enabled": True, "version": 1, "sender_address": app.state.settings.email_from_address, "sender_name": app.state.settings.email_from_name, "sender_source": "process"}
        assert "smtp-private" not in saved.text
        stale = client.put("/api/v1/settings/email", json={**payload, "password": None}, headers={**system, "If-Match": '"0"', "Idempotency-Key": "email-config-stale"})
        assert stale.status_code == 409 and stale.json()["code"] == "resource_version_conflict"
        invalid_tls = client.put("/api/v1/settings/email", json={**payload, "tls_mode": "none"}, headers={**system, "If-Match": '"1"', "Idempotency-Key": "email-config-none"})
        assert invalid_tls.status_code == 422
        redirected = client.put("/api/v1/settings/email", json={**payload, "host": "attacker.example.com", "password": None}, headers={**system, "If-Match": '"1"', "Idempotency-Key": "redirect-without-password"})
        assert redirected.status_code == 422
        assert redirected.json()["code"] == "password_reentry_required"
        toggled = client.put("/api/v1/settings/email", json={**payload, "password": None, "enabled": False}, headers={**system, "If-Match": '"1"', "Idempotency-Key": "disable-email"})
        assert toggled.status_code == 200 and toggled.json()["data"]["version"] == 2
        client.post("/api/v1/auth/logout", headers=system)
        recruiting = login(client, "admin@example.test")
        assert client.get("/api/v1/settings/email", headers=recruiting).status_code == 404
        denied = client.put("/api/v1/settings/email", json=payload, headers={**recruiting, "If-Match": '"1"', "Idempotency-Key": "denied"})
        assert denied.status_code == 404
    with app.state.identity_store.sync_session() as db:
        configs = db.scalars(select(EmailProviderConfig).order_by(EmailProviderConfig.version)).all()
        assert [row.version for row in configs] == [1, 2]
        assert configs[0].host == configs[1].host == "smtp.example.test"
        assert configs[0].enabled is True and configs[1].enabled is False
        assert {(row.default_reply_to_email, row.default_reply_to_name) for row in configs} == {("recruiting@example.com", "Recruiting Team")}
        assert configs[0].encrypted_password == configs[1].encrypted_password
        assert b"smtp-private" not in configs[0].encrypted_password
        assert app.state.email_secret_cipher.decrypt_smtp_password(configs[0].encrypted_password) == "smtp-private"
        record = db.scalar(select(IdempotencyRecord).where(IdempotencyRecord.operation == "email.config.put", IdempotencyRecord.idempotency_key == "email-config-v1"))
        raw_hash = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        assert record.request_hash != raw_hash


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


def test_test_send_history_and_resend_use_saved_config_and_opaque_jobs(tmp_path, monkeypatch):
    app, _, _ = app_and_seed(tmp_path)
    seed_user(app, "recruiter", "generic-resend-recruiter@example.test")
    with TestClient(app) as client:
        system = login(client, "system@example.test")
        saved = client.put("/api/v1/settings/email", json={"host": "smtp.example.test", "port": 465, "tls_mode": "tls", "username": "mailer", "password": "smtp-private", "default_reply_to_email": "saved-reply@example.com", "default_reply_to_name": "Saved Reply", "enabled": True}, headers={**system, "If-Match": '"0"', "Idempotency-Key": "config"})
        assert saved.status_code == 200
        sent = client.post("/api/v1/settings/email/test", json={"recipient": "responsible.hr@example.com"}, headers={**system, "Idempotency-Key": "fresh-test-send"})
        assert sent.status_code == 202, sent.text
        delivery_id = uuid.UUID(sent.json()["data"]["id"])
        explicit = client.post("/api/v1/settings/email/test", json={"recipient": "override-recipient@example.com", "reply_to_email": "override@example.com", "reply_to_name": "Override Reply"}, headers={**system, "Idempotency-Key": "explicit-test-send"})
        assert explicit.status_code == 202, explicit.text
        assert client.post("/api/v1/settings/email/test", json={"recipient": "hr@example.com", "reply_to_email": "partial@example.com"}, headers={**system, "Idempotency-Key": "partial"}).status_code == 422
        override = client.post("/api/v1/settings/email/test", json={"recipient": "hr@example.com", "host": "evil.example.test"}, headers={**system, "Idempotency-Key": "override"})
        assert override.status_code == 422
        client.post("/api/v1/auth/logout", headers=system)
        admin = login(client, "admin@example.test")
        history = client.get("/api/v1/email-deliveries?limit=20", headers=admin)
        assert history.status_code == 200 and history.headers["Cache-Control"] == "no-store"
        assert "r************r@example.com" in {row["recipient"] for row in history.json()["data"]}
        assert "responsible.hr@example.com" not in history.text
        missing_match = client.post(f"/api/v1/email-deliveries/{delivery_id}/resend", headers={**admin, "Idempotency-Key": "resend-missing-match"})
        assert missing_match.status_code == 428
        monkeypatch.setattr(
            app.state.email_secret_cipher,
            "decrypt_recipient",
            lambda *_: (_ for _ in ()).throw(AssertionError("API must not decrypt recipients")),
        )
        resent = client.post(f"/api/v1/email-deliveries/{delivery_id}/resend", headers={**admin, "If-Match": '"1"', "Idempotency-Key": "resend-once"})
        replay = client.post(f"/api/v1/email-deliveries/{delivery_id}/resend", headers={**admin, "If-Match": '"1"', "Idempotency-Key": "resend-once"})
        assert resent.status_code == 202 and replay.json() == resent.json()
        assert resent.json()["data"]["version"] == 1
        stale = client.post(f"/api/v1/email-deliveries/{delivery_id}/resend", headers={**admin, "If-Match": '"1"', "Idempotency-Key": "resend-stale"})
        assert stale.status_code == 409 and stale.json()["code"] == "resource_version_conflict"
        first_page = client.get("/api/v1/email-deliveries?limit=1", headers=admin).json()
        second_page = client.get(f"/api/v1/email-deliveries?limit=1&cursor={first_page['meta']['next_cursor']}", headers=admin).json()
        third_page = client.get(f"/api/v1/email-deliveries?limit=1&cursor={second_page['meta']['next_cursor']}", headers=admin).json()
        assert first_page["meta"]["next_cursor"] is not None
        assert second_page["meta"]["next_cursor"] is not None
        assert third_page["meta"]["next_cursor"] is None
        assert second_page["data"][0]["id"] != first_page["data"][0]["id"]
        recruiter_login = client.post(
            "/api/v1/auth/login",
            json={
                "organization_slug": "acme",
                "email": "generic-resend-recruiter@example.test",
                "password": "correct horse",
            },
            headers={"Origin": "https://hr.example.test"},
        )
        assert recruiter_login.status_code == 200
        recruiter = {
            "Origin": "https://hr.example.test",
            "X-CSRF-Token": recruiter_login.headers["X-CSRF-Token"],
        }
        denied_generic_resend = client.post(
            f"/api/v1/email-deliveries/{delivery_id}/resend",
            headers={
                **recruiter,
                "If-Match": '"2"',
                "Idempotency-Key": "generic-recruiter-resend-denied",
            },
        )
        assert (denied_generic_resend.status_code, denied_generic_resend.json()["code"]) == (
            404,
            "resource_not_found",
        )
    with app.state.identity_store.sync_session() as db:
        original = db.get(EmailDelivery, delivery_id)
        explicit_delivery = db.get(EmailDelivery, uuid.UUID(explicit.json()["data"]["id"]))
        config = db.scalar(select(EmailProviderConfig).where(EmailProviderConfig.version == 1))
        jobs = db.scalars(select(BackgroundJob).where(BackgroundJob.type == "communications.send_email")).all()
        assert original.recipient_masked == "r************r@example.com"
        assert b"responsible.hr@example.com" not in original.recipient_ciphertext
        assert original.sender_email == app.state.settings.email_from_address
        assert (original.reply_to_email, original.reply_to_name) == ("saved-reply@example.com", "Saved Reply")
        assert (explicit_delivery.reply_to_email, explicit_delivery.reply_to_name) == ("override@example.com", "Override Reply")
        assert (config.default_reply_to_email, config.default_reply_to_name) == ("saved-reply@example.com", "Saved Reply")
        assert original.version == 2
        assert len(db.scalars(select(EmailDelivery)).all()) == 3
        assert jobs[0].payload == {"organization_id": str(original.organization_id), "delivery_id": str(original.id)}
        assert jobs[0].max_attempts == 3
        assert "responsible.hr@example.com" not in str(jobs[0].payload)
        records = db.scalars(select(IdempotencyRecord).where(IdempotencyRecord.operation == "email.config.test")).all()
        assert len(records) == 2
        raw_test_hash = hashlib.sha256(json.dumps({"recipient":"responsible.hr@example.com","reply_to_email":None,"reply_to_name":None}, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        assert all(record.request_hash != raw_test_hash for record in records)
