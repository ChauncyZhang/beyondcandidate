from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import select

from server.app.communications.models import EmailDelivery
from server.app.identity.models import Job
from server.app.integrations.feishu.models import FeishuInterviewSync
from server.app.interviews.models import Interview, InterviewFeedback, InterviewParticipant
from server.app.offers.models import OfferAccessToken, OfferApproval, OfferResponse
from server.app.onboarding.models import OnboardingRecord
from server.app.queue.models import OutboxEvent
from server.app.recruiting.models import Application, ApplicationReviewTask, Candidate
from server.app.recruiting.service import create_application_record
from server.app.recruiting.termination import (
    TerminationError, application_is_terminated, confirm_external_resolution,
    dependent_resources, preview_external_items, preview_version, record_external_fact, terminate_application,
    termination_projection,
)
from server.tests.test_offer_service import (
    APPLICATION, ORG, OWNER, command, create_offer, make_session, seed_application,
)


def stop(db, application):
    return terminate_application(db, application, expected_version=application.version,
        expected_preview=preview_version(application, dependent_resources(db, application)),
        reason_text="Candidate withdrew", confirm_not_onboarded=True, actor_user_id=OWNER, trace_id="termination-test")


def test_coordinator_closes_local_resources_without_rewriting_history_or_other_applications():
    with make_session() as db:
        application = seed_application(db)
        other_job = Job(organization_id=ORG, owner_id=OWNER, title="Other role", status="open")
        db.add(other_job)
        db.flush()
        other = Application(organization_id=ORG, candidate_id=application.candidate_id, job_id=other_job.id,
            resume_id=application.resume_id, owner_id=OWNER, stage="review")
        review_task = ApplicationReviewTask(organization_id=ORG, application_id=application.id,
            assignee_id=OWNER, ai_status="succeeded")
        other_task = ApplicationReviewTask(organization_id=ORG, application_id=other.id,
            assignee_id=OWNER, ai_status="succeeded")
        offer = create_offer(db, ORG, OWNER, command(), trace_id="test")
        timestamp = datetime.now(timezone.utc)
        token = OfferAccessToken(organization_id=ORG, offer_id=offer.id, offer_version_id=offer.current_version_id,
            token_hash="a" * 64, expires_at=timestamp + timedelta(days=1))
        pending = OfferApproval(organization_id=ORG, offer_id=offer.id, offer_version_id=offer.current_version_id,
            round_number=1, version_number=1, sequence=1, assignee_id=OWNER, status="pending")
        submitted = OfferApproval(organization_id=ORG, offer_id=offer.id, offer_version_id=offer.current_version_id,
            round_number=1, version_number=1, sequence=2, assignee_id=OWNER, status="approved", decided_at=timestamp)
        response = OfferResponse(organization_id=ORG, offer_id=offer.id, status="accepted", responded_at=timestamp)
        interview = Interview(organization_id=ORG, application_id=application.id, round_name="Technical",
            method="phone", timezone="UTC", starts_at=timestamp, ends_at=timestamp + timedelta(hours=1),
            status="pending_feedback", owner_id=OWNER, created_by=OWNER)
        db.add(other)
        db.flush()
        other_task.application_id = other.id
        db.add_all([review_task, other_task, token, pending, submitted, response, interview])
        db.flush()
        ready = InterviewParticipant(organization_id=ORG, interview_id=interview.id, user_id=OWNER, task_status="ready")
        done = InterviewParticipant(organization_id=ORG, interview_id=interview.id, user_id=uuid4(), task_status="completed")
        feedback = InterviewFeedback(organization_id=ORG, interview_id=interview.id, author_id=done.user_id,
            status="submitted", submitted_at=timestamp, strengths="Historical assessment")
        db.add_all([ready, done, feedback])
        db.flush()
        record = stop(db, application)
        db.commit()
        assert application.stage == "withdrawn" and other.stage == "review"
        assert offer.status == "withdrawn" and token.revoked_at is not None
        assert pending.status == "cancelled" and pending.decided_at is None
        assert submitted.status == "approved"
        assert response.status == "accepted"
        assert interview.status == "cancelled" and ready.task_status == "cancelled"
        assert done.task_status == "completed"
        assert feedback.status == "submitted" and feedback.strengths == "Historical assessment"
        assert record.source_stage == "passed"
        assert review_task.status == "cancelled" and other_task.status == "open"
        replacement = create_application_record(db, organization_id=ORG, candidate_id=application.candidate_id,
            job_id=application.job_id, resume_id=application.resume_id, owner_id=OWNER)
        assert replacement.stage == "new" and replacement.source_application_id == application.id
        assert not application_is_terminated(db, ORG, replacement.id)
        assert application_is_terminated(db, ORG, application.id)
        assert dependent_resources(db, replacement)["offers"] == []
        assert dependent_resources(db, replacement)["review_tasks"] == []
        assert token.revoked_at is not None and review_task.status == "cancelled"
        assert other.stage == "review" and other_task.status == "open"


@pytest.mark.parametrize("submission_state,attempts,digest,expected", [
    (None, 0, False, None), ("preflight", 1, False, None), ("prepared", 1, True, None),
    ("attempted", 1, True, "in_flight"), ("unknown", 1, True, "unknown"),
    (None, 1, True, "unknown"), ("succeeded", 1, True, "needs_action"),
])
def test_onboarding_seed_uses_durable_generation_ledger(submission_state, attempts, digest, expected):
    with make_session() as db:
        application = seed_application(db)
        generation = uuid4()
        timestamp = datetime.now(timezone.utc)
        onboarding = OnboardingRecord(organization_id=ORG, application_id=application.id,
            candidate_id=application.candidate_id, job_id=application.job_id, offer_response_id=uuid4(), offer_id=uuid4(),
            department_id=uuid4(), job_title="Role", department_name="Team", expected_start_date=timestamp.date(),
            pii_ciphertext=b"encrypted", status="failed", generation=generation, started_by=OWNER, started_at=timestamp)
        db.add(onboarding)
        db.flush()
        payload = {"generation": str(generation), "onboarding_id": str(onboarding.id)}
        if submission_state is not None:
            payload["submission_state"] = submission_state
        if digest:
            payload["submission_fingerprint"] = "a" * 64
        if submission_state == "succeeded":
            payload["instance_code"] = "instance-1"
        event = OutboxEvent(organization_id=ORG, aggregate_type="feishu_onboarding", aggregate_id=onboarding.id,
            topic="feishu.approval.onboarding.create", payload=payload, status="failed", attempts=attempts,
            created_at=timestamp, updated_at=timestamp, available_at=timestamp)
        db.add(event)
        db.flush()
        db.refresh(onboarding)
        db.refresh(event)
        before = (onboarding.status, onboarding.generation, onboarding.started_at, onboarding.pii_ciphertext, dict(event.payload))
        preview_items = preview_external_items(dependent_resources(db, application))
        record = stop(db, application)
        assert preview_items == record.external_items
        assert (onboarding.status, onboarding.generation, onboarding.started_at, onboarding.pii_ciphertext, event.payload) == before
        if expected is None:
            assert record.external_items == [] and record.external_status == "resolved"
        else:
            assert record.external_items == [{"kind": "feishu_onboarding", "resource_id": str(onboarding.id),
                "state": expected, "reference": "instance-1" if submission_state == "succeeded" else None,
                "generation": str(generation)}]
        # Fingerprints catch a durable attempt even with unchanged application version.
        old = preview_version(application, dependent_resources(db, application))
        event.payload = {**event.payload, "submission_state": "attempted" if submission_state != "attempted" else "unknown"}
        db.flush()
        assert preview_version(application, dependent_resources(db, application)) != old


def test_predicate_is_read_only_and_legacy_withdrawn_is_fenced():
    with make_session() as db:
        application = seed_application(db)
        assert not application_is_terminated(db, ORG, application.id)
        application.stage = "withdrawn"
        # The read-only predicate may not flush caller changes.
        assert not application_is_terminated(db, ORG, application.id)
        assert application in db.dirty
        db.flush()
        assert application_is_terminated(db, ORG, application.id)
        assert not application_is_terminated(db, uuid4(), application.id)
        record_external_fact(db, ORG, application.id, kind="email", resource_id=uuid4(), state="unknown")
        assert not db.new


def test_unknown_calendar_create_survives_cancellation_and_blocks_manual_ack():
    with make_session() as db:
        application = seed_application(db)
        timestamp = datetime.now(timezone.utc)
        generation = uuid4()
        interview = Interview(organization_id=ORG, application_id=application.id, round_name="Technical",
            method="phone", timezone="UTC", starts_at=timestamp, ends_at=timestamp + timedelta(hours=1),
            status="scheduled", owner_id=OWNER, created_by=OWNER)
        db.add(interview)
        db.flush()
        sync = FeishuInterviewSync(organization_id=ORG, interview_id=interview.id, desired_action="create",
            sync_status="failed", attempts=1, last_error_code="provider_timeout", idempotency_key=generation)
        db.add(sync)
        db.flush()
        preview_items = preview_external_items(dependent_resources(db, application))
        record = stop(db, application)
        assert preview_items == record.external_items
        assert record.external_status == "pending"
        assert record.external_items == [{"kind": "calendar", "resource_id": str(interview.id),
            "state": "unknown", "reference": None, "generation": str(generation)}]
        with pytest.raises(TerminationError, match="external_resolution_pending"):
            confirm_external_resolution(db, application, expected_version=record.version,
                reason_text="Checked", actor_user_id=OWNER, trace_id="test")
        record_external_fact(db, ORG, application.id, kind="calendar", resource_id=interview.id,
            state="needs_action", reference="remote-late", generation=generation)
        assert record.external_items == [{"kind": "calendar", "resource_id": str(interview.id),
            "state": "needs_action", "reference": "remote-late", "generation": str(generation)}]


def test_mail_cleanup_preserves_inflight_and_sent_facts_without_touching_unrelated_mail():
    with make_session() as db:
        application = seed_application(db)
        offer = create_offer(db, ORG, OWNER, command(), trace_id="test")
        token = OfferAccessToken(organization_id=ORG, offer_id=offer.id, offer_version_id=offer.current_version_id,
            token_hash="a" * 64, expires_at=datetime.now(timezone.utc) + timedelta(days=1))
        db.add(token)
        db.flush()
        def message(key, *, inflight=False, sent=False, unrelated=False):
            return EmailDelivery(organization_id=ORG, provider_config_id=uuid4(), provider_config_version=1,
                recipient_ciphertext=b"encrypted", recipient_masked="c***@example.com", sender_email="hr@example.com",
                sender_name="HR", reply_to_email="hr@example.com", reply_to_name="HR", rendered_subject="Offer",
                rendered_body="Historical body", resource_type="offer_access_token", resource_id=uuid4() if unrelated else token.id,
                business_dedupe_key=key, request_fingerprint="a" * 64, status="sent" if sent else "queued",
                safe_error_code="email_smtp_in_flight" if inflight else None, attempts=1 if inflight else 0)
        queued, inflight, sent, unrelated = message("queued"), message("inflight", inflight=True), message("sent", sent=True), message("other", unrelated=True)
        db.add_all([queued, inflight, sent, unrelated])
        db.flush()
        preview_items = preview_external_items(dependent_resources(db, application))
        record = stop(db, application)
        assert preview_items == record.external_items
        assert queued.status == "cancelled" and queued.safe_error_code == "application_terminated"
        assert inflight.status == "queued" and inflight.safe_error_code == "email_smtp_in_flight"
        assert sent.status == "sent" and sent.rendered_body == "Historical body"
        assert unrelated.status == "queued"
        assert record.external_items == [{"kind": "email", "resource_id": str(inflight.id),
            "state": "in_flight", "reference": None, "generation": "1"}]


@pytest.mark.parametrize("other_active", [False, True])
def test_termination_refreshes_retention_sweep_deadline_without_expiring_other_active_application(other_active):
    from server.app.governance.models import RetentionPolicy
    from server.app.governance.retention import candidate_due_dates, recalculate_candidate_retention

    with make_session() as db:
        application = seed_application(db)
        candidate_id = application.candidate_id
        policy = db.scalar(select(RetentionPolicy).where(RetentionPolicy.organization_id == ORG))
        if policy is None:
            db.add(RetentionPolicy(organization_id=ORG, terminal_days=30))
        else:
            policy.terminal_days = 30
        if other_active:
            job = Job(organization_id=ORG, owner_id=OWNER, title="Still recruiting", status="open")
            db.add(job)
            db.flush()
            db.add(Application(organization_id=ORG, candidate_id=candidate_id, job_id=job.id,
                resume_id=application.resume_id, owner_id=OWNER, stage="review"))
        db.flush()
        recalculate_candidate_retention(db, ORG, candidate_id)
        db.commit()
        assert db.get(Candidate, candidate_id).retention_due_at is None
        stop(db, application)
        db.commit()
        db.expire_all()
        actual = db.get(Candidate, candidate_id).retention_due_at
        expected = candidate_due_dates(db, ORG, 30, {candidate_id})[candidate_id]
        if other_active:
            assert actual is None and expected is None
        else:
            assert actual is not None and expected is not None
            assert actual.replace(tzinfo=timezone.utc) == expected.replace(tzinfo=timezone.utc)
            assert db.scalar(select(Candidate.id).where(Candidate.organization_id == ORG,
                Candidate.id == candidate_id, Candidate.retention_due_at <= expected)) == candidate_id


@pytest.mark.parametrize("kind,state,can_reconcile", [
    ("feishu_onboarding", "unknown", True), ("feishu_onboarding", "in_flight", True),
    ("feishu_onboarding", "needs_action", False), ("feishu_onboarding", "resolved", False),
    ("email", "unknown", False), ("calendar", "in_flight", False),
])
def test_projection_only_exposes_reconciliation_for_pending_onboarding_uncertainty(kind, state, can_reconcile):
    with make_session() as db:
        application = seed_application(db)
        assert termination_projection(db, application)["allowed_actions"]["reconcile_external"] is False
        stop(db, application)
        record_external_fact(db, ORG, application.id, kind=kind, resource_id=uuid4(), state=state, generation=uuid4())
        actions = termination_projection(db, application)["allowed_actions"]
        assert actions["reconcile_external"] is can_reconcile
        if state in {"unknown", "in_flight"}:
            assert actions["confirm_external"] is False
