"""Termination fences and controlled remote-create interleavings (no live Feishu)."""

import asyncio
from uuid import uuid4

import pytest
from sqlalchemy import select

from server.app.integrations.feishu import worker as workers
from server.app.integrations.feishu.provider import ApprovalInstance, FeishuProviderError
from server.app.onboarding import service
from server.app.onboarding.models import OnboardingRecord
from server.app.onboarding.schemas import OnboardingUpdateCommand
from server.app.queue.models import OutboxEvent
from server.app.recruiting.models import Application, Candidate
from server.app.recruiting import termination
from server.app.recruiting.termination_models import ApplicationTermination
from server.tests.test_onboarding_approvers import DEFINITION, NOW, context, start, worker


@pytest.fixture
def boundary(context, monkeypatch):
    state = {"terminated": False, "facts": []}

    def terminated(db, organization_id, application_id):
        return state["terminated"]

    def fact(db, organization_id, application_id, **value):
        if state["terminated"]:
            state["facts"].append(value)

    monkeypatch.setattr(service, "application_is_terminated", terminated, raising=False)
    monkeypatch.setattr(workers, "application_is_terminated", terminated, raising=False)
    monkeypatch.setattr(workers, "record_external_fact", fact, raising=False)
    return context, state


def test_terminated_onboarding_projection_and_forward_writes_are_blocked(boundary):
    ctx, state = boundary
    state["terminated"] = True
    with ctx.sessions.begin() as db:
        record = db.get(OnboardingRecord, ctx.record_id)
        before = (record.version, record.pii_ciphertext, record.generation)
        view = service.onboarding_projection(record, cipher=ctx.pii, now=NOW)
        assert view["allowed_actions"] == {"update": False, "start_onboarding": False}
        assert view["can_submit"] is False
        assert view["blocking_reason"] == "application_terminated"
        with pytest.raises(service.OnboardingNotReady, match="application_terminated"):
            service.update_onboarding(db, record, OnboardingUpdateCommand(expected_start_date=NOW.date()),
                cipher=ctx.pii, expected_version=record.version, actor_user_id=ctx.actor_id, trace_id="termination")
        with pytest.raises(service.OnboardingNotReady, match="application_terminated"):
            service.start_onboarding_submission(db, record, expected_version=record.version,
                generation=uuid4(), actor_user_id=ctx.actor_id, cipher=ctx.pii, now=NOW, trace_id="termination")
        assert (record.version, record.pii_ciphertext, record.generation) == before
        assert db.scalar(select(OutboxEvent)) is None


@pytest.mark.parametrize("checkpoint", ["queued", "definition"])
def test_termination_before_create_stops_worker_without_external_action(boundary, checkpoint):
    ctx, state = boundary
    queued = start(ctx)
    if checkpoint == "queued":
        state["terminated"] = True
    else:
        def definition(*args):
            state["terminated"] = True
            return DEFINITION
        ctx.provider.get_approval_definition = definition
    worker(ctx, queued)
    assert ctx.provider.approval_instances == []
    assert state["facts"][-1]["state"] == "resolved"
    with ctx.sessions() as db:
        record = db.get(OnboardingRecord, ctx.record_id)
        assert record.status == "submitting"
        assert str(record.generation) == queued.payload["generation"]


def test_termination_after_fingerprint_but_before_attempt_is_resolved(boundary, monkeypatch):
    ctx, state = boundary
    queued = start(ctx)
    handler = workers.FeishuOnboardingOutboxHandler(ctx.sessions, ctx.provider, ctx.secret, ctx.pii)
    prepare = handler._prepare_request

    def prepared_then_terminate(*args):
        result = prepare(*args)
        state["terminated"] = True
        return result

    monkeypatch.setattr(handler, "_prepare_request", prepared_then_terminate)
    asyncio.run(handler(queued, queued.id))
    assert ctx.provider.approval_instances == []
    assert state["facts"][-1]["state"] == "resolved"
    with ctx.sessions() as db:
        event = db.get(OutboxEvent, queued.id)
        assert event.payload["submission_state"] == "prepared"
        assert len(event.payload["submission_fingerprint"]) == 64


def test_explicit_unknown_is_never_downgraded_to_unattempted(boundary):
    ctx, state = boundary
    queued = start(ctx)
    with ctx.sessions.begin() as db:
        event = db.get(OutboxEvent, queued.id)
        event.payload = {**event.payload, "submission_state": "unknown"}
    state["terminated"] = True
    worker(ctx, queued)
    assert state["facts"][-1]["state"] == "unknown"


def test_stale_generation_never_borrows_another_generation_instance(boundary):
    ctx, state = boundary
    queued = start(ctx)
    with ctx.sessions.begin() as db:
        record = db.get(OnboardingRecord, ctx.record_id)
        record.generation = uuid4()
        record.feishu_instance_code = "other_generation_instance"
        event = db.get(OutboxEvent, queued.id)
        event.payload = {**event.payload, "submission_state": "unknown"}
    state["terminated"] = True
    worker(ctx, queued)
    assert state["facts"][-1]["state"] == "unknown"
    assert state["facts"][-1]["reference"] is None
    assert str(state["facts"][-1]["generation"]) == queued.payload["generation"]


def test_termination_during_create_preserves_late_instance_and_blocks_second_approval(boundary):
    ctx, state = boundary
    queued = start(ctx)
    create = ctx.provider.create_approval_instance

    def create_then_terminate(*args, **kwargs):
        with ctx.sessions() as db:
            assert db.get(OutboxEvent, queued.id).payload["submission_state"] == "attempted"
        result = create(*args, **kwargs)
        state["terminated"] = True
        return result

    ctx.provider.create_approval_instance = create_then_terminate
    worker(ctx, queued)
    worker(ctx, queued)
    assert len(ctx.provider.approval_instances) == 1
    with ctx.sessions() as db:
        record = db.get(OnboardingRecord, ctx.record_id)
        assert record.status == "submitting" and record.submitted_at is None
        assert record.feishu_instance_code
        assert record.started_by == ctx.actor_id
        assert str(record.generation) == queued.payload["generation"]
        event = db.get(OutboxEvent, queued.id)
        assert event.payload["instance_code"] == record.feishu_instance_code
        assert len(event.payload["submission_fingerprint"]) == 64
        assert state["facts"][-1] == dict(kind="feishu_onboarding", resource_id=record.id,
            state="needs_action", reference=record.feishu_instance_code, generation=record.generation)


@pytest.mark.parametrize("retryable", [True, False])
def test_attempted_error_after_termination_stays_unknown_and_is_not_retried(boundary, retryable):
    ctx, state = boundary
    queued = start(ctx)
    calls = []

    def uncertain(*args, **kwargs):
        calls.append(True)
        state["terminated"] = True
        raise FeishuProviderError("feishu_unavailable", retryable=retryable)

    ctx.provider.create_approval_instance = uncertain
    worker(ctx, queued)
    worker(ctx, queued)
    assert len(calls) == 1
    assert state["facts"][-1]["state"] == "unknown"
    with ctx.sessions() as db:
        assert db.get(OutboxEvent, queued.id).payload["submission_state"] == "unknown"
        assert db.get(OnboardingRecord, ctx.record_id).status == "submitting"


@pytest.fixture
def real_context(context):
    ctx = context
    with ctx.sessions.begin() as db:
        record = db.get(OnboardingRecord, ctx.record_id)
        db.add(Candidate(id=record.candidate_id, organization_id=ctx.org_id, display_name="Candidate"))
        db.add(Application(id=record.application_id, organization_id=ctx.org_id,
            candidate_id=record.candidate_id, job_id=record.job_id, resume_id=uuid4(),
            owner_id=ctx.actor_id, stage="hired"))
        ctx.application_id = record.application_id
    return ctx


def terminate(ctx):
    with ctx.sessions.begin() as db:
        application = termination.lock_application(db, ctx.org_id, ctx.application_id)
        resources = termination.dependent_resources(db, application)
        return termination.terminate_application(db, application, expected_version=application.version,
            expected_preview=termination.preview_version(application, resources), reason_text="Candidate withdrew",
            confirm_not_onboarded=True, actor_user_id=ctx.actor_id, trace_id="termination-test").id


def test_real_termination_during_create_blocks_ack_then_retains_success(real_context):
    ctx = real_context
    queued = start(ctx)

    def inflight(*args, **kwargs):
        termination_id = terminate(ctx)
        with ctx.sessions.begin() as db:
            record = db.get(ApplicationTermination, termination_id)
            assert record.external_status == "pending"
            assert any(item["state"] in {"in_flight", "unknown"} for item in record.external_items)
            application = termination.lock_application(db, ctx.org_id, ctx.application_id)
            with pytest.raises(termination.TerminationError, match="external_resolution_pending"):
                termination.confirm_external_resolution(db, application, expected_version=record.version,
                    reason_text="Must not pass", actor_user_id=ctx.actor_id, trace_id="ack")
        return ApprovalInstance("late_original_instance")

    ctx.provider.create_approval_instance = inflight
    worker(ctx, queued)
    with ctx.sessions() as db:
        onboarding = db.get(OnboardingRecord, ctx.record_id)
        stopped = db.scalar(select(ApplicationTermination))
        assert onboarding.feishu_instance_code == "late_original_instance"
        assert onboarding.status == "submitting" and onboarding.submitted_at is None
        assert db.get(Application, ctx.application_id).stage == "withdrawn"
        assert stopped.external_status == "pending"
        assert {item["state"] for item in stopped.external_items} == {"needs_action"}
        assert stopped.external_items[0]["reference"] == "late_original_instance"


def test_real_unknown_error_blocks_ack_even_if_not_retryable(real_context):
    ctx = real_context
    queued = start(ctx)

    def uncertain(*args, **kwargs):
        terminate(ctx)
        raise FeishuProviderError("feishu_bad_response", retryable=False)

    ctx.provider.create_approval_instance = uncertain
    worker(ctx, queued)
    with ctx.sessions.begin() as db:
        stopped = db.scalar(select(ApplicationTermination))
        assert {item["state"] for item in stopped.external_items} == {"unknown"}
        application = termination.lock_application(db, ctx.org_id, ctx.application_id)
        with pytest.raises(termination.TerminationError, match="external_resolution_pending"):
            termination.confirm_external_resolution(db, application, expected_version=stopped.version,
                reason_text="Must not pass", actor_user_id=ctx.actor_id, trace_id="ack")


def test_new_late_fact_reopens_cleanup_and_preserves_ack_history(real_context):
    ctx = real_context
    queued = start(ctx)
    worker(ctx, queued)
    terminate(ctx)
    handler = workers.FeishuOnboardingOutboxHandler(ctx.sessions, ctx.provider, ctx.secret, ctx.pii)
    with ctx.sessions.begin() as db:
        application = termination.lock_application(db, ctx.org_id, ctx.application_id)
        stopped = db.scalar(select(ApplicationTermination))
        termination.confirm_external_resolution(db, application, expected_version=stopped.version,
            reason_text="Handled in Feishu", actor_user_id=ctx.actor_id, trace_id="ack")
        history = stopped.resolution_history
        acknowledged_version = stopped.version
    with ctx.sessions.begin() as db:
        record = service.lock_onboarding_record(db, ctx.org_id, ctx.record_id)
        handler._termination_fact(db, record, record.generation, state="needs_action", reference="new_late_instance")
    with ctx.sessions() as db:
        stopped = db.scalar(select(ApplicationTermination))
        assert stopped.external_status == "pending" and stopped.version > acknowledged_version
        assert stopped.resolution_history == history
        assert any(item["reference"] == "new_late_instance" and item["state"] == "needs_action"
            for item in stopped.external_items)
        assert db.get(Application, ctx.application_id).stage == "withdrawn"
        assert db.get(OnboardingRecord, ctx.record_id).status == "submitted"


def test_real_terminated_create_and_missing_record_projection_are_blocked(real_context):
    ctx = real_context
    terminate(ctx)
    with ctx.sessions.begin() as db:
        application = termination.lock_application(db, ctx.org_id, ctx.application_id)
        view = service.onboarding_projection(None, application=application, cipher=ctx.pii, now=NOW)
        assert view["allowed_actions"] == {"update": False, "start_onboarding": False}
        assert view["blocking_reason"] == "application_terminated"
        with pytest.raises(service.OnboardingNotReady, match="application_terminated"):
            service.create_onboarding_from_accepted_offer(db, application,
                OnboardingUpdateCommand(expected_start_date=NOW.date()), cipher=ctx.pii,
                expected_version=0, actor_user_id=ctx.actor_id, trace_id="create")
