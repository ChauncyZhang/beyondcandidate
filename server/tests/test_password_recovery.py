import asyncio
import re
from datetime import timedelta

import pytest
from sqlalchemy import event, select

from server.app.communications.models import EmailDelivery, EmailProviderConfig
from server.app.communications.provider import ProviderReceipt
from server.app.communications.worker import EmailDeliveryJobHandler
from server.app.identity.models import AuditLog, Organization, PasswordInvitation, PasswordRecoveryRequest, User, UserSession, UserStatus
from server.app.identity.password_recovery import PasswordRecoveryJobHandler, RECOVERY_JOB_TYPE, REQUEST_EVENT
from server.app.identity.security import hash_token
from server.app.queue.models import BackgroundJob
from server.tests.test_identity_management import management_app, seed_user, login
from server.tests.test_password_reset import ORIGIN, accept, NEW_PASSWORD


def request_reset(client, email="target@example.com", slug="acme", network="192.0.2.1"):
    return client.post("/api/v1/auth/password-reset/request", json={"organization_slug": slug, "email": email}, headers={**ORIGIN, "X-Real-IP": network})


def configure(app, target):
    app.state.settings.offer_public_base_url = "https://trusted.example.com"
    with app.state.identity_store.sync_session.begin() as db:
        db.add(EmailProviderConfig(
            organization_id=target.organization_id, host="smtp.example.com", port=465,
            tls_mode="tls", username="mailer", encrypted_password=app.state.email_secret_cipher.encrypt_smtp_password("smtp-secret"),
            sender_address="security@example.com", sender_name="Security", default_reply_to_email="help@example.com",
            default_reply_to_name="Help", enabled=True, version=1, created_by=target.user_id, updated_by=target.user_id,
        ))


@pytest.mark.parametrize("status", ["active", "disabled", "invited", "inactive_org", "no_smtp"])
def test_public_acknowledgement_never_queries_accounts_or_smtp(management_app, status):
    app, client, _ = management_app
    target = seed_user(app, role="recruiter", email="target@example.com")
    configure(app, target)
    with app.state.identity_store.sync_session.begin() as db:
        if status in {"disabled", "invited"}:
            db.get(User, target.user_id).status = UserStatus(status)
        elif status == "inactive_org":
            db.get(Organization, target.organization_id).status = "inactive"
        elif status == "no_smtp":
            db.delete(db.scalar(select(EmailProviderConfig)))
    statements = []

    def capture(connection, cursor, statement, parameters, context, many):
        statements.append(statement.lower())

    event.listen(app.state.identity_store.engine, "before_cursor_execute", capture)
    try:
        for email in ("target@example.com", "missing@example.com") * 3:
            assert request_reset(client, email=email).status_code == 202
    finally:
        event.remove(app.state.identity_store.engine, "before_cursor_execute", capture)
    assert not any("from users" in statement or "email_provider_configs" in statement or "password_invitations" in statement or "email_deliveries" in statement for statement in statements)
    with app.state.identity_store.sync_session() as db:
        jobs = db.scalars(select(BackgroundJob)).all()
        assert len(jobs) == 5 and all(job.type == RECOVERY_JOB_TYPE for job in jobs)
        assert db.query(PasswordInvitation).count() == db.query(EmailDelivery).count() == 0


def process_recoveries(app, clock):
    handler = PasswordRecoveryJobHandler(app.state.identity_store.sync_session,
        app.state.email_secret_cipher, app.state.settings, clock=clock)
    with app.state.identity_store.sync_session() as db:
        jobs = db.scalars(select(BackgroundJob).where(BackgroundJob.type == RECOVERY_JOB_TYPE)).all()
    for job in jobs:
        asyncio.run(handler(job))


class FakeSmtp:
    def __init__(self):
        self.messages = []

    async def send(self, message):
        self.messages.append(message)
        return ProviderReceipt("fake-smtp-receipt")


def deliver(app, clock):
    provider = FakeSmtp()
    handler = EmailDeliveryJobHandler(app.state.identity_store.sync_session, provider, app.state.email_secret_cipher,
        offer_public_base_url=app.state.settings.offer_public_base_url, clock=clock)
    with app.state.identity_store.sync_session() as db:
        jobs = db.scalars(select(BackgroundJob).where(BackgroundJob.type == "communications.send_email")).all()
    for job in jobs:
        asyncio.run(handler(job))
    return provider.messages


def test_request_queue_send_accept_and_secret_persistence(management_app, monkeypatch):
    app, client, clock = management_app
    target = seed_user(app, role="recruiter", email="target@example.com")
    configure(app, target)
    headers = login(client, "target@example.com")
    response = client.post("/api/v1/auth/password-reset/request", json={"organization_slug": "acme", "email": "TARGET@example.com"}, headers={**headers, "X-Real-IP": "192.0.2.1"})
    assert response.status_code == 202
    assert response.json() == {"data": {"requested": True}}
    assert response.headers["Cache-Control"] == "no-store"
    with app.state.identity_store.sync_session() as db:
        assert db.query(PasswordInvitation).count() == db.query(EmailDelivery).count() == 0
        recovery = db.scalar(select(PasswordRecoveryRequest))
        assert recovery.processed_at is None and b"target@example.com" not in recovery.email_ciphertext
        assert app.state.email_secret_cipher.decrypt_recipient(recovery.email_ciphertext) == "target@example.com"
        recovery_job = db.scalar(select(BackgroundJob))
        assert recovery_job.type == RECOVERY_JOB_TYPE
        assert recovery_job.payload == {"organization_id": str(target.organization_id), "request_id": str(recovery.id)}
    process_recoveries(app, clock)
    with app.state.identity_store.sync_session() as db:
        token = db.scalar(select(PasswordInvitation))
        delivery = db.scalar(select(EmailDelivery))
        assert token.purpose == "password_reset" and token.reset_authorization_version == 1
        assert app.state.identity_service._aware(token.expires_at) == clock.now + timedelta(hours=1)
        assert delivery.resource_type == "password_reset" and delivery.resource_id == token.id
        assert "{{password_reset_link}}" in delivery.rendered_body
        assert all(s.revoked_at is None for s in db.scalars(select(UserSession)))
        recovery = db.scalar(select(PasswordRecoveryRequest))
        assert recovery.processed_at is not None and recovery.email_ciphertext is None
    messages = []

    async def smtp_send(message, **kwargs):
        assert kwargs["hostname"] == "smtp.example.com" and kwargs["password"] == "smtp-secret"
        assert kwargs["validate_certs"] is True and kwargs["use_tls"] is True
        messages.append(message)
        return {}, "accepted"

    monkeypatch.setattr("server.app.communications.provider.aiosmtplib.send", smtp_send)
    with app.state.identity_store.sync_session() as db:
        job = db.scalar(select(BackgroundJob).where(BackgroundJob.type == "communications.send_email"))
    handler = EmailDeliveryJobHandler(app.state.identity_store.sync_session, None, app.state.email_secret_cipher,
        offer_public_base_url=app.state.settings.offer_public_base_url, clock=clock)
    asyncio.run(handler(job))
    assert len(messages) == 1
    message = messages[0]
    assert message["To"] == "target@example.com" and "security@example.com" in message["From"]
    raw = re.search(r"https://trusted.example.com/#reset-password=([A-Za-z0-9_-]+)", message.get_body().get_content()).group(1)
    with app.state.identity_store.sync_session() as db:
        assert db.scalar(select(PasswordInvitation)).token_hash == hash_token(raw)
        for model in (PasswordInvitation, PasswordRecoveryRequest, EmailDelivery, BackgroundJob, AuditLog):
            for row in db.scalars(select(model)):
                assert raw not in repr({column.name: getattr(row, column.name) for column in model.__table__.columns})
        audits = db.scalars(select(AuditLog).where(AuditLog.event_type == "identity.password_reset_requested")).all()
        assert len(audits) == 1 and audits[0].trace_id
        assert set(audits[0].metadata_json) == {"network_id"}
        assert "192.0.2.1" not in repr(audits[0].metadata_json)
    assert client.post("/api/v1/auth/invitations/accept", json={"token": raw, "password": NEW_PASSWORD}, headers=ORIGIN).status_code == 422
    assert accept(client, raw).status_code == 200
    assert client.get("/api/v1/me", headers={"Sec-Fetch-Site": "same-origin"}).status_code == 401
    payload = {"organization_slug": "acme", "email": "target@example.com", "password": "correct horse battery"}
    assert client.post("/api/v1/auth/login", json=payload, headers=ORIGIN).status_code == 401
    payload["password"] = NEW_PASSWORD
    assert client.post("/api/v1/auth/login", json=payload, headers=ORIGIN).status_code == 200
    assert accept(client, raw).status_code == 422


@pytest.mark.parametrize("case", ["unknown", "unknown_org", "disabled", "invited", "inactive_org", "no_smtp", "disabled_smtp"])
def test_generic_ineligible_response_without_token_or_delivery(management_app, case):
    app, client, clock = management_app
    target = seed_user(app, role="recruiter", email="target@example.com")
    configure(app, target)
    with app.state.identity_store.sync_session.begin() as db:
        if case in {"disabled", "invited"}:
            db.get(User, target.user_id).status = UserStatus(case)
        elif case == "inactive_org":
            db.get(Organization, target.organization_id).status = "inactive"
        elif case == "no_smtp":
            db.delete(db.scalar(select(EmailProviderConfig)))
        elif case == "disabled_smtp":
            db.scalar(select(EmailProviderConfig)).enabled = False
    response = request_reset(client, email="missing@example.com" if case == "unknown" else "target@example.com", slug="missing" if case == "unknown_org" else "acme")
    assert response.status_code == 202 and response.json() == {"data": {"requested": True}}
    process_recoveries(app, clock)
    with app.state.identity_store.sync_session() as db:
        assert db.query(PasswordInvitation).count() == db.query(EmailDelivery).count() == 0
        assert db.query(User).count() == 1
        assert db.query(AuditLog).count() == 1
        requests = db.scalars(select(PasswordRecoveryRequest)).all()
        assert len(requests) == (0 if case == "unknown_org" else 1)
        assert all(row.email_ciphertext is None and row.processed_at is not None for row in requests)


def test_origin_validation_and_global_unavailability(management_app):
    app, client, _ = management_app
    path = "/api/v1/auth/password-reset/request"
    payload = {"organization_slug": "acme", "email": "target@example.com"}
    for headers in ({}, {"Origin": "https://evil.example.com"}):
        assert client.post(path, json=payload, headers=headers).status_code == 403
    assert client.post(path, json={**payload, "email": "bad"}, headers=ORIGIN).status_code == 422
    assert request_reset(client).status_code == 503


def test_cooldown_hour_limit_and_preserved_previous_links(management_app):
    app, client, clock = management_app
    target = seed_user(app, role="recruiter", email="target@example.com")
    configure(app, target)
    with app.state.identity_store.sync_session.begin() as db:
        db.add(PasswordInvitation(organization_id=target.organization_id, user_id=target.user_id,
            purpose="password_reset", reset_authorization_version=1, token_hash=hash_token("previous-admin-link-value"), expires_at=clock.now + timedelta(hours=1)))
    for index in range(7):
        assert request_reset(client, network=f"192.0.2.{index + 1}").status_code == 202
        process_recoveries(app, clock)
        if index != 0:
            clock.now += timedelta(seconds=61)
    with app.state.identity_store.sync_session() as db:
        assert db.query(EmailDelivery).count() == 5
        old = db.scalar(select(PasswordInvitation).where(PasswordInvitation.token_hash == hash_token("previous-admin-link-value")))
        assert app.state.identity_service._aware(old.expires_at) > clock.now
    assert accept(client, "previous-admin-link-value").status_code == 200


def test_network_limit_is_shared_across_users_and_unknown_requests(management_app):
    app, client, _ = management_app
    app.state.settings.offer_public_base_url = "https://trusted.example.com"
    for index in range(8):
        assert request_reset(client, email=f"unknown{index}@example.com").status_code == 202
    with app.state.identity_store.sync_session() as db:
        assert db.query(AuditLog).count() == 5
    assert request_reset(client, network="192.0.2.2").status_code == 202
    with app.state.identity_store.sync_session() as db:
        assert db.query(AuditLog).count() == 6


@pytest.mark.parametrize("obsolete", ["used", "expired", "disabled", "invited", "inactive_org", "version", "recipient", "hash", "purpose", "tenant"])
def test_worker_cancels_obsolete_links(management_app, obsolete):
    app, client, clock = management_app
    target = seed_user(app, role="recruiter", email="target@example.com")
    configure(app, target)
    assert request_reset(client).status_code == 202
    process_recoveries(app, clock)
    if obsolete == "tenant":
        other = seed_user(app, role="recruiter", email="other@example.com", organization_slug="other")
    with app.state.identity_store.sync_session.begin() as db:
        token = db.scalar(select(PasswordInvitation))
        user = db.get(User, target.user_id)
        if obsolete == "used": token.used_at = clock.now
        elif obsolete == "expired": token.expires_at = clock.now
        elif obsolete in {"disabled", "invited"}: user.status = UserStatus(obsolete)
        elif obsolete == "inactive_org": db.get(Organization, target.organization_id).status = "inactive"
        elif obsolete == "version": user.authorization_version += 1
        elif obsolete == "recipient": user.normalized_email = user.email = "changed@example.com"
        elif obsolete == "hash": token.token_hash = hash_token("different-token-value")
        elif obsolete == "purpose": token.purpose = "invitation"
        elif obsolete == "tenant":
            foreign = PasswordInvitation(organization_id=other.organization_id, user_id=other.user_id,
                purpose="password_reset", reset_authorization_version=1, token_hash=hash_token("foreign-reset-token-value"),
                expires_at=clock.now + timedelta(hours=1))
            db.add(foreign)
            db.flush()
            db.scalar(select(EmailDelivery)).resource_id = foreign.id
    assert deliver(app, clock) == []
    with app.state.identity_store.sync_session() as db:
        assert db.scalar(select(EmailDelivery)).status == "cancelled"


def test_reset_token_is_deterministic_and_purpose_isolated(management_app):
    from uuid import uuid4
    from server.app.communications.security import EmailSecretCipher

    app, _, _ = management_app
    reset_id = uuid4()
    cipher = app.state.email_secret_cipher
    raw = cipher.raw_password_reset_token(reset_id)
    same_key = EmailSecretCipher(b"MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY=")
    other_key = EmailSecretCipher(b"QEFCQ0RFRkdISUpLTE1OT1BRUlNUVVZXWFlaW1xdXl8=")
    assert raw == same_key.raw_password_reset_token(reset_id)
    assert raw != other_key.raw_password_reset_token(reset_id)
    assert raw != app.state.offer_token_codec.raw_token(reset_id)
    assert raw != cipher.raw_password_reset_token(uuid4())


def test_worker_uses_latest_saved_smtp_and_fallback_sender(management_app, monkeypatch):
    app, client, clock = management_app
    target = seed_user(app, role="recruiter", email="target@example.com")
    configure(app, target)
    with app.state.identity_store.sync_session.begin() as db:
        config = db.scalar(select(EmailProviderConfig))
        config.sender_address = config.sender_name = None
    assert request_reset(client).status_code == 202
    process_recoveries(app, clock)
    with app.state.identity_store.sync_session.begin() as db:
        db.add(EmailProviderConfig(
            organization_id=target.organization_id, host="new.smtp.example.com", port=587,
            tls_mode="starttls", username="new-mailer", encrypted_password=app.state.email_secret_cipher.encrypt_smtp_password("new-secret"),
            default_reply_to_email="newhelp@example.com", default_reply_to_name="New Help", enabled=True,
            version=2, created_by=target.user_id, updated_by=target.user_id))
        job = db.scalar(select(BackgroundJob).where(BackgroundJob.type == "communications.send_email"))
    sent = []

    async def smtp_send(message, **kwargs):
        assert kwargs["hostname"] == "new.smtp.example.com" and kwargs["password"] == "new-secret"
        assert app.state.settings.email_from_address in message["From"]
        assert "newhelp@example.com" in message["Reply-To"]
        sent.append(message)
        return {}, "accepted"

    monkeypatch.setattr("server.app.communications.provider.aiosmtplib.send", smtp_send)
    asyncio.run(EmailDeliveryJobHandler(app.state.identity_store.sync_session, None, app.state.email_secret_cipher,
        offer_public_base_url=app.state.settings.offer_public_base_url, clock=clock)(job))
    assert len(sent) == 1


@pytest.mark.parametrize("delay", [timedelta(minutes=10), timedelta(hours=1)])
def test_worker_expiry_starts_at_request_and_uses_current_version(management_app, delay):
    app, client, clock = management_app
    target = seed_user(app, role="recruiter", email="target@example.com")
    configure(app, target)
    issued_at = clock.now
    assert request_reset(client).status_code == 202
    clock.now += delay
    with app.state.identity_store.sync_session.begin() as db:
        db.get(User, target.user_id).authorization_version = 2
    process_recoveries(app, clock)
    process_recoveries(app, clock)
    with app.state.identity_store.sync_session() as db:
        request = db.scalar(select(PasswordRecoveryRequest))
        assert request.processed_at is not None and request.email_ciphertext is None
        tokens = db.scalars(select(PasswordInvitation)).all()
        assert len(tokens) == (0 if delay == timedelta(hours=1) else 1)
        if tokens:
            assert app.state.identity_service._aware(tokens[0].expires_at) == issued_at + timedelta(hours=1)
            assert tokens[0].reset_authorization_version == 2
        assert db.query(EmailDelivery).count() == len(tokens)


def test_recovery_retry_rolls_back_token_delivery_and_processed_marker(management_app, monkeypatch):
    from sqlalchemy.exc import OperationalError
    from server.app.identity import password_recovery
    from server.app.queue.service import RetryableJobError

    app, client, clock = management_app
    target = seed_user(app, role="recruiter", email="target@example.com")
    configure(app, target)
    assert request_reset(client).status_code == 202
    original_enqueue = password_recovery.enqueue_delivery

    def fail_after_enqueue(*args, **kwargs):
        original_enqueue(*args, **kwargs)
        raise OperationalError("simulated-database-unavailable", None, Exception())

    monkeypatch.setattr(password_recovery, "enqueue_delivery", fail_after_enqueue)
    with pytest.raises(RetryableJobError, match="password_recovery_database_unavailable"):
        process_recoveries(app, clock)
    with app.state.identity_store.sync_session() as db:
        assert db.query(PasswordInvitation).count() == db.query(EmailDelivery).count() == 0
        request = db.scalar(select(PasswordRecoveryRequest))
        assert request.processed_at is None and request.email_ciphertext is not None
        assert db.query(BackgroundJob).count() == 1
    monkeypatch.setattr(password_recovery, "enqueue_delivery", original_enqueue)
    process_recoveries(app, clock)
    process_recoveries(app, clock)
    with app.state.identity_store.sync_session() as db:
        assert db.query(PasswordInvitation).count() == db.query(EmailDelivery).count() == 1
        assert db.scalar(select(PasswordRecoveryRequest)).email_ciphertext is None


def test_worker_bootstrap_payload_validation_and_terminal_cleanup(management_app):
    from uuid import uuid4
    from types import SimpleNamespace
    from server.app.queue.payloads import DEFAULT_PAYLOAD_POLICIES, UnsafePayload
    from server.app.queue.repository import QueueRepository
    from server.app.queue.service import PermanentJobError
    from server.app.worker.main import build_password_recovery_handler, build_terminal_callbacks

    app, client, clock = management_app
    target = seed_user(app, role="recruiter", email="target@example.com")
    configure(app, target)
    assert request_reset(client).status_code == 202
    handler = build_password_recovery_handler(app.state.settings)
    assert isinstance(handler, PasswordRecoveryJobHandler)
    with app.state.identity_store.sync_session() as db:
        job = db.scalar(select(BackgroundJob))
        payload = dict(job.payload)
    with pytest.raises(UnsafePayload):
        DEFAULT_PAYLOAD_POLICIES.validate_job(RECOVERY_JOB_TYPE, {**payload, "email": "target@example.com"})
    with pytest.raises(PermanentJobError, match="password_recovery_payload_invalid"):
        asyncio.run(handler(SimpleNamespace(organization_id=uuid4(), payload=payload)))
    with app.state.identity_store.sync_session.begin() as db:
        queue = QueueRepository(db, terminal_callbacks=build_terminal_callbacks())
        claimed = queue.claim(target.organization_id, "recovery-test-worker", lease_seconds=60)
        assert claimed.type == RECOVERY_JOB_TYPE
        queue.fail(target.organization_id, claimed.id, "recovery-test-worker", safe_code="internal_error", retryable=False)
    with app.state.identity_store.sync_session() as db:
        request = db.scalar(select(PasswordRecoveryRequest))
        assert request.processed_at is not None and request.email_ciphertext is None
        assert db.scalar(select(BackgroundJob)).status == "dead_letter"
        assert db.query(PasswordInvitation).count() == db.query(EmailDelivery).count() == 0


def test_recovery_handler_is_registered_in_normal_worker_bootstrap(management_app, monkeypatch):
    from types import SimpleNamespace
    from server.app.worker import main as worker_main

    app, _, _ = management_app
    captured = {}

    class Worker:
        def __init__(self, *args, **kwargs):
            captured.update(kwargs["handlers"])

        def request_shutdown(self):
            pass

        async def run(self):
            pass

    monkeypatch.setattr(worker_main.Settings, "from_environment", lambda: app.state.settings)
    monkeypatch.setattr(worker_main.GovernanceSettings, "from_environment", lambda _: SimpleNamespace())
    monkeypatch.setattr(worker_main, "build_screening_handlers", lambda *args: {})
    monkeypatch.setattr(worker_main, "build_governance_handlers", lambda *args: {})
    monkeypatch.setattr(worker_main, "build_communications_handler", lambda *args: object())
    monkeypatch.setattr(worker_main, "Worker", Worker)
    monkeypatch.setattr("server.app.queue.runtime.DatabaseQueueGateway", lambda *args, **kwargs: object())
    monkeypatch.setattr("server.app.core.storage.create_storage_client", lambda *args, **kwargs: object())
    monkeypatch.setattr("server.app.db.session.create_engine", lambda *args, **kwargs: object())
    monkeypatch.setattr("server.app.integrations.feishu.worker.build_feishu_outbox_handlers", lambda *args: {})

    async def bootstrap():
        loop = asyncio.get_running_loop()
        monkeypatch.setattr(loop, "add_signal_handler", lambda *args: None)
        await worker_main._run()

    asyncio.run(bootstrap())
    assert isinstance(captured[RECOVERY_JOB_TYPE], PasswordRecoveryJobHandler)
