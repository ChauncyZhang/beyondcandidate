"""Durable anonymous intake; account-dependent work runs only in the worker."""

import asyncio
import uuid
from datetime import timedelta, timezone

from sqlalchemy import func, select, text
from sqlalchemy.exc import DisconnectionError, OperationalError, TimeoutError as SqlAlchemyTimeoutError

from server.app.communications.service import (
    DeliveryCommand, SenderPolicy, enqueue_delivery, latest_email_provider_config,
)
from server.app.identity.models import (
    AuditLog, Organization, PasswordInvitation, PasswordRecoveryRequest, User, UserStatus,
)
from server.app.identity.security import hash_token
from server.app.identity.service import Clock
from server.app.queue.payloads import PASSWORD_RECOVERY_PAYLOAD, UnsafePayload
from server.app.queue.repository import QueueRepository
from server.app.queue.service import PermanentJobError, RetryableJobError


REQUEST_EVENT = "identity.password_recovery_received"
ISSUED_EVENT = "identity.password_reset_requested"
RECOVERY_JOB_TYPE = "identity.password_recovery"
LINK_PLACEHOLDER = "{{password_reset_link}}"


def _aware(value):
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def request_password_reset(identity, cipher, settings, organization_slug, email, *, trace_id, network):
    network_id = cipher.fingerprint("password-reset-network", (network or "unknown").split(",", 1)[0].strip())
    with identity.store.sync_session.begin() as db:
        if db.bind.dialect.name == "postgresql":
            db.execute(text("select pg_advisory_xact_lock(hashtextextended(:scope, 0))"),
                {"scope": f"password-reset-network:{network_id}"})
        now = identity.clock.current_time()
        network_count = db.scalar(select(func.count()).select_from(AuditLog).where(
            AuditLog.event_type == REQUEST_EVENT, AuditLog.ip_hash == network_id,
            AuditLog.created_at > now - timedelta(minutes=1),
        ))
        if network_count >= 5:
            return
        organization = db.scalar(select(Organization).where(Organization.slug == organization_slug))
        recovery = None
        # Account, tenant status and SMTP eligibility are deliberately absent
        # from this public path, including when the submitted account is known.
        if organization is not None:
            recovery = PasswordRecoveryRequest(
                id=uuid.uuid4(), organization_id=organization.id,
                email_ciphertext=cipher.encrypt_recipient(email.strip().casefold()),
                network_hash=network_id, created_at=now,
            )
            db.add(recovery)
            db.flush()
            QueueRepository(db).enqueue(organization.id, RECOVERY_JOB_TYPE,
                {"organization_id": str(organization.id), "request_id": str(recovery.id)},
                dedupe_key=f"password-recovery:{recovery.id}", trace_id=trace_id)
        # Worker outcomes never affect the shared ingress network quota.
        db.add(AuditLog(event_type=REQUEST_EVENT, category="system", outcome="received",
            organization_id=organization.id if organization else None,
            resource_type="password_recovery" if recovery else None,
            resource_id=recovery.id if recovery else None,
            ip_hash=network_id, trace_id=trace_id, metadata_json={"network_id": network_id}, created_at=now))


def _issue_password_reset(db, recovery, email, cipher, settings, clock, trace_id):
    organization = db.get(Organization, recovery.organization_id)
    if organization is None or organization.status != "active":
        return
    user = db.scalar(select(User).where(User.organization_id == organization.id,
        User.normalized_email == email.strip().casefold()).with_for_update(of=User))
    now = clock.current_time()
    expires_at = _aware(recovery.created_at) + timedelta(hours=1)
    if user is None or user.status != UserStatus.ACTIVE or expires_at <= now:
        return
    recent = db.scalars(select(AuditLog.created_at).where(
        AuditLog.event_type == ISSUED_EVENT, AuditLog.resource_id == user.id,
        AuditLog.organization_id == organization.id, AuditLog.outcome == "queued",
        AuditLog.created_at > now - timedelta(hours=1),
    )).all()
    if len(recent) >= 5 or any(_aware(value) > now - timedelta(seconds=60) for value in recent):
        return
    config = latest_email_provider_config(db, organization.id)
    if config is None or not config.enabled:
        return
    try:
        with db.begin_nested():
            db.add(PasswordInvitation(id=recovery.id, organization_id=organization.id, user_id=user.id,
                purpose="password_reset", reset_authorization_version=user.authorization_version,
                token_hash=hash_token(cipher.raw_password_reset_token(recovery.id)),
                expires_at=expires_at, created_at=recovery.created_at))
            db.flush()
            enqueue_delivery(db, DeliveryCommand(
                organization_id=organization.id, recipient=user.email,
                subject="重置您的登录密码",
                body=f"您好：\n\n请点击以下链接重置登录密码：\n{LINK_PLACEHOLDER}\n\n链接自申请起 1 小时内有效，且仅可使用一次。如非本人操作，请忽略此邮件。",
                resource_type="password_reset", resource_id=recovery.id,
                idempotency_key=str(recovery.id), operation="password_reset.request", trace_id=trace_id,
            ), cipher=cipher, sender_policy=SenderPolicy(settings.email_from_address, settings.email_from_name))
    except ValueError:
        return
    db.add(AuditLog(event_type=ISSUED_EVENT, category="system", outcome="queued",
        organization_id=organization.id, resource_type="user", resource_id=user.id,
        ip_hash=recovery.network_hash, trace_id=trace_id,
        metadata_json={"network_id": recovery.network_hash}, created_at=now))


def _job_identity(job):
    try:
        payload = PASSWORD_RECOVERY_PAYLOAD.validate(job.payload)
        organization_id = uuid.UUID(payload["organization_id"])
        if uuid.UUID(str(job.organization_id)) != organization_id:
            raise UnsafePayload("job tenant does not match payload")
        return organization_id, uuid.UUID(payload["request_id"])
    except (AttributeError, TypeError, ValueError, UnsafePayload):
        raise PermanentJobError("password_recovery_payload_invalid") from None


class PasswordRecoveryJobHandler:
    def __init__(self, sessions, cipher, settings, *, clock=None):
        self._sessions, self._cipher, self._settings = sessions, cipher, settings
        self._clock = clock or Clock()

    async def __call__(self, job):
        organization_id, request_id = _job_identity(job)
        try:
            await asyncio.to_thread(self._process, organization_id, request_id, getattr(job, "trace_id", None))
        except (OperationalError, DisconnectionError, SqlAlchemyTimeoutError):
            raise RetryableJobError("password_recovery_database_unavailable") from None

    def _process(self, organization_id, request_id, trace_id):
        with self._sessions.begin() as db:
            recovery = db.scalar(select(PasswordRecoveryRequest).where(
                PasswordRecoveryRequest.id == request_id,
                PasswordRecoveryRequest.organization_id == organization_id,
            ).with_for_update(of=PasswordRecoveryRequest))
            if recovery is None or recovery.processed_at is not None:
                return
            now = self._clock.current_time()
            if _aware(recovery.created_at) + timedelta(hours=1) > now:
                try:
                    email = self._cipher.decrypt_recipient(recovery.email_ciphertext)
                except ValueError:
                    email = None
                if email is not None:
                    _issue_password_reset(db, recovery, email, self._cipher, self._settings, self._clock, trace_id)
            recovery.processed_at = self._clock.current_time()
            recovery.email_ciphertext = None


def password_recovery_terminal_callback(db, job, safe_code, now):
    try:
        organization_id, request_id = _job_identity(job)
    except PermanentJobError:
        return
    recovery = db.scalar(select(PasswordRecoveryRequest).where(
        PasswordRecoveryRequest.id == request_id,
        PasswordRecoveryRequest.organization_id == organization_id,
    ).with_for_update(of=PasswordRecoveryRequest))
    if recovery is not None and recovery.processed_at is None:
        recovery.processed_at = now
        recovery.email_ciphertext = None
