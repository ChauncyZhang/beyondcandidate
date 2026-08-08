import asyncio
import logging
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from server.app.communications.models import EmailDelivery, EmailProviderConfig
from server.app.communications.provider import MailMessage, PermanentMailError, ProviderReceipt, SmtpMailProvider, TemporaryMailError
from server.app.communications.security import EmailSecretCipher
from server.app.communications.service import DeliveryCommand, email_delivery_terminal_callback, enqueue_delivery, render_template
from server.app.communications.worker import EmailDeliveryJobHandler
from server.app.identity.models import AuditLog, Base, Organization, User
from server.app.queue.models import BackgroundJob, JobAttempt
from server.app.queue.repository import QueueRepository
from server.app.queue.service import PermanentJobError, RetryableJobError


class FakeMailProvider:
    def __init__(self, failures=None):
        self.failures = list(failures or [])
        self.messages = []

    async def send(self, message):
        self.messages.append(message)
        if self.failures:
            raise self.failures.pop(0)
        return ProviderReceipt("receipt-123")


def delivery_store(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'email-worker.db'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    cipher = EmailSecretCipher(b"MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY=")
    with sessions.begin() as db:
        organization = Organization(slug="mail-worker", name="Mail Worker", status="active")
        user = User(organization=organization, email="hr@example.test", normalized_email="hr@example.test", display_name="HR", password_hash="x")
        db.add_all([organization, user]); db.flush()
        db.add(EmailProviderConfig(organization_id=organization.id, host="smtp.example.test", port=587, tls_mode="starttls", username="mailer@example.test", encrypted_password=cipher.encrypt("smtp-private"), enabled=True, version=1, created_by=user.id, updated_by=user.id))
        delivery = enqueue_delivery(db, DeliveryCommand(organization_id=organization.id, recipient="candidate@example.com", sender_email="careers@example.com", sender_name="BeyondCandidate", reply_to_email="hr@example.com", reply_to_name="Responsible HR", subject="Interview invitation", body="Hello Candidate", resource_type="test", resource_id=uuid.uuid4(), idempotency_key="worker-delivery"), cipher=cipher)
        job = db.scalar(select(BackgroundJob).where(BackgroundJob.type == "communications.send_email"))
        return sessions, cipher, delivery.id, job


def test_worker_retries_temporary_smtp_failure_without_marking_sent(tmp_path):
    sessions, cipher, delivery_id, job = delivery_store(tmp_path)
    handler = EmailDeliveryJobHandler(sessions, FakeMailProvider([TemporaryMailError("smtp_timeout")]), cipher)
    with pytest.raises(RetryableJobError) as caught:
        asyncio.run(handler(job))
    assert caught.value.safe_code == "smtp_timeout"
    with sessions() as db:
        stored = db.get(EmailDelivery, delivery_id)
        assert (stored.status, stored.safe_error_code, stored.attempts) == ("queued", "smtp_timeout", 1)


def test_worker_uses_fixed_sender_hr_reply_to_and_immutable_snapshots(tmp_path):
    sessions, cipher, delivery_id, job = delivery_store(tmp_path)
    provider = FakeMailProvider()
    asyncio.run(EmailDeliveryJobHandler(sessions, provider, cipher)(job))
    message = provider.messages[0]
    assert (message.sender_email, message.sender_name) == ("careers@example.com", "BeyondCandidate")
    assert (message.reply_to_email, message.reply_to_name) == ("hr@example.com", "Responsible HR")
    assert (message.recipient, message.subject, message.body) == ("candidate@example.com", "Interview invitation", "Hello Candidate")
    with sessions() as db:
        stored = db.get(EmailDelivery, delivery_id)
        assert (stored.status, stored.provider_receipt_id) == ("sent", "receipt-123")


def test_worker_uses_latest_saved_active_provider_config(tmp_path):
    sessions, cipher, delivery_id, job = delivery_store(tmp_path)
    with sessions.begin() as db:
        config = db.scalar(select(EmailProviderConfig))
        config.version = 2
        config.host = "smtp-new.example.com"
    provider = FakeMailProvider()
    asyncio.run(EmailDeliveryJobHandler(sessions, provider, cipher)(job))
    with sessions() as db:
        assert db.get(EmailDelivery, delivery_id).status == "sent"


def test_disabled_provider_is_a_persisted_hr_visible_final_failure(tmp_path):
    sessions, cipher, delivery_id, job = delivery_store(tmp_path)
    with sessions.begin() as db:
        db.scalar(select(EmailProviderConfig)).enabled = False
    with pytest.raises(PermanentJobError) as caught:
        asyncio.run(EmailDeliveryJobHandler(sessions, FakeMailProvider(), cipher)(job))
    assert caught.value.safe_code == "email_configuration_unavailable"
    with sessions() as db:
        stored = db.get(EmailDelivery, delivery_id)
        assert (stored.status, stored.safe_error_code) == ("failed", "email_configuration_unavailable")


def test_permanent_failure_is_safe_and_contains_no_raw_secret_log(tmp_path, caplog):
    sessions, cipher, delivery_id, job = delivery_store(tmp_path)
    raw_error = "550 candidate@example.com mailbox rejected secret-detail"
    provider = FakeMailProvider([PermanentMailError("smtp_recipient_rejected", raw_error)])
    with caplog.at_level(logging.ERROR), pytest.raises(PermanentJobError):
        asyncio.run(EmailDeliveryJobHandler(sessions, provider, cipher)(job))
    with sessions() as db:
        stored = db.get(EmailDelivery, delivery_id)
        assert (stored.status, stored.safe_error_code) == ("failed", "smtp_recipient_rejected")
        assert stored.recipient_masked == "c*******e@example.com"
    assert "candidate@example.com" not in caplog.text and "secret-detail" not in caplog.text


def test_terminal_callback_marks_retry_exhaustion_failed(tmp_path):
    sessions, _, delivery_id, job = delivery_store(tmp_path)
    with sessions.begin() as db:
        stored_job = db.get(BackgroundJob, job.id)
        now = QueueRepository(db).database_now()
        stored_job.status = "running"; stored_job.attempts = 3; stored_job.max_attempts = 3
        stored_job.lease_owner = "worker-1"; stored_job.lease_expires_at = now.replace(year=2099)
        db.add(JobAttempt(organization_id=stored_job.organization_id, job_id=stored_job.id, attempt_no=3, started_at=now, worker_id="worker-1"))
    with sessions.begin() as db:
        QueueRepository(db, terminal_callbacks={"communications.send_email": email_delivery_terminal_callback}).fail(job.organization_id, job.id, "worker-1", safe_code="smtp_timeout", retryable=True)
    with sessions() as db:
        stored = db.get(EmailDelivery, delivery_id)
        assert (stored.status, stored.safe_error_code) == ("failed", "smtp_timeout")


def test_terminal_callback_is_idempotent_for_hr_failure_notification(tmp_path):
    sessions, _, _, job = delivery_store(tmp_path)
    with sessions.begin() as db:
        now = QueueRepository(db).database_now()
        email_delivery_terminal_callback(db, job, "smtp_timeout", now)
        email_delivery_terminal_callback(db, job, "smtp_timeout", now)
    with sessions() as db:
        audits = db.scalars(select(AuditLog).where(AuditLog.event_type == "email.delivery_failed")).all()
        assert len(audits) == 1


def test_enqueue_delivery_is_transaction_friendly_and_business_idempotent(tmp_path):
    sessions, cipher, delivery_id, _ = delivery_store(tmp_path)
    with sessions.begin() as db:
        original = db.get(EmailDelivery, delivery_id)
        replay = enqueue_delivery(db, DeliveryCommand(organization_id=original.organization_id, recipient="candidate@example.com", sender_email=original.sender_email, sender_name=original.sender_name, reply_to_email=original.reply_to_email, reply_to_name=original.reply_to_name, subject=original.rendered_subject, body=original.rendered_body, resource_type=original.resource_type, resource_id=original.resource_id, idempotency_key="worker-delivery"), cipher=cipher)
        assert replay.id == original.id
    with sessions() as db:
        assert len(db.scalars(select(EmailDelivery)).all()) == 1
        assert len(db.scalars(select(BackgroundJob).where(BackgroundJob.type == "communications.send_email")).all()) == 1


def test_smtp_provider_keeps_certificate_verification_and_receipt_opaque(monkeypatch):
    captured = {}
    async def smtp_send(message, **kwargs):
        captured.update(kwargs)
        return ({"candidate@example.com": "250 accepted"}, "queued as private-provider-detail")
    monkeypatch.setattr("server.app.communications.provider.aiosmtplib.send", smtp_send)
    provider = SmtpMailProvider(host="smtp.example.com", port=587, tls_mode="starttls", username="mailer", password="private")
    receipt = asyncio.run(provider.send(MailMessage("candidate@example.com", "careers@example.com", "BeyondCandidate", "hr@example.com", "HR", "Subject", "Body")))
    assert captured["start_tls"] is True and captured["use_tls"] is False
    assert captured.get("validate_certs", True) is True
    assert "candidate@example.com" not in receipt.receipt_id
    assert "private-provider-detail" not in receipt.receipt_id


def test_render_template_rejects_missing_unknown_and_injected_header_variables():
    template = SimpleNamespace(subject_template="Hello {{candidate_name}}", body_template="Role {{job_title}}", variable_allowlist=["candidate_name", "job_title"])
    with pytest.raises(ValueError, match="template_variables_invalid"):
        render_template(template, {"candidate_name": "Candidate"})
    with pytest.raises(ValueError, match="template_variables_invalid"):
        render_template(template, {"candidate_name": "Candidate", "job_title": "Engineer", "secret": "private"})
    with pytest.raises(ValueError, match="header_injection"):
        render_template(template, {"candidate_name": "Candidate\r\nBcc: victim@example.com", "job_title": "Engineer"})
