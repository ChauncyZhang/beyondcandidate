from datetime import datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from server.app.identity.models import Job
from server.app.recruiting.models import Application, ApplicationReviewTask
from server.app.recruiting.service import apply_application_workflow_action_record
from server.app.recruiting.tasks import close_review_task, ensure_review_task
from server.tests.test_recruiting_api import make_app
from server.tests.test_screening_routing import seed_routing_case


def test_review_task_creation_is_idempotent_and_uses_hiring_owner(tmp_path):
    app = make_app(tmp_path)
    case = seed_routing_case(app, suffix="task")
    with app.state.identity_store.sync_session() as db:
        application = db.get(Application, case.application_id)
        job = db.get(Job, case.job_id)
        first = ensure_review_task(
            db,
            application=application,
            job=job,
            ai_status="failed",
            safe_error_code="provider_unavailable",
        )
        db.flush()
        second = ensure_review_task(
            db,
            application=application,
            job=job,
            ai_status="succeeded",
        )
        assert first.id == second.id
        assert first.assignee_id == case.manager_id
        assert first.ai_status == "succeeded"
        assert first.safe_error_code is None
        assert db.scalar(select(func.count(ApplicationReviewTask.id))) == 1


def test_review_task_refreshes_failed_ai_state_after_successful_retry(tmp_path):
    app = make_app(tmp_path)
    case = seed_routing_case(app, suffix="task-refresh")
    with app.state.identity_store.sync_session() as db:
        application = db.get(Application, case.application_id)
        job = db.get(Job, case.job_id)
        failed = ensure_review_task(
            db,
            application=application,
            job=job,
            ai_status="failed",
            safe_error_code="provider_unavailable",
        )
        db.flush()

        succeeded = ensure_review_task(
            db,
            application=application,
            job=job,
            ai_status="succeeded",
        )
        stale_failure = ensure_review_task(
            db,
            application=application,
            job=job,
            ai_status="failed",
            safe_error_code="provider_unavailable",
        )

        assert succeeded.id == failed.id
        assert stale_failure.id == succeeded.id
        assert stale_failure.ai_status == "succeeded"
        assert stale_failure.safe_error_code is None
        assert db.scalar(select(func.count(ApplicationReviewTask.id))) == 1


def test_review_task_normalizes_unsafe_failure_code(tmp_path):
    app = make_app(tmp_path)
    case = seed_routing_case(app, suffix="task-safe-code")
    with app.state.identity_store.sync_session() as db:
        task = ensure_review_task(
            db,
            application=db.get(Application, case.application_id),
            job=db.get(Job, case.job_id),
            ai_status="failed",
            safe_error_code="candidate_alice_resume_prompt",
        )
        assert task.safe_error_code == "internal_error"


def test_review_task_rejects_cross_tenant_job_context(tmp_path):
    app = make_app(tmp_path)
    case = seed_routing_case(app, suffix="task-tenant")
    with app.state.identity_store.sync_session() as db:
        application = db.get(Application, case.application_id)
        wrong_job = SimpleNamespace(
            id=uuid4(),
            organization_id=uuid4(),
            hiring_owner_id=case.manager_id,
            owner_id=case.creator_id,
        )
        with pytest.raises(ValueError, match="review_task_context_mismatch"):
            ensure_review_task(
                db,
                application=application,
                job=wrong_job,
                ai_status="succeeded",
            )


def test_review_task_falls_back_to_job_owner_and_closes_only_in_tenant(tmp_path):
    app = make_app(tmp_path)
    case = seed_routing_case(app, suffix="fallback")
    with app.state.identity_store.sync_session() as db:
        application = db.get(Application, case.application_id)
        job = db.get(Job, case.job_id)
        job.hiring_owner_id = None
        task = ensure_review_task(
            db,
            application=application,
            job=job,
            ai_status="succeeded",
        )
        db.flush()
        assert task.assignee_id == case.creator_id
        assert close_review_task(
            db,
            organization_id=case.organization_id,
            application_id=case.application_id,
        ) is task
        assert task.status == "closed"
        assert isinstance(task.closed_at, datetime)
        assert task.assignee_id == case.creator_id
        assert task.ai_status == "succeeded"
        closed_at = task.closed_at
        assert close_review_task(
            db,
            organization_id=case.organization_id,
            application_id=case.application_id,
        ) is None
        assert task.closed_at == closed_at


@pytest.mark.parametrize(
    ("action", "reason_text"),
    [("review_approved", None), ("review_rejected", "Not a current match")],
)
def test_manager_review_action_closes_open_task_in_same_transaction(
    tmp_path, action, reason_text
):
    app = make_app(tmp_path)
    case = seed_routing_case(app, suffix=action)
    with app.state.identity_store.sync_session() as db:
        application = db.get(Application, case.application_id)
        application.stage = "review"
        job = db.get(Job, case.job_id)
        task = ensure_review_task(
            db,
            application=application,
            job=job,
            ai_status="succeeded",
        )
        db.flush()
        apply_application_workflow_action_record(
            db,
            case.organization_id,
            case.application_id,
            action,
            expected_version=1,
            actor_user_id=case.manager_id,
            trace_id="trace-manager",
            reason_text=reason_text,
        )
        assert task.status == "closed"
        assert task.closed_at is not None
