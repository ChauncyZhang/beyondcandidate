"""Termination fences: run with python -m pytest server/tests/test_screening_termination.py -q.

Controlled AI callbacks prove history preservation and no forward side effects;
SQLite does not prove PostgreSQL lock contention (covered by the combined gate).
"""
import asyncio
import uuid
from unittest.mock import patch

import pytest
from sqlalchemy import func, select, update

from server.app.identity.models import AuditLog
from server.app.llm.models import LlmInvocation, LlmScreeningEvaluation
from server.app.queue.models import BackgroundJob
from server.app.queue.service import PermanentJobError
from server.app.recruiting.models import Application, ApplicationReviewTask, ApplicationStageEvent
from server.app.recruiting.termination_models import ApplicationTermination
from server.app.screening.actions import ScreeningItemNotRetryable, retry_screening_item
from server.app.screening.llm_pipeline import LlmScreeningPipeline
from server.app.screening.models import ScreeningItem, ScreeningResult
from server.app.screening.routing import route_llm_screening_terminal
from server.tests.test_llm_pipeline import Gateway, prepared, run_llm_terminal_finalizer, terminal_llm_failure
from server.tests.test_recruiting_api import make_app
from server.tests.test_screening_routing import seed_routing_case


def withdraw(db, application_id):
    application = db.get(Application, application_id)
    db.add(ApplicationTermination(
        organization_id=application.organization_id, application_id=application.id,
        candidate_id=application.candidate_id, job_id=application.job_id,
        actor_user_id=application.owner_id, source_stage=application.stage,
        reason_text="Candidate withdrew",
    ))
    application.stage = "withdrawn"
    application.version += 1
    for task in db.scalars(select(ApplicationReviewTask).where(
        ApplicationReviewTask.application_id == application_id,
        ApplicationReviewTask.status == "open",
    )):
        task.status = "cancelled"
    db.commit()


def route(db, case, *, failed=False):
    return route_llm_screening_terminal(
        db, organization_id=case.organization_id, item_id=case.item_id,
        actor_user_id=case.creator_id, score=None if failed else 91,
        ai_status="failed" if failed else "succeeded",
        safe_error_code="provider_unavailable" if failed else None,
        trace_id="termination-fence", evaluation_id=uuid.uuid4(), invocation_id=uuid.uuid4(),
    )


@pytest.mark.parametrize("cached", [False, True])
def test_late_success_after_fail_open_does_not_route_or_refresh_review(tmp_path, cached):
    app = make_app(tmp_path)
    case = seed_routing_case(app, suffix="termination")
    with app.state.identity_store.sync_session() as db:
        route(db, case, failed=True)
        db.commit()
        application = db.get(Application, case.application_id)
        if cached:
            # Simulate a row changed after an ORM instance was already loaded.
            db.execute(update(Application).where(Application.id == application.id).values(
                stage="withdrawn", version=3,
            ).execution_options(synchronize_session=False))
            db.flush()
            assert application.stage == "review"
        else:
            withdraw(db, case.application_id)
        before_audits = db.scalar(select(func.count(AuditLog.id)))
        with patch("server.app.screening.routing.ensure_review_task") as review, patch(
            "server.app.screening.routing.ensure_deferred_membership"
        ) as talent:
            result = route(db, case)
        assert result.routed is False
        assert result.stage == "withdrawn"
        assert application.stage == "withdrawn" and application.version == 3
        review.assert_not_called()
        talent.assert_not_called()
        assert db.scalar(select(func.count(AuditLog.id))) == before_audits
        assert db.scalar(select(func.count(ApplicationStageEvent.id))) == 1


@pytest.mark.parametrize("failure", [None, "terminal", "transient"])
@pytest.mark.parametrize("fail_open_retry", [False, True])
def test_inflight_ai_after_withdrawal_keeps_result_without_forward_effects(tmp_path, failure, fail_open_retry):
    app, cipher, job = prepared(tmp_path)
    application_id = uuid.UUID(job.payload["application_id"])
    if fail_open_retry:
        item_id, _run_id, _application_id = terminal_llm_failure(app, job)
        with app.state.identity_store.sync_session() as db:
            _, _, retried = retry_screening_item(db, job.organization_id, item_id, "before-stop")
            db.commit()
            job.id, job.payload = retried.id, dict(retried.payload)
    with app.state.identity_store.sync_session() as db:
        before_jobs = db.scalar(select(func.count(BackgroundJob.id)))

    def terminate_during_provider_call():
        with app.state.identity_store.sync_session() as db:
            withdraw(db, application_id)

    gateway = Gateway(inspect=terminate_during_provider_call)
    if failure:
        from server.app.llm.gateway import GatewayError
        gateway.outcome = GatewayError("provider_unavailable")
        if failure == "terminal":
            job.attempts = job.max_attempts
    pipeline = LlmScreeningPipeline(app.state.identity_store.sync_session, gateway, cipher)
    if failure:
        with pytest.raises(PermanentJobError):
            asyncio.run(pipeline.evaluate_item(job))
    else:
        asyncio.run(pipeline.evaluate_item(job))
    with app.state.identity_store.sync_session() as db:
        assert db.get(Application, application_id).stage == "withdrawn"
        assert db.scalar(select(func.count(ScreeningResult.id))) == 1
        assert db.scalar(select(func.count(LlmInvocation.id))) == 1
        assert db.scalar(select(func.count(BackgroundJob.id))) == before_jobs
        assert db.scalar(select(func.count(LlmScreeningEvaluation.id))) == (0 if failure else 1)
        tasks = list(db.scalars(select(ApplicationReviewTask)))
        assert len(tasks) == int(fail_open_retry)
        assert all(task.status == "cancelled" and task.ai_status == "failed" for task in tasks)
        assert db.scalar(select(func.count(ApplicationStageEvent.id))) == int(fail_open_retry)
        assert db.scalar(select(func.count(AuditLog.id)).where(
            AuditLog.event_type == "screening.terminal_routed"
        )) == int(fail_open_retry)


def test_late_rule_score_preserves_history_without_enqueueing_ai(tmp_path):
    app, _cipher, job = prepared(tmp_path)
    item_id = uuid.UUID(job.payload["screening_item_id"])
    with app.state.identity_store.sync_session() as db:
        item = db.get(ScreeningItem, item_id)
        item.status = "parsed"
        item.llm_status = "not_requested"
        for queued in db.scalars(select(BackgroundJob)):
            queued.status = "succeeded"
        db.commit()
        before = db.scalar(select(func.count(BackgroundJob.id)))
    from server.app.screening.pipeline import ScreeningPipeline
    from server.app.screening.rules import score_resume

    def terminate_during_score(*args, **kwargs):
        with app.state.identity_store.sync_session() as db:
            withdraw(db, uuid.UUID(job.payload["application_id"]))
        return score_resume(*args, **kwargs)

    pipeline = ScreeningPipeline(app.state.identity_store.sync_session, None, None, app.state.settings)
    with patch("server.app.screening.pipeline.score_resume", side_effect=terminate_during_score):
        asyncio.run(pipeline.score_item(job))
    with app.state.identity_store.sync_session() as db:
        item = db.get(ScreeningItem, item_id)
        assert item.status == "scored" and item.llm_status == "skipped"
        assert item.llm_safe_error_code == "application_terminated"
        assert db.scalar(select(func.count(ScreeningResult.id))) == 1
        assert db.scalar(select(func.count(BackgroundJob.id))) == before


def test_withdrawn_application_rejects_retry_without_new_jobs(tmp_path):
    app, _cipher, job = prepared(tmp_path)
    item_id, _run_id, application_id = terminal_llm_failure(app, job)
    with app.state.identity_store.sync_session() as db:
        withdraw(db, application_id)
        before = db.scalar(select(func.count(BackgroundJob.id)))
        with pytest.raises(ScreeningItemNotRetryable):
            retry_screening_item(db, job.organization_id, item_id, "termination-retry")
        assert db.scalar(select(func.count(BackgroundJob.id))) == before


def test_queued_ai_for_withdrawn_application_never_calls_provider(tmp_path):
    app, cipher, job = prepared(tmp_path)
    with app.state.identity_store.sync_session() as db:
        withdraw(db, uuid.UUID(job.payload["application_id"]))
    gateway = Gateway()
    asyncio.run(LlmScreeningPipeline(app.state.identity_store.sync_session, gateway, cipher).evaluate_item(job))
    assert gateway.calls == []
    with app.state.identity_store.sync_session() as db:
        item = db.get(ScreeningItem, uuid.UUID(job.payload["screening_item_id"]))
        assert item.llm_status == "skipped"
        assert item.finished_at is not None
        assert db.scalar(select(func.count(ScreeningResult.id))) == 1


def test_late_dead_letter_finalizer_cannot_fail_open_after_termination(tmp_path):
    app, _cipher, job = prepared(tmp_path)
    with app.state.identity_store.sync_session() as db:
        withdraw(db, uuid.UUID(job.payload["application_id"]))
    run_llm_terminal_finalizer(app, job, "provider_unavailable", repeat_callback=True)
    with app.state.identity_store.sync_session() as db:
        item = db.get(ScreeningItem, uuid.UUID(job.payload["screening_item_id"]))
        assert item.llm_status == "failed" and item.finished_at is not None
        assert db.get(Application, item.application_id).stage == "withdrawn"
        assert db.scalar(select(func.count(ScreeningResult.id))) == 1
        assert db.scalar(select(func.count(ApplicationReviewTask.id))) == 0
        assert db.scalar(select(func.count(ApplicationStageEvent.id))) == 0


def test_persistent_termination_record_fences_fail_open_even_if_stage_is_active(tmp_path):
    app = make_app(tmp_path)
    case = seed_routing_case(app, suffix="termination-record")
    with app.state.identity_store.sync_session() as db:
        route(db, case, failed=True)
        withdraw(db, case.application_id)
        # The durable boundary must remain authoritative even for inconsistent
        # legacy/imported stage state; the router must never repair it forward.
        application = db.get(Application, case.application_id)
        application.stage = "review"
        db.flush()
        with patch("server.app.screening.routing.ensure_review_task") as review:
            assert route(db, case).routed is False
        review.assert_not_called()
