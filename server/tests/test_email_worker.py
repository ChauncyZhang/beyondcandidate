import asyncio
import logging
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from server.app.communications.models import EmailDelivery, EmailProviderConfig
from server.app.communications.provider import MailMessage, PermanentMailError, ProviderReceipt, SmtpMailProvider, TemporaryMailError
from server.app.communications.security import EmailSecretCipher
from server.app.communications.service import DeliveryCommand, DeliveryIdempotencyConflict, SenderPolicy, email_delivery_terminal_callback, enqueue_delivery, render_template
from server.app.communications.worker import EmailDeliveryJobHandler
from server.app.identity.models import AuditLog, Base, Organization, User, UserRole
from server.app.integrations.feishu.models import FeishuOrganizationConfig
from server.app.interviews.models import Interview  # noqa: F401 - registers Feishu FK metadata
from server.app.queue.models import BackgroundJob, JobAttempt, OutboxEvent
from server.app.queue.repository import QueueRepository
from server.app.queue.service import PermanentJobError, RetryableJobError
from server.app.recruiting.models import Application  # noqa: F401 - registers notification FK metadata


class FakeMailProvider:
    def __init__(self, failures=None):
        self.failures = list(failures or [])
        self.messages = []

    async def send(self, message):
        self.messages.append(message)
        if self.failures:
            raise self.failures.pop(0)
        return ProviderReceipt("receipt-123")


def delivery_store(tmp_path, *, feishu_state="enabled", automated=False, admin_role=True):
    engine = create_engine(f"sqlite:///{tmp_path / 'email-worker.db'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    cipher = EmailSecretCipher(b"MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY=")
    with sessions.begin() as db:
        organization = Organization(slug="mail-worker", name="Mail Worker", status="active")
        user = User(organization=organization, email="hr@example.test", normalized_email="hr@example.test", display_name="HR", password_hash="x")
        if admin_role:
            user.roles.append(UserRole(role="recruiting_admin"))
        db.add_all([organization, user]); db.flush()
        db.add(EmailProviderConfig(organization_id=organization.id, host="smtp.example.test", port=587, tls_mode="starttls", username="mailer@example.test", encrypted_password=cipher.encrypt_smtp_password("smtp-private"), default_reply_to_email="default-hr@example.com", default_reply_to_name="Default HR", enabled=True, version=1, created_by=user.id, updated_by=user.id))
        if feishu_state != "absent":
            db.add(FeishuOrganizationConfig(organization_id=organization.id, app_id="app", encrypted_app_secret=b"opaque", redirect_uri="https://hr.example.test/callback", calendar_id="primary", enabled=feishu_state == "enabled", version=1, created_by=user.id, updated_by=user.id))
        delivery = enqueue_delivery(db, DeliveryCommand(organization_id=organization.id, recipient="candidate@example.com", reply_to_email="hr@example.com", reply_to_name="Responsible HR", subject="Interview invitation", body="Hello Candidate", resource_type="test", resource_id=uuid.uuid4(), idempotency_key="worker-delivery", operation="test.worker", created_by=None if automated else user.id), cipher=cipher, sender_policy=SenderPolicy("careers@example.com", "BeyondCandidate"))
        job = db.scalar(select(BackgroundJob).where(BackgroundJob.type == "communications.send_email"))
    return sessions, cipher, delivery.id, job


def _make_delivery_job_terminal(sessions, job):
    with sessions.begin() as db:
        stored_job = db.get(BackgroundJob, job.id)
        now = QueueRepository(db).database_now()
        stored_job.status = "running"; stored_job.attempts = 3; stored_job.max_attempts = 3
        stored_job.lease_owner = "worker-1"; stored_job.lease_expires_at = now.replace(year=2099)
        db.add(JobAttempt(organization_id=stored_job.organization_id, job_id=stored_job.id, attempt_no=3, started_at=now, worker_id="worker-1"))


def test_email_cipher_separates_smtp_recipient_and_idempotency_purposes():
    cipher = EmailSecretCipher(b"MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY=")
    password_token = cipher.encrypt_smtp_password("same-value")
    recipient_token = cipher.encrypt_recipient("same-value")
    assert password_token != recipient_token
    assert cipher.decrypt_smtp_password(password_token) == "same-value"
    assert cipher.decrypt_recipient(recipient_token) == "same-value"
    with pytest.raises(ValueError):
        cipher.decrypt_recipient(password_token)
    first = cipher.fingerprint("email.config.test", {"recipient": "candidate@example.com"})
    assert first == cipher.fingerprint("email.config.test", {"recipient": "candidate@example.com"})
    assert first != cipher.fingerprint("email.delivery.request", {"recipient": "candidate@example.com"})
    assert "candidate" not in first


def test_worker_retries_temporary_smtp_failure_without_marking_sent(tmp_path):
    sessions, cipher, delivery_id, job = delivery_store(tmp_path)
    handler = EmailDeliveryJobHandler(sessions, FakeMailProvider([TemporaryMailError("smtp_timeout")]), cipher)
    with pytest.raises(RetryableJobError) as caught:
        asyncio.run(handler(job))
    assert caught.value.safe_code == "smtp_timeout"
    with sessions() as db:
        stored = db.get(EmailDelivery, delivery_id)
        assert (stored.status, stored.safe_error_code, stored.attempts, stored.version) == ("queued", "smtp_timeout", 1, 3)


def test_worker_uses_fixed_sender_hr_reply_to_and_immutable_snapshots(tmp_path):
    sessions, cipher, delivery_id, job = delivery_store(tmp_path)
    with sessions.begin() as db:
        stored = db.get(EmailDelivery, delivery_id)
        stored.attachment_filename = "interview.ics"
        stored.attachment_content_type = "text/calendar"
        stored.attachment_content = b"BEGIN:VCALENDAR\r\nMETHOD:REQUEST\r\nEND:VCALENDAR\r\n"
    provider = FakeMailProvider()
    asyncio.run(EmailDeliveryJobHandler(sessions, provider, cipher)(job))
    message = provider.messages[0]
    assert (message.sender_email, message.sender_name) == ("careers@example.com", "BeyondCandidate")
    assert (message.reply_to_email, message.reply_to_name) == ("hr@example.com", "Responsible HR")
    assert (message.recipient, message.subject, message.body) == ("candidate@example.com", "Interview invitation", "Hello Candidate")
    assert (message.attachment_filename, message.attachment_content_type) == ("interview.ics", "text/calendar")
    assert message.attachment_content.startswith(b"BEGIN:VCALENDAR\r\n")
    assert message.message_id == f"<email-{delivery_id}@beyondcandidate.internal>"
    with sessions() as db:
        stored = db.get(EmailDelivery, delivery_id)
        assert (stored.status, stored.provider_receipt_id) == ("sent", "receipt-123")


def test_worker_uses_exact_saved_provider_snapshot_when_newer_config_exists(tmp_path, monkeypatch):
    sessions, cipher, delivery_id, job = delivery_store(tmp_path)
    with sessions.begin() as db:
        config = db.scalar(select(EmailProviderConfig).where(EmailProviderConfig.version == 1))
        db.add(EmailProviderConfig(organization_id=config.organization_id, host="smtp-new.example.com", port=465, tls_mode="tls", username="new@example.test", encrypted_password=cipher.encrypt_smtp_password("new-private"), default_reply_to_email=config.default_reply_to_email, default_reply_to_name=config.default_reply_to_name, enabled=True, version=2, created_by=config.created_by, updated_by=config.updated_by))
    captured = {}
    monkeypatch.setattr("server.app.communications.worker.SmtpMailProvider", lambda **kwargs: captured.update(kwargs) or FakeMailProvider())
    asyncio.run(EmailDeliveryJobHandler(sessions, None, cipher)(job))
    assert captured["host"] == "smtp.example.test"
    assert captured["password"] == "smtp-private"
    with sessions() as db:
        assert db.get(EmailDelivery, delivery_id).status == "sent"


def test_disabled_provider_is_a_persisted_hr_visible_final_failure(tmp_path):
    sessions, cipher, delivery_id, job = delivery_store(tmp_path)
    with sessions.begin() as db:
        config = db.scalar(select(EmailProviderConfig).where(EmailProviderConfig.version == 1))
        db.add(EmailProviderConfig(organization_id=config.organization_id, host=config.host, port=config.port, tls_mode=config.tls_mode, username=config.username, encrypted_password=config.encrypted_password, default_reply_to_email=config.default_reply_to_email, default_reply_to_name=config.default_reply_to_name, enabled=False, version=2, created_by=config.created_by, updated_by=config.updated_by))
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
    _make_delivery_job_terminal(sessions, job)
    with sessions.begin() as db:
        QueueRepository(db, terminal_callbacks={"communications.send_email": email_delivery_terminal_callback}).fail(job.organization_id, job.id, "worker-1", safe_code="smtp_timeout", retryable=True)
    with sessions() as db:
        stored = db.get(EmailDelivery, delivery_id)
        assert (stored.status, stored.safe_error_code) == ("failed", "smtp_timeout")


@pytest.mark.parametrize("feishu_state,expected_feishu_events", [("enabled", 1), ("disabled", 0), ("absent", 0)])
def test_terminal_callback_replay_creates_one_durable_notification_independent_of_feishu(tmp_path, feishu_state, expected_feishu_events):
    from server.app.notifications.models import UserNotification

    sessions, _, delivery_id, job = delivery_store(tmp_path, feishu_state=feishu_state)
    with sessions.begin() as db:
        now = QueueRepository(db).database_now()
        email_delivery_terminal_callback(db, job, "smtp_timeout", now)
        email_delivery_terminal_callback(db, job, "smtp_timeout", now)
    with sessions() as db:
        audits = db.scalars(select(AuditLog).where(AuditLog.event_type == "email.delivery_failed")).all()
        assert len(audits) == 1
        notifications = db.scalars(select(UserNotification).where(UserNotification.resource_id == delivery_id)).all()
        assert len(notifications) == 1
        assert notifications[0].organization_id == job.organization_id
        assert notifications[0].event_type == "email_delivery_failed"
        events = db.scalars(select(OutboxEvent).where(OutboxEvent.topic == "feishu.notification.send")).all()
        assert len(events) == expected_feishu_events
        if events:
            assert set(events[0].payload) == {"organization_id", "recipient_user_id", "event_type", "email_delivery_id"}
            assert events[0].payload["event_type"] == "email_delivery_failed"


def test_automated_delivery_falls_back_to_deterministic_recruiting_admin(tmp_path):
    from server.app.notifications.models import UserNotification

    sessions, _, delivery_id, job = delivery_store(tmp_path, automated=True, feishu_state="absent")
    with sessions.begin() as db:
        older_admin = User(
            organization_id=job.organization_id, email="fallback@example.test", normalized_email="fallback@example.test",
            display_name="Fallback HR", password_hash="x", created_at=datetime(2000, 1, 1, tzinfo=timezone.utc),
        )
        older_admin.roles.append(UserRole(role="recruiting_admin")); db.add(older_admin); db.flush()
        expected_user_id = older_admin.id
        email_delivery_terminal_callback(db, job, "smtp_timeout", QueueRepository(db).database_now())
    with sessions() as db:
        notification = db.scalar(select(UserNotification).where(UserNotification.resource_id == delivery_id))
        assert notification.user_id == expected_user_id


def test_raising_feishu_adapter_cannot_rollback_terminal_core_rows(tmp_path, monkeypatch, caplog):
    from server.app.notifications.models import UserNotification
    from server.app.integrations.feishu import notifications as feishu_notifications

    sessions, _, delivery_id, job = delivery_store(tmp_path, feishu_state="enabled")
    _make_delivery_job_terminal(sessions, job)
    monkeypatch.setattr(feishu_notifications, "schedule_feishu_notification", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("adapter failed")))
    callbacks = {"communications.send_email": email_delivery_terminal_callback}
    with caplog.at_level(logging.ERROR), sessions.begin() as db:
        QueueRepository(db, terminal_callbacks=callbacks).fail(job.organization_id, job.id, "worker-1", safe_code="smtp_timeout", retryable=True)
    with sessions.begin() as db:
        stored_job = db.get(BackgroundJob, job.id)
        email_delivery_terminal_callback(db, stored_job, "smtp_timeout", QueueRepository(db).database_now())
    with sessions() as db:
        assert db.get(BackgroundJob, job.id).status == "dead_letter"
        assert db.get(EmailDelivery, delivery_id).status == "failed"
        assert len(db.scalars(select(AuditLog).where(AuditLog.event_type == "email.delivery_failed")).all()) == 1
        assert len(db.scalars(select(UserNotification).where(UserNotification.resource_id == delivery_id)).all()) == 1
        assert db.scalar(select(OutboxEvent).where(OutboxEvent.topic == "feishu.notification.send")) is None
    assert "email_failure_adapter_scheduling_failed" in caplog.text
    assert "adapter failed" not in caplog.text


def test_no_responsible_user_dead_letters_with_one_operator_incident_and_replay_is_idempotent(tmp_path):
    from server.app.notifications.models import UserNotification

    sessions, _, delivery_id, job = delivery_store(tmp_path, automated=True, admin_role=False, feishu_state="absent")
    _make_delivery_job_terminal(sessions, job)
    callbacks = {"communications.send_email": email_delivery_terminal_callback}
    with sessions.begin() as db:
        QueueRepository(db, terminal_callbacks=callbacks).fail(job.organization_id, job.id, "worker-1", safe_code="smtp_timeout", retryable=True)
    with sessions.begin() as db:
        stored_job = db.get(BackgroundJob, job.id)
        email_delivery_terminal_callback(db, stored_job, "smtp_timeout", QueueRepository(db).database_now())
    with sessions() as db:
        stored_job = db.get(BackgroundJob, job.id)
        delivery = db.get(EmailDelivery, delivery_id)
        assert stored_job.status == "dead_letter"
        assert stored_job.last_error_code == "email_notification_recipient_unavailable"
        assert delivery.status == "failed"
        assert delivery.safe_error_code == "email_notification_recipient_unavailable"
        assert len(db.scalars(select(AuditLog).where(AuditLog.event_type == "email.delivery_failed")).all()) == 1
        incidents = db.scalars(select(AuditLog).where(AuditLog.event_type == "email.notification_recipient_unavailable")).all()
        assert len(incidents) == 1 and incidents[0].category == "system"
        assert db.scalar(select(UserNotification).where(UserNotification.resource_id == delivery_id)) is None


def test_enqueue_delivery_is_transaction_friendly_and_business_idempotent(tmp_path):
    sessions, cipher, delivery_id, _ = delivery_store(tmp_path)
    with sessions.begin() as db:
        original = db.get(EmailDelivery, delivery_id)
        replay = enqueue_delivery(db, DeliveryCommand(organization_id=original.organization_id, recipient="candidate@example.com", reply_to_email=original.reply_to_email, reply_to_name=original.reply_to_name, subject=original.rendered_subject, body=original.rendered_body, resource_type=original.resource_type, resource_id=original.resource_id, idempotency_key="worker-delivery", operation="test.worker", created_by=original.created_by), cipher=cipher, sender_policy=SenderPolicy(original.sender_email, original.sender_name))
        assert replay.id == original.id
    with sessions() as db:
        assert len(db.scalars(select(EmailDelivery)).all()) == 1
        assert len(db.scalars(select(BackgroundJob).where(BackgroundJob.type == "communications.send_email")).all()) == 1


def test_enqueue_delivery_rejects_same_business_key_with_changed_fingerprint(tmp_path):
    sessions, cipher, delivery_id, _ = delivery_store(tmp_path)
    with sessions.begin() as db:
        original = db.get(EmailDelivery, delivery_id)
        with pytest.raises(DeliveryIdempotencyConflict):
            enqueue_delivery(db, DeliveryCommand(
                organization_id=original.organization_id, recipient="different@example.com",
                reply_to_email=original.reply_to_email, reply_to_name=original.reply_to_name,
                subject=original.rendered_subject, body=original.rendered_body,
                resource_type=original.resource_type, resource_id=original.resource_id,
                idempotency_key="worker-delivery", operation="test.worker", created_by=original.created_by,
            ), cipher=cipher, sender_policy=SenderPolicy(original.sender_email, original.sender_name))


def test_smtp_provider_keeps_certificate_verification_and_receipt_opaque(monkeypatch):
    captured = {}
    async def smtp_send(message, **kwargs):
        captured.update(kwargs)
        captured["message_id"] = message["Message-ID"]
        captured["attachments"] = [
            (part.get_content_type(), part.get_filename()) for part in message.iter_attachments()
        ]
        return ({"candidate@example.com": "250 accepted"}, "queued as private-provider-detail")
    monkeypatch.setattr("server.app.communications.provider.aiosmtplib.send", smtp_send)
    provider = SmtpMailProvider(host="smtp.example.com", port=587, tls_mode="starttls", username="mailer", password="private")
    receipt = asyncio.run(provider.send(MailMessage(
        "candidate@example.com", "careers@example.com", "BeyondCandidate",
        "hr@example.com", "HR", "Subject", "Body",
        "<email-test@beyondcandidate.internal>", "interview.ics", "text/calendar",
        b"BEGIN:VCALENDAR\r\nMETHOD:REQUEST\r\nEND:VCALENDAR\r\n",
    )))
    assert captured["start_tls"] is True and captured["use_tls"] is False
    assert captured.get("validate_certs", True) is True
    assert captured["message_id"] == "<email-test@beyondcandidate.internal>"
    assert captured["attachments"] == [("text/calendar", "interview.ics")]
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
