from datetime import datetime, timedelta, timezone
from uuid import UUID

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import select

from server.app.communications.models import EmailProviderConfig
from server.app.interviews.models import Interview, InterviewFeedback, InterviewParticipant
from server.app.interviews import api as interview_api
from server.app.recruiting.models import Application, CandidateContact
from server.tests.test_interview_api import (
    create_interview,
    feedback_payload,
    interview_payload,
    make_app,
    seed_application,
)
from server.tests.test_recruiting_api import login


def test_cancel_pending_feedback_keeps_submitted_history_and_stops_draft(tmp_path):
    app = make_app(tmp_path)
    seed = seed_application(app)
    payload = interview_payload(seed)
    payload["participants"].append({"user_id": str(seed["other_interviewer_id"]), "required_feedback": True})
    with TestClient(app) as client:
        created, _ = create_interview(client, seed, payload=payload)
        interview_id = UUID(created.json()["data"]["id"])
        with app.state.identity_store.sync_session() as db:
            db.get(Interview, interview_id).status = "pending_feedback"
            participants = db.scalars(select(InterviewParticipant).where(InterviewParticipant.interview_id == interview_id)).all()
            for participant in participants:
                submitted = participant.user_id == seed["interviewer_id"]
                participant.task_status = "completed" if submitted else "ready"
                db.add(InterviewFeedback(
                    organization_id=participant.organization_id,
                    interview_id=interview_id,
                    author_id=participant.user_id,
                    status="submitted" if submitted else "draft",
                    submitted_at=datetime.now(timezone.utc) if submitted else None,
                    **feedback_payload(),
                ))
            db.commit()
        headers = login(client, "interview-admin@example.test")
        cancelled = client.post(f"/api/v1/interviews/{interview_id}/transitions", json={"target": "cancelled", "reason": "Candidate cannot attend"}, headers={**headers, "If-Match": '"1"', "Idempotency-Key": "cancel-feedback"})
        assert cancelled.status_code == 200, cancelled.text
        submitted_author = login(client, "assigned@example.test")
        history = client.get(f"/api/v1/interviews/{interview_id}/my-feedback", headers=submitted_author)
        assert history.status_code == 200
        assert history.json()["data"]["status"] == "submitted"
        amended = client.post(f"/api/v1/interview-feedback/{history.json()['data']['id']}/amendments", json={**feedback_payload(), "notes": "Historical correction", "reason": "Correct recorded detail"}, headers={**submitted_author, "If-Match": '"1"'})
        assert amended.status_code == 200, amended.text
        author_headers = login(client, "unassigned@example.test")
        blocked = client.post(f"/api/v1/interviews/{interview_id}/my-feedback/submit", headers={**author_headers, "Idempotency-Key": "cancelled-feedback"})
        assert blocked.status_code == 409
    with app.state.identity_store.sync_session() as db:
        assert db.get(Application, seed["application_id"]).stage == "interviewing"
        participants = db.scalars(select(InterviewParticipant).where(InterviewParticipant.interview_id == interview_id)).all()
        assert {p.user_id: p.task_status for p in participants} == {seed["interviewer_id"]: "completed", seed["other_interviewer_id"]: "cancelled"}
        feedbacks = db.scalars(select(InterviewFeedback).where(InterviewFeedback.interview_id == interview_id)).all()
        assert {f.author_id: f.status for f in feedbacks} == {seed["interviewer_id"]: "amended", seed["other_interviewer_id"]: "draft"}
        assert next(f for f in feedbacks if f.author_id == seed["interviewer_id"]).notes == "Historical correction"


def test_cancel_without_confirmed_email_stops_locally_with_manual_notice(tmp_path):
    app = make_app(tmp_path)
    seed = seed_application(app)
    with TestClient(app) as client:
        created, headers = create_interview(client, seed)
        interview_id = created.json()["data"]["id"]
        with app.state.identity_store.sync_session() as db:
            contact = db.scalar(select(CandidateContact).where(CandidateContact.candidate_id == seed["candidate_id"]))
            contact.confirmation_status = "unconfirmed"
            contact.confirmed_by = contact.confirmed_at = None
            db.commit()
        response = client.post(f"/api/v1/interviews/{interview_id}/transitions", json={"target": "cancelled", "reason": "Not attending"}, headers={**headers, "If-Match": '"1"', "Idempotency-Key": "manual-cancel"})
        assert response.status_code == 200, response.text
        assert response.json()["data"]["email_delivery"]["safe_error_code"] == "candidate_email_unconfirmed"
        assert response.json()["data"]["email_delivery"]["status"] == "not_sent"
        assert response.json()["data"]["status"] == "cancelled"


def test_single_cancellation_allows_replacement_interview_in_same_application(tmp_path):
    app = make_app(tmp_path)
    seed = seed_application(app)
    with TestClient(app) as client:
        created, headers = create_interview(client, seed)
        interview_id = created.json()["data"]["id"]
        response = client.post(f"/api/v1/interviews/{interview_id}/transitions", json={"target": "cancelled", "reason": "Schedule a replacement"}, headers={**headers, "If-Match": '"1"', "Idempotency-Key": "replacement-cancel"})
        assert response.status_code == 200
        replacement, _ = create_interview(client, seed, key="replacement-interview")
        assert replacement.json()["data"]["application_id"] == str(seed["application_id"])
        assert replacement.json()["data"]["id"] != interview_id


def test_withdrawn_application_rejects_partial_feedback_and_reschedule(tmp_path):
    app = make_app(tmp_path)
    seed = seed_application(app)
    payload = interview_payload(seed)
    payload["participants"].append({"user_id": str(seed["other_interviewer_id"]), "required_feedback": True})
    with TestClient(app) as client:
        created, _ = create_interview(client, seed, payload=payload)
        interview_id = created.json()["data"]["id"]
        interviewer = login(client, "assigned@example.test")
        assert client.put(f"/api/v1/interviews/{interview_id}/my-feedback", json=feedback_payload(), headers={**interviewer, "If-Match": '"0"'}).status_code == 200
        with app.state.identity_store.sync_session() as db:
            # A stale worker need not have cancelled this row yet to enforce the boundary.
            db.get(Application, seed["application_id"]).stage = "withdrawn"
            db.commit()
        blocked = client.post(f"/api/v1/interviews/{interview_id}/my-feedback/submit", headers={**interviewer, "Idempotency-Key": "ended-feedback"})
        assert blocked.status_code == 409, blocked.text
        admin = login(client, "interview-admin@example.test")
        start = datetime(2099, 7, 22, 8, tzinfo=timezone.utc)
        moved = client.patch(f"/api/v1/interviews/{interview_id}", json={"starts_at": start.isoformat(), "ends_at": (start + timedelta(minutes=45)).isoformat()}, headers={**admin, "If-Match": '"1"'})
        assert moved.status_code == 409, moved.text
    with app.state.identity_store.sync_session() as db:
        assert db.scalar(select(InterviewFeedback).where(InterviewFeedback.interview_id == UUID(interview_id))).status == "draft"


def test_cancellation_requires_reason_and_manager_permission(tmp_path):
    app = make_app(tmp_path)
    seed = seed_application(app)
    with TestClient(app) as client:
        created, headers = create_interview(client, seed)
        interview_id = created.json()["data"]["id"]
        blank = client.post(f"/api/v1/interviews/{interview_id}/transitions", json={"target": "cancelled", "reason": "  "}, headers={**headers, "If-Match": '"1"', "Idempotency-Key": "blank-cancel"})
        assert blank.status_code == 422
        interviewer = login(client, "assigned@example.test")
        forbidden = client.post(f"/api/v1/interviews/{interview_id}/transitions", json={"target": "cancelled", "reason": "Not HR"}, headers={**interviewer, "If-Match": '"1"', "Idempotency-Key": "unauthorized-cancel"})
        assert forbidden.status_code == 404


def test_invalid_notification_configuration_cannot_roll_back_cancellation(tmp_path):
    app = make_app(tmp_path)
    seed = seed_application(app)
    with TestClient(app, raise_server_exceptions=False) as client:
        created, headers = create_interview(client, seed)
        interview_id = created.json()["data"]["id"]
        with app.state.identity_store.sync_session() as db:
            config = db.scalar(select(EmailProviderConfig))
            config.sender_address, config.sender_name = "sender@example.com", "Invalid\nSender"
            db.commit()
        response = client.post(f"/api/v1/interviews/{interview_id}/transitions", json={"target": "cancelled", "reason": "Cannot attend"}, headers={**headers, "If-Match": '"1"', "Idempotency-Key": "invalid-mail-cancel"})
        assert response.status_code == 200, response.text
        assert response.json()["data"]["status"] == "cancelled"
        assert response.json()["data"]["email_delivery"]["safe_error_code"] == "interview_email_invalid"


@pytest.mark.parametrize("action", ["cancel", "reschedule", "draft", "submit"])
def test_interview_mutations_refresh_identity_when_cancellation_wins(tmp_path, monkeypatch, action):
    from server.app.interviews.cancellation import cancel_interview_record
    from server.app.recruiting import termination

    app = make_app(tmp_path)
    seed = seed_application(app)
    with TestClient(app) as client:
        created, headers = create_interview(client, seed)
        interview_id = UUID(created.json()["data"]["id"])
        if action in {"draft", "submit"}:
            headers = login(client, "assigned@example.test")
        if action == "submit":
            assert client.put(f"/api/v1/interviews/{interview_id}/my-feedback", json=feedback_payload(), headers={**headers, "If-Match": '"0"'}).status_code == 200
        original = interview_api._lock_application_candidate_retention
        changed = []

        def cancel_before_lock(db, organization_id, application_id):
            if not changed:
                changed.append(True)
                with app.state.identity_store.sync_session() as other:
                    application = termination.lock_application(other, organization_id, application_id)
                    if action == "cancel":
                        termination.terminate_application(other, application, expected_version=application.version,
                            expected_preview=termination.preview_version(application, termination.dependent_resources(other, application)),
                            reason_text="Candidate withdrew", confirm_not_onboarded=False,
                            actor_user_id=seed["admin_id"], trace_id="stop-before-cancel")
                    else:
                        interview = other.scalar(select(Interview).where(Interview.id == interview_id).with_for_update())
                        cancel_interview_record(other, interview, actor_user_id=seed["admin_id"],
                            trace_id="cancel-before-forward", reason_text="Candidate cannot attend")
                    other.commit()
            return original(db, organization_id, application_id)

        monkeypatch.setattr(interview_api, "_lock_application_candidate_retention", cancel_before_lock)
        path = f"/api/v1/interviews/{interview_id}"
        if action == "cancel":
            response = client.post(path + "/transitions", json={"target": "cancelled", "reason": "Stale cancellation"}, headers={**headers, "If-Match": '"1"', "Idempotency-Key": "stale-cancel"})
        elif action == "reschedule":
            start = datetime(2099, 7, 22, 8, tzinfo=timezone.utc)
            response = client.patch(path, json={"starts_at": start.isoformat(), "ends_at": (start + timedelta(minutes=45)).isoformat()}, headers={**headers, "If-Match": '"1"'})
        elif action == "draft":
            response = client.put(path + "/my-feedback", json=feedback_payload(), headers={**headers, "If-Match": '"0"'})
        else:
            response = client.post(path + "/my-feedback/submit", headers={**headers, "Idempotency-Key": "stale-feedback"})
        assert response.status_code == 409, response.text
    with app.state.identity_store.sync_session() as db:
        from server.app.interviews.models import InterviewEvent
        current = db.get(Interview, interview_id)
        assert current.status == "cancelled" and current.version == 2
        assert len(db.scalars(select(InterviewEvent).where(InterviewEvent.interview_id == interview_id, InterviewEvent.event_type == "interview.cancelled")).all()) == 1
        assert not db.scalars(select(InterviewFeedback).where(InterviewFeedback.interview_id == interview_id, InterviewFeedback.status == "submitted")).all()
