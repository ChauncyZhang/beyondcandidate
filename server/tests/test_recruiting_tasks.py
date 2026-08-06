from datetime import datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import delete, func, select

from server.app.identity.models import Job, JobCollaborator, User
from server.app.integrations.feishu.models import FeishuOrganizationConfig
from server.app.queue.models import OutboxEvent
from server.app.recruiting.models import Application, ApplicationReviewTask
from server.app.recruiting.service import apply_application_workflow_action_record
from server.app.recruiting.tasks import close_review_task, ensure_review_task
from server.tests.test_recruiting_api import make_app
from server.tests.test_screening_routing import seed_routing_case


def enable_feishu(app, organization_id, actor_id):
    with app.state.identity_store.sync_session() as db:
        db.add(
            FeishuOrganizationConfig(
                organization_id=organization_id,
                app_id="cli_test",
                encrypted_app_secret=app.state.feishu_secret_cipher.encrypt("app-secret"),
                redirect_uri="https://hr.example.test/api/v1/auth/feishu/callback",
                calendar_id="primary",
                enabled=True,
                created_by=actor_id,
                updated_by=actor_id,
            )
        )
        db.commit()


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


def test_review_task_and_approval_queue_the_next_responsible_person(tmp_path):
    app = make_app(tmp_path)
    case = seed_routing_case(app, suffix="task-notification")
    enable_feishu(app, case.organization_id, case.creator_id)

    with app.state.identity_store.sync_session() as db:
        application = db.get(Application, case.application_id)
        application.stage = "review"
        ensure_review_task(
            db,
            application=application,
            job=db.get(Job, case.job_id),
            ai_status="succeeded",
        )
        db.flush()
        apply_application_workflow_action_record(
            db,
            case.organization_id,
            case.application_id,
            "review_approved",
            expected_version=1,
            actor_user_id=case.manager_id,
            trace_id="trace-feishu-notification",
        )
        db.flush()

        events = list(
            db.scalars(
                select(OutboxEvent)
                .where(OutboxEvent.topic == "feishu.notification.send")
                .order_by(OutboxEvent.created_at, OutboxEvent.id)
            )
        )
        recipients_by_event = {
            event.payload["event_type"]: event.aggregate_id for event in events
        }
        assert recipients_by_event == {
            "review_requested": case.manager_id,
            "interview_arrangement_requested": case.creator_id,
        }


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


def test_review_task_does_not_assign_or_notify_scope_only_hiring_manager(
    tmp_path,
):
    app = make_app(tmp_path)
    case = seed_routing_case(app, suffix="organization-manager")
    enable_feishu(app, case.organization_id, case.creator_id)
    with app.state.identity_store.sync_session() as db:
        application = db.get(Application, case.application_id)
        application.stage = "review"
        job = db.get(Job, case.job_id)
        job.hiring_owner_id = None
        db.execute(
            delete(JobCollaborator).where(
                JobCollaborator.organization_id == case.organization_id,
                JobCollaborator.job_id == case.job_id,
                JobCollaborator.access_role == "job_manager",
            )
        )
        manager = db.get(User, case.manager_id)
        manager.recruiting_scope_type = "organization"

        task = ensure_review_task(
            db,
            application=application,
            job=job,
            ai_status="succeeded",
        )
        db.flush()
        events = list(
            db.scalars(
                select(OutboxEvent).where(
                    OutboxEvent.topic == "feishu.notification.send"
                )
            )
        )

        assert task.assignee_id == case.creator_id
        assert [(event.payload["event_type"], event.aggregate_id) for event in events] == [
            ("review_requested", case.creator_id)
        ]


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
