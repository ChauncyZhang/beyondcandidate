"""Read-only recovery of terminated original Feishu requests, never a new create."""

import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select

from server.app.integrations.feishu.models import FeishuIdentityBinding, FeishuOnboardingConfig, FeishuOrganizationConfig
from server.app.integrations.feishu.provider import ApprovalInstance, FeishuProviderError, HttpFeishuProvider
from server.app.integrations.feishu import worker as workers
from server.app.integrations.feishu.worker import FeishuOnboardingOutboxHandler, build_feishu_outbox_handlers
from server.app.onboarding.models import OnboardingRecord
from server.app.queue.models import OutboxEvent
from server.app.queue.repository import QueueRepository
from server.app.queue.service import PermanentJobError, RetryableJobError
from server.app.recruiting import termination
from server.app.recruiting.models import Application
from server.app.recruiting.termination_models import ApplicationTermination
from server.tests.test_onboarding_qa_repair import lock_trace
from server.tests.test_onboarding_approvers import DEFINITION, NOW, context, start, worker
from server.tests.test_onboarding_termination import real_context, terminate


def test_lost_remote_response_can_be_found_after_termination_without_recreate(real_context):
    ctx = real_context
    queued = start(ctx)
    creates, lookups = [], []

    def response_lost(*args, **kwargs):
        creates.append(kwargs["idempotency_key"])
        terminate(ctx)
        raise FeishuProviderError("feishu_unavailable", retryable=True)

    def found(credentials, approval_code, generation, *, started_at):
        lookups.append((credentials.app_id, approval_code, generation, started_at))
        return ApprovalInstance("original_remote_instance")

    ctx.provider.create_approval_instance = response_lost
    ctx.provider.find_approval_instance_by_uuid = found
    worker(ctx, queued)
    worker(ctx, queued)
    assert len(creates) == 1 and len(lookups) == 1
    assert lookups[0][2] == queued.payload["generation"]
    with ctx.sessions() as db:
        record = db.get(OnboardingRecord, ctx.record_id)
        stopped = db.scalar(select(ApplicationTermination))
        assert record.feishu_instance_code == "original_remote_instance"
        assert record.status == "submitting" and record.submitted_at is None
        assert {item["state"] for item in stopped.external_items} == {"needs_action"}
        assert db.get(Application, ctx.application_id).stage == "withdrawn"


def lost_response(ctx):
    queued = start(ctx)

    def response_lost(*args, **kwargs):
        terminate(ctx)
        raise FeishuProviderError("feishu_unavailable", retryable=True)

    ctx.provider.create_approval_instance = response_lost
    worker(ctx, queued)
    with ctx.sessions.begin() as db:
        original = db.get(OutboxEvent, queued.id)
        original.status = "published"
        original.payload = {**original.payload, "submission_state": "unknown"}
    return queued


def reconcile_event(ctx, generation):
    with ctx.sessions.begin() as db:
        return QueueRepository(db).append_outbox(ctx.org_id, "feishu.approval.onboarding.reconcile",
            "feishu_onboarding", ctx.record_id, {"organization_id": str(ctx.org_id),
                "onboarding_id": str(ctx.record_id), "generation": str(generation)})


@pytest.mark.parametrize("outcome", ["not_found", "transient"])
def test_lookup_uncertainty_stays_unknown_and_later_recheck_can_succeed(real_context, outcome, lock_trace):
    ctx = real_context
    original = lost_response(ctx)
    calls = []

    def lookup(*args, **kwargs):
        assert all(not transaction.is_active for transaction, _ in lock_trace)
        calls.append(True)
        if outcome == "transient":
            raise FeishuProviderError("feishu_unavailable", retryable=True)
        return None

    ctx.provider.find_approval_instance_by_uuid = lookup
    ctx.provider.create_approval_instance = lambda *args, **kwargs: pytest.fail("Reconciliation must never create")
    queued = reconcile_event(ctx, original.payload["generation"])
    handler = FeishuOnboardingOutboxHandler(ctx.sessions, ctx.provider, ctx.secret, ctx.pii)
    code = "feishu_approval_reconciliation_not_found" if outcome == "not_found" else "feishu_approval_reconciliation_lookup_failed"
    with pytest.raises(PermanentJobError, match=code):
        asyncio.run(handler(queued, queued.id))
    with ctx.sessions.begin() as db:
        stopped = db.scalar(select(ApplicationTermination))
        assert {item["state"] for item in stopped.external_items} == {"unknown"}
        record = db.get(OnboardingRecord, ctx.record_id)
        assert record.safe_error_code == code and record.feishu_instance_code is None
        application = termination.lock_application(db, ctx.org_id, ctx.application_id)
        with pytest.raises(termination.TerminationError, match="external_resolution_pending"):
            termination.confirm_external_resolution(db, application, expected_version=stopped.version,
                reason_text="Cannot attest uncertainty", actor_user_id=ctx.actor_id, trace_id="ack")
    ctx.provider.find_approval_instance_by_uuid = lambda *args, **kwargs: ApprovalInstance("found_on_hr_recheck")
    retry = reconcile_event(ctx, original.payload["generation"])
    asyncio.run(handler(retry, retry.id))
    assert len(calls) == 1
    with ctx.sessions() as db:
        assert db.get(OnboardingRecord, ctx.record_id).feishu_instance_code == "found_on_hr_recheck"
        assert db.get(Application, ctx.application_id).stage == "withdrawn"
        assert {item["state"] for item in db.scalar(select(ApplicationTermination)).external_items} == {"needs_action"}


@pytest.mark.parametrize("change,code", [
    ("app", "feishu_approval_reconciliation_tenant_changed"),
    ("tenant", "feishu_approval_reconciliation_tenant_changed"),
    ("config", "feishu_approval_reconciliation_tenant_changed"),
    ("legacy", "feishu_approval_reconciliation_context_missing"),
])
def test_reconciliation_fails_closed_without_original_tenant_context(real_context, change, code):
    ctx = real_context
    with ctx.sessions.begin() as db:
        db.scalar(select(FeishuIdentityBinding)).tenant_key = "original_tenant"
    original = lost_response(ctx)
    with ctx.sessions.begin() as db:
        if change == "app":
            db.scalar(select(FeishuOrganizationConfig)).app_id = "different_app"
        elif change == "tenant":
            db.scalar(select(FeishuIdentityBinding)).tenant_key = "different_tenant"
        elif change == "config":
            db.scalar(select(FeishuOrganizationConfig)).id = uuid4()
        else:
            event = db.get(OutboxEvent, original.id)
            event.payload = {key: value for key, value in event.payload.items() if key != "reconciliation_context"}
    ctx.provider.find_approval_instance_by_uuid = lambda *args, **kwargs: pytest.fail("Must not query an unverified tenant")
    ctx.provider.create_approval_instance = lambda *args, **kwargs: pytest.fail("Must never recreate")
    queued = reconcile_event(ctx, original.payload["generation"])
    handler = FeishuOnboardingOutboxHandler(ctx.sessions, ctx.provider, ctx.secret, ctx.pii)
    with pytest.raises(PermanentJobError, match=code):
        asyncio.run(handler(queued, queued.id))
    with ctx.sessions() as db:
        assert {item["state"] for item in db.scalar(select(ApplicationTermination)).external_items} == {"unknown"}
        assert db.get(OnboardingRecord, ctx.record_id).safe_error_code == code


def test_lookup_uses_original_template_and_start_snapshot_not_mutated_config(real_context):
    ctx = real_context
    original = lost_response(ctx)
    with ctx.sessions.begin() as db:
        ledger = db.get(OutboxEvent, original.id)
        snapshot = ledger.payload["reconciliation_context"]
        attempted_at = ledger.payload["submission_attempted_at"][0]
        assert snapshot["approval_code"] == "approval_onboarding"
        assert snapshot["app_id"] == "cli"
        assert "ou_fixed" not in json.dumps(snapshot)
        config = db.scalar(select(FeishuOnboardingConfig))
        config.approval_code = "new_template"
        config.enabled = False

    def lookup(credentials, approval_code, generation, *, started_at):
        assert approval_code == snapshot["approval_code"]
        assert generation == snapshot["generation"]
        assert started_at.isoformat() == attempted_at
        return ApprovalInstance("original_template_instance")

    ctx.provider.find_approval_instance_by_uuid = lookup
    worker(ctx, original)
    with ctx.sessions() as db:
        assert db.get(OnboardingRecord, ctx.record_id).feishu_instance_code == "original_template_instance"


def test_reconcile_payload_is_bound_to_persisted_organization_and_record(real_context):
    ctx = real_context
    original = lost_response(ctx)
    queued = reconcile_event(ctx, original.payload["generation"])
    handler = FeishuOnboardingOutboxHandler(ctx.sessions, ctx.provider, ctx.secret, ctx.pii)
    for field in ("organization_id", "onboarding_id", "generation"):
        spoofed = SimpleNamespace(payload={**queued.payload, field: str(uuid4())})
        with pytest.raises(PermanentJobError, match="feishu_payload_invalid"):
            asyncio.run(handler(spoofed, queued.id))


def test_build_map_registers_read_only_reconciliation_handler():
    from server.app.core.settings import Settings
    mapping = build_feishu_outbox_handlers(Settings(environment="test", database_url="sqlite://"))
    assert mapping["feishu.approval.onboarding.reconcile"] is mapping["feishu.approval.onboarding.create"]


def test_legacy_attempted_digest_cannot_invent_lookup_context_on_forward_retry(real_context):
    ctx = real_context
    queued = start(ctx)
    calls = []

    def lost(*args, **kwargs):
        calls.append(True)
        raise FeishuProviderError("feishu_unavailable", retryable=True)

    ctx.provider.create_approval_instance = lost
    with pytest.raises(RetryableJobError):
        worker(ctx, queued)
    with ctx.sessions.begin() as db:
        original = db.get(OutboxEvent, queued.id)
        original.payload = {key: value for key, value in original.payload.items() if key != "reconciliation_context"}
        digest = original.payload["submission_fingerprint"]
    with pytest.raises(PermanentJobError, match="feishu_approval_reconciliation_context_missing"):
        worker(ctx, queued)
    assert len(calls) == 1
    with ctx.sessions() as db:
        original = db.get(OutboxEvent, queued.id)
        assert original.payload["submission_fingerprint"] == digest
        assert "reconciliation_context" not in original.payload


class AttemptClock(datetime):
    point = NOW

    @classmethod
    def now(cls, tz=None):
        return cls.point.astimezone(tz) if tz is not None else cls.point.replace(tzinfo=None)


class WindowedHttpProvider(HttpFeishuProvider):
    """Use the real paginated UUID lookup with synthetic read-only HTTP data."""

    def __init__(self, ctx, behavior):
        self.ctx, self.behavior = ctx, behavior
        self.creates, self.remote, self.windows, self.durable_before_create = [], [], [], []

    def get_approval_definition(self, *args):
        return DEFINITION

    def create_approval_instance(self, credentials, request, *, idempotency_key):
        with self.ctx.sessions() as db:
            events = list(db.scalars(select(OutboxEvent).where(OutboxEvent.topic == "feishu.approval.onboarding.create")))
            # The timestamp is visible to a new session before the remote call.
            self.durable_before_create.append(any(AttemptClock.point.isoformat() in event.payload.get("submission_attempted_at", []) for event in events))
        self.creates.append((idempotency_key, AttemptClock.point))
        return self.behavior(self, idempotency_key)

    def add_remote(self, generation):
        self.remote.append(("original_actual_instance", generation, int(AttemptClock.point.timestamp() * 1000)))

    def _headers(self, credentials):
        return {}

    def _json(self, method, url, *, params, headers):
        assert method == "GET", "Reconciliation must remain read-only"
        if url.endswith("/approval/v4/instances"):
            bounds = (int(params["start_time"]), int(params["end_time"]))
            self.windows.append(bounds)
            return {"data": {"instance_code_list": [code for code, _, created in self.remote
                if bounds[0] <= created <= bounds[1]], "has_more": False}}
        code = url.rsplit("/", 1)[-1]
        return {"data": {"uuid": next(generation for stored, generation, _ in self.remote if stored == code)}}


@pytest.mark.parametrize("retry", ["none", "same_event", "new_event"])
def test_actual_http_lookup_covers_delayed_creation_and_every_retry_window(real_context, monkeypatch, retry):
    ctx = real_context
    original = start(ctx)
    monkeypatch.setattr(workers, "datetime", AttemptClock)
    AttemptClock.point = NOW + timedelta(minutes=11)

    def behavior(provider, generation):
        if retry != "none" and len(provider.creates) == 1:
            raise FeishuProviderError("feishu_unavailable", retryable=retry == "same_event")
        provider.add_remote(generation)
        terminate(ctx)
        raise FeishuProviderError("feishu_unavailable", retryable=True)

    provider = WindowedHttpProvider(ctx, behavior)
    ctx.provider = provider
    if retry != "none":
        with pytest.raises(RetryableJobError if retry == "same_event" else PermanentJobError):
            worker(ctx, original)
        current = original
        if retry == "new_event":
            start(ctx)
            with ctx.sessions() as db:
                # SQLite's queue clock has second precision; select the new ID
                # explicitly rather than relying on tied created_at ordering.
                current = db.scalar(select(OutboxEvent).where(OutboxEvent.id != original.id))
                db.expunge(current)
        AttemptClock.point = NOW + timedelta(minutes=40)
    else:
        current = original
    worker(ctx, current)
    # A later HR check must use the original actual attempts, never today's time.
    AttemptClock.point = NOW + timedelta(days=2)
    worker(ctx, current)
    assert len(provider.creates) == (1 if retry == "none" else 2)
    assert len(provider.windows) == len(provider.creates)
    for (_, attempted_at), bounds in zip(provider.creates, provider.windows):
        assert bounds == (int((attempted_at - timedelta(minutes=10)).timestamp() * 1000),
            int((attempted_at + timedelta(minutes=10)).timestamp() * 1000))
    assert all(provider.durable_before_create)
    with ctx.sessions() as db:
        record = db.get(OnboardingRecord, ctx.record_id)
        assert record.feishu_instance_code == "original_actual_instance"
        assert record.status == "submitting" and record.submitted_at is None
        assert {item["state"] for item in db.scalar(select(ApplicationTermination)).external_items} == {"needs_action"}
        events = list(db.scalars(select(OutboxEvent).where(OutboxEvent.topic == "feishu.approval.onboarding.create")))
        for event in events:
            assert event.payload["submission_attempted_at"] == [value.isoformat() for _, value in provider.creates]
            assert datetime.fromisoformat(event.payload["reconciliation_context"]["started_at"]) == NOW


@pytest.mark.parametrize("created_on", [1, 2])
def test_normal_60012_recovery_searches_all_actual_attempt_windows(real_context, monkeypatch, created_on):
    ctx = real_context
    queued = start(ctx)
    monkeypatch.setattr(workers, "datetime", AttemptClock)
    AttemptClock.point = NOW + timedelta(minutes=11)

    def behavior(provider, generation):
        if len(provider.creates) == created_on:
            provider.add_remote(generation)
        if len(provider.creates) == 1:
            raise FeishuProviderError("feishu_unavailable", retryable=True)
        raise FeishuProviderError("feishu_conflict", retryable=False, provider_code=60012)

    provider = WindowedHttpProvider(ctx, behavior)
    ctx.provider = provider
    with pytest.raises(RetryableJobError):
        worker(ctx, queued)
    AttemptClock.point = NOW + timedelta(minutes=40)
    worker(ctx, queued)
    assert len(provider.creates) == 2
    assert len(provider.windows) == created_on
    assert all(provider.durable_before_create)
    with ctx.sessions() as db:
        record = db.get(OnboardingRecord, ctx.record_id)
        assert record.status == "submitted" and record.feishu_instance_code == "original_actual_instance"


@pytest.mark.parametrize("timing,code", [
    (None, "feishu_approval_reconciliation_attempt_times_missing"),
    (["not-a-time"], "feishu_approval_reconciliation_attempt_times_invalid"),
])
def test_missing_or_invalid_attempt_timing_fails_closed(real_context, timing, code):
    ctx = real_context
    original = lost_response(ctx)
    with ctx.sessions.begin() as db:
        event = db.get(OutboxEvent, original.id)
        event.payload = {key: value for key, value in event.payload.items() if key != "submission_attempted_at"}
        if timing is not None:
            event.payload = {**event.payload, "submission_attempted_at": timing}
    ctx.provider.find_approval_instance_by_uuid = lambda *args, **kwargs: pytest.fail("Cannot guess original attempt window")
    queued = reconcile_event(ctx, original.payload["generation"])
    handler = FeishuOnboardingOutboxHandler(ctx.sessions, ctx.provider, ctx.secret, ctx.pii)
    with pytest.raises(PermanentJobError, match=code):
        asyncio.run(handler(queued, queued.id))
    with ctx.sessions.begin() as db:
        stopped = db.scalar(select(ApplicationTermination))
        assert {item["state"] for item in stopped.external_items} == {"unknown"}
        application = termination.lock_application(db, ctx.org_id, ctx.application_id)
        with pytest.raises(termination.TerminationError, match="external_resolution_pending"):
            termination.confirm_external_resolution(db, application, expected_version=stopped.version,
                reason_text="Still unknown", actor_user_id=ctx.actor_id, trace_id="ack")
