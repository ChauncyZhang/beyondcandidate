"""Bounded regressions for identity freshness and Candidate-first mutations."""

import asyncio
import json
from uuid import uuid4

import pytest
from sqlalchemy import event, select
from sqlalchemy.orm import Session

from server.app.governance.deletion_models import DeletionRequest
from server.app.identity.models import Department, Job, User, UserStatus
from server.app.integrations.feishu.models import FeishuDepartmentMapping, FeishuIdentityBinding, FeishuOnboardingConfig
from server.app.integrations.feishu.provider import FeishuProviderError
from server.app.integrations.feishu.worker import FeishuOnboardingOutboxHandler
from server.app.onboarding.models import OnboardingRecord
from server.app.onboarding.schemas import OnboardingUpdateCommand
from server.app.onboarding.service import OnboardingNotReady, update_onboarding
from server.app.offers.models import Offer, OfferResponse, OfferVersion
from server.app.queue.models import OutboxEvent
from server.app.queue.service import PermanentJobError, RetryableJobError
from server.app.recruiting.models import Application, Candidate
from server.tests.test_feishu_api import configure_feishu, feishu_app, write_headers
from server.tests.test_onboarding_approvers import DEFINITION, NOW, context, start, worker
from server.tests.test_onboarding import SEMANTICS
from server.tests.test_workbench_api import seed_application


@pytest.fixture
def distinct_context(context):
    ctx = context
    initiator_id = uuid4()
    with ctx.sessions.begin() as db:
        db.add(User(id=initiator_id, organization_id=ctx.org_id, email="initiator@example.test",
                    normalized_email="initiator@example.test", display_name="Initiator",
                    password_hash="unused", status=UserStatus.ACTIVE))
        db.add(FeishuIdentityBinding(organization_id=ctx.org_id, user_id=initiator_id, open_id="ou_initiator"))
        record = db.get(OnboardingRecord, ctx.record_id)
        db.add(Candidate(id=record.candidate_id, organization_id=ctx.org_id, display_name="Candidate"))
    # The original actor remains the fixed approver, not the clicking initiator.
    ctx.actor_id = initiator_id
    return ctx


def deletion(ctx, db):
    record = db.get(OnboardingRecord, ctx.record_id)
    db.add(DeletionRequest(organization_id=ctx.org_id, candidate_id=record.candidate_id,
                          status="requested", requested_by=ctx.actor_id, reason_code="candidate_request",
                          impact_manifest={}, manifest_hash="f" * 64, policy_version=1, candidate_version=1))


def change_binding(ctx, value):
    with ctx.sessions.begin() as db:
        binding = db.scalar(select(FeishuIdentityBinding).where(FeishuIdentityBinding.user_id == ctx.actor_id))
        if value is None:
            db.delete(binding)
        else:
            binding.open_id = value


@pytest.mark.parametrize("binding", ["ou_current", None])
def test_distinct_initiator_is_reresolved_after_definition_read(distinct_context, binding):
    ctx = distinct_context
    queued = start(ctx)

    def definition(credentials, approval_code):
        change_binding(ctx, binding)
        return DEFINITION

    ctx.provider.get_approval_definition = definition
    if binding is None:
        with pytest.raises(PermanentJobError, match="feishu_onboarding_initiator_unbound"):
            worker(ctx, queued)
        assert ctx.provider.approval_instances == []
        with ctx.sessions() as db:
            assert "submission_fingerprint" not in db.get(OutboxEvent, queued.id).payload
    else:
        worker(ctx, queued)
        request = ctx.provider.approval_instances[0][0]
        assert request.initiator_open_id == binding
        assert request.node_approvers == ({"key": "approval_hr", "value": ["ou_fixed"]},)
        with ctx.sessions() as db:
            stored = db.get(OutboxEvent, queued.id).payload
            assert stored["submission_state"] == "attempted"
            assert len(stored["submission_fingerprint"]) == 64
            assert "ou_current" not in json.dumps(stored)


@pytest.mark.parametrize("binding", ["ou_changed", None])
@pytest.mark.parametrize("during_read", [False, True])
def test_attempted_retry_with_changed_distinct_initiator_requires_reconciliation(distinct_context, binding, during_read):
    ctx = distinct_context
    queued = start(ctx)
    calls = []

    def ambiguous(credentials, request, *, idempotency_key):
        calls.append(request)
        raise FeishuProviderError("feishu_timeout", retryable=True)

    ctx.provider.create_approval_instance = ambiguous
    with pytest.raises(RetryableJobError):
        worker(ctx, queued)
    with ctx.sessions() as db:
        digest = db.get(OutboxEvent, queued.id).payload["submission_fingerprint"]
    if during_read:
        def definition(credentials, approval_code):
            change_binding(ctx, binding)
            return DEFINITION
        ctx.provider.get_approval_definition = definition
    else:
        change_binding(ctx, binding)
    with pytest.raises(PermanentJobError, match="feishu_approval_reconciliation_required"):
        worker(ctx, queued)
    assert len(calls) == 1
    assert calls[0].initiator_open_id == "ou_initiator"
    with ctx.sessions() as db:
        assert db.get(OutboxEvent, queued.id).payload["submission_fingerprint"] == digest
        assert db.get(OnboardingRecord, ctx.record_id).safe_error_code == "feishu_approval_reconciliation_required"


@pytest.mark.parametrize("checkpoint", ["initial", "after_definition", "after_fingerprint"])
def test_deletion_blocks_worker_without_mutating_record(distinct_context, checkpoint, monkeypatch):
    ctx = distinct_context
    queued = start(ctx)
    with ctx.sessions() as db:
        record = db.get(OnboardingRecord, ctx.record_id)
        before = (record.status, record.version, record.safe_error_code, record.pii_ciphertext)
    handler = FeishuOnboardingOutboxHandler(ctx.sessions, ctx.provider, ctx.secret, ctx.pii)
    if checkpoint == "initial":
        with ctx.sessions.begin() as db:
            deletion(ctx, db)
    elif checkpoint == "after_definition":
        def definition(credentials, approval_code):
            with ctx.sessions.begin() as db:
                deletion(ctx, db)
            return DEFINITION
        ctx.provider.get_approval_definition = definition
    else:
        prepare = handler._prepare_request
        def prepare_then_delete(*args):
            request = prepare(*args)
            with ctx.sessions.begin() as db:
                deletion(ctx, db)
            return request
        monkeypatch.setattr(handler, "_prepare_request", prepare_then_delete)
    with pytest.raises(PermanentJobError, match="candidate_deletion_pending"):
        asyncio.run(handler(queued, queued.id))
    assert ctx.provider.approval_instances == []
    with ctx.sessions() as db:
        record = db.get(OnboardingRecord, ctx.record_id)
        assert (record.status, record.version, record.safe_error_code, record.pii_ciphertext) == before
        payload = db.get(OutboxEvent, queued.id).payload
        assert ("submission_fingerprint" in payload) == (checkpoint == "after_fingerprint")
        if checkpoint == "initial":
            assert "submission_state" not in payload


@pytest.fixture
def lock_trace():
    locks = []
    def record_lock(state):
        statement = state.statement
        if not state.is_select or getattr(statement, "_for_update_arg", None) is None:
            return
        model = statement.column_descriptions[0].get("entity")
        if model in {Candidate, Application, OnboardingRecord, Offer}:
            locks.append((state.session.get_transaction(), model))
    event.listen(Session, "do_orm_execute", record_lock)
    yield locks
    event.remove(Session, "do_orm_execute", record_lock)


def assert_candidate_first(locks):
    assert any(model is OnboardingRecord for _, model in locks)
    seen = set()
    for transaction, model in locks:
        if model is Candidate:
            seen.add(transaction)
        else:
            assert transaction in seen, f"{model.__name__} was locked before Candidate"


def test_service_update_start_and_worker_all_lock_candidate_first(distinct_context, lock_trace):
    ctx = distinct_context
    with ctx.sessions.begin() as db:
        record = db.get(OnboardingRecord, ctx.record_id)
        update_onboarding(db, record, OnboardingUpdateCommand(onboarding_data={"gender": "male"}),
                          cipher=ctx.pii, expected_version=record.version, actor_user_id=ctx.actor_id, trace_id="qa")
    queued = start(ctx)
    # The remote read must run after the preflight transaction released its locks.
    def definition(credentials, approval_code):
        assert all(not transaction.is_active for transaction, _ in lock_trace)
        return DEFINITION
    ctx.provider.get_approval_definition = definition
    def create(credentials, request, *, idempotency_key):
        assert any(transaction.is_active and model is Candidate for transaction, model in lock_trace)
        from server.app.integrations.feishu.provider import ApprovalInstance
        return ApprovalInstance("qa_instance")
    ctx.provider.create_approval_instance = create
    worker(ctx, queued)
    assert_candidate_first(lock_trace)


def test_pending_deletion_service_update_preserves_pii_and_version(distinct_context, lock_trace):
    ctx = distinct_context
    with ctx.sessions.begin() as db:
        deletion(ctx, db)
    with ctx.sessions.begin() as db:
        record = db.get(OnboardingRecord, ctx.record_id)
        before = (record.pii_ciphertext, record.version, record.status)
        with pytest.raises(OnboardingNotReady, match="candidate_deletion_pending"):
            update_onboarding(db, record, OnboardingUpdateCommand(onboarding_data={"gender": "male"}),
                              cipher=ctx.pii, expected_version=record.version, actor_user_id=ctx.actor_id, trace_id="qa")
        assert (record.pii_ciphertext, record.version, record.status) == before
    assert_candidate_first(lock_trace)


def test_pending_deletion_after_send_freezes_success_status(distinct_context, monkeypatch):
    ctx = distinct_context
    queued = start(ctx)
    handler = FeishuOnboardingOutboxHandler(ctx.sessions, ctx.provider, ctx.secret, ctx.pii)
    create = handler._create_instance
    def create_then_delete(*args):
        result = create(*args)
        with ctx.sessions.begin() as db:
            deletion(ctx, db)
        return result
    monkeypatch.setattr(handler, "_create_instance", create_then_delete)
    asyncio.run(handler(queued, queued.id))
    assert len(ctx.provider.approval_instances) == 1
    with ctx.sessions() as db:
        record = db.get(OnboardingRecord, ctx.record_id)
        assert record.status == "submitting" and record.version == 2
        assert record.feishu_instance_code is None


def test_api_create_update_and_submit_candidate_first(feishu_app, lock_trace):
    app, client = feishu_app
    org_id, csrf = configure_feishu(feishu_app)
    with app.state.identity_store.sync_session() as db:
        actor = db.scalar(select(User).where(User.organization_id == org_id))
        department = Department(organization_id=org_id, name="QA")
        db.add(department)
        db.flush()
        job = Job(organization_id=org_id, title="QA Role", owner_id=actor.id, department_id=department.id, status="open")
        db.add(job)
        db.flush()
        application = seed_application(db, job, actor, 1, "hired", NOW)
        version_id = uuid4()
        offer = Offer(organization_id=org_id, application_id=application.id, job_id=job.id,
                      current_version_id=version_id, status="accepted", candidate_response_deadline=NOW)
        db.add(offer)
        db.flush()
        db.add(OfferVersion(id=version_id, organization_id=org_id, offer_id=offer.id, version_number=1,
                            content={}, is_special=False, candidate_response_deadline=NOW, created_by=actor.id))
        db.flush()
        db.add(OfferResponse(organization_id=org_id, offer_id=offer.id, status="accepted", responded_at=NOW,
                             source="candidate", offer_version_id=version_id, version_number=1, request_hash="f" * 64,
                             expected_start_date=app.state.identity_service.clock.current_time().date()))
        db.add(FeishuOnboardingConfig(organization_id=org_id, approval_code=DEFINITION.approval_code,
                                     field_mapping=SEMANTICS, approval_nodes=[], node_approvers=[], enabled=True,
                                     validation_status="valid", definition_fingerprint=DEFINITION.fingerprint,
                                     created_by=actor.id, updated_by=actor.id))
        db.add(FeishuDepartmentMapping(organization_id=org_id, department_id=department.id,
                                      feishu_department_id="od_qa"))
        db.commit()
        application_id, candidate_id, actor_id = application.id, application.candidate_id, actor.id
    headers = {**write_headers(csrf), "If-Match": '"0"'}
    created = client.put(f"/api/v1/onboardings/{application_id}", headers=headers,
                         json={"onboarding_data": {"gender": "female", "phone": "13800138000",
                                                   "email": "qa@example.com", "home_address": "QA Address"}})
    assert created.status_code == 200, created.text
    record_id = created.json()["data"]["id"]
    updated = client.put(f"/api/v1/onboardings/{record_id}", headers={**headers, "If-Match": created.headers["ETag"]},
                         json={"onboarding_data": {"gender": "male"}})
    assert updated.status_code == 200, updated.text
    submitted = client.post(f"/api/v1/onboardings/{record_id}/submissions",
                            headers={**headers, "If-Match": updated.headers["ETag"], "Idempotency-Key": str(uuid4())})
    assert submitted.status_code == 202, submitted.text
    with app.state.identity_store.sync_session() as db:
        db.add(DeletionRequest(organization_id=org_id, candidate_id=candidate_id, status="requested",
                              requested_by=actor_id, reason_code="candidate_request", impact_manifest={},
                              manifest_hash="f" * 64, policy_version=1, candidate_version=1))
        db.commit()
    headers = {**headers, "If-Match": submitted.headers["ETag"], "Idempotency-Key": str(uuid4())}
    blocked = client.post(f"/api/v1/onboardings/{record_id}/submissions", headers=headers)
    assert blocked.status_code == 409 and "candidate_deletion_pending" in blocked.text
    blocked_update = client.put(f"/api/v1/onboardings/{record_id}", headers=headers,
                               json={"onboarding_data": {"gender": "female"}})
    assert blocked_update.status_code == 409 and "candidate_deletion_pending" in blocked_update.text
    assert_candidate_first(lock_trace)
