"""Elapsed interview contracts; normal gate: python -m pytest server/tests.

SQLite API coverage proves projection and scope, not PostgreSQL locking or UI refresh.
Materials retain their existing access audit; projection must not mutate business rows.
"""

from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, select

from server.app.identity.models import Base, Organization, User, UserRole
from server.app.identity.security import PasswordService
from server.app.interviews.models import Interview, InterviewFeedback, InterviewParticipant
from server.tests.test_interview_api import feedback_payload, make_app, seed_application
from server.tests.test_recruiting_api import login


NOW = datetime(2030, 1, 2, 12, tzinfo=timezone.utc)


@pytest.fixture
def context(tmp_path, monkeypatch):
    from server.app.interviews import api

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW if tz else NOW.replace(tzinfo=None)

    monkeypatch.setattr(api, "datetime", Clock)
    app = make_app(tmp_path)
    seed = seed_application(app)
    with TestClient(app) as client:
        yield app, seed, client


def add_interview(app, seed, *, status="scheduled", end=NOW, assigned=True,
                  required=True, submitted=False, role="interviewer"):
    with app.state.identity_store.sync_session() as db:
        user = db.get(User, seed["admin_id"])
        interview = Interview(
            organization_id=user.organization_id,
            application_id=seed["application_id"],
            owner_id=user.id, created_by=user.id, round_name="Round 1",
            method="video", timezone="Asia/Shanghai", status=status,
            starts_at=end - timedelta(minutes=45), ends_at=end,
            meeting_url="https://meeting.example.test/room",
            calendar_organizer={"name": "Recruiter", "email": "recruiter@example.com"},
        )
        db.add(interview)
        db.flush()
        if assigned:
            db.add(InterviewParticipant(
                organization_id=user.organization_id, interview_id=interview.id,
                user_id=seed["interviewer_id"], required_feedback=required,
                role=role,
                task_status="completed" if submitted else "ready",
            ))
            db.flush()
        if submitted:
            db.add(InterviewFeedback(
                organization_id=user.organization_id, interview_id=interview.id,
                author_id=seed["interviewer_id"], status="submitted", submitted_at=NOW,
                **feedback_payload(),
            ))
        db.commit()
        return str(interview.id)


def snapshot(app):
    with app.state.identity_store.sync_session() as db:
        return {
            table.name: db.execute(select(table)).all()
            for table in Base.metadata.tables.values()
            if table.name != "audit_logs"
        }


@pytest.mark.parametrize("stored", ["scheduled", "rescheduled", "confirmed"])
@pytest.mark.parametrize("offset,expected", [
    (1, "scheduled"), (0, "pending_feedback"), (-1, "pending_feedback"),
])
def test_elapsed_boundary_across_read_apis(context, stored, offset, expected):
    app, seed, client = context
    expected = stored if offset > 0 else expected
    interview_id = add_interview(app, seed, status=stored, end=NOW + timedelta(microseconds=offset))
    headers = login(client, "assigned@example.test")
    before = snapshot(app)
    writes = []

    def capture(_conn, _cursor, statement, _parameters, _context, _many):
        if statement.lstrip().split()[0].upper() in {"INSERT", "UPDATE", "DELETE"}:
            writes.append(statement)

    event.listen(app.state.identity_store.engine, "before_cursor_execute", capture)
    try:
        detail = client.get(f"/api/v1/interviews/{interview_id}", headers=headers)
        listing = client.get("/api/v1/interviews", headers=headers)
        tasks = client.get("/api/v1/me/tasks", headers=headers)
        assert writes == []
        materials = client.get(f"/api/v1/interviews/{interview_id}/materials", headers=headers)
    finally:
        event.remove(app.state.identity_store.engine, "before_cursor_execute", capture)
    for response in (detail, listing, tasks, materials):
        assert response.status_code == 200
        assert response.headers["Cache-Control"] == "no-store"
    assert detail.json()["data"]["status"] == expected
    assert listing.json()["data"][0]["status"] == expected
    assert materials.json()["data"]["interview_id"] == interview_id
    assert "status" not in materials.json()["data"]
    task = tasks.json()["data"][0]
    assert task["status"] == expected
    assert task["ends_at"] == (NOW + timedelta(microseconds=offset)).isoformat()
    is_feedback = expected == "pending_feedback"
    assert task["type"] == ("interview_feedback" if is_feedback else "interview")
    assert task["id"] == f"{'feedback' if is_feedback else 'interview'}:{interview_id}"
    assert snapshot(app) == before
    assert all("audit_logs" in statement.lower() for statement in writes)


def test_timezone_and_filtering_before_pagination_and_scoping(context):
    app, seed, client = context
    ids = [add_interview(app, seed, status=status, end=NOW - timedelta(minutes=i))
           for i, status in enumerate(("scheduled", "rescheduled", "confirmed"))]
    future_id = add_interview(app, seed, end=NOW + timedelta(hours=1))
    hidden_id = add_interview(app, seed, assigned=False)
    # SQLite drops tzinfo: use UTC storage just as production scheduling does.
    with app.state.identity_store.sync_session() as db:
        item = db.get(Interview, UUID(ids[0]))
        item.ends_at = datetime.fromisoformat("2030-01-02T20:00:00+08:00").astimezone(timezone.utc)
        outsider_org = Organization(slug="elapsed-other", name="Other", status="active")
        outsider = User(organization=outsider_org, email="elapsed-other@example.test",
                        normalized_email="elapsed-other@example.test", display_name="Other",
                        password_hash=PasswordService().hash("correct horse"))
        outsider.roles.append(UserRole(role="recruiting_admin"))
        db.add(outsider)
        db.commit()
    headers = login(client, "assigned@example.test")
    seen = []
    params = {"status": "pending_feedback", "limit": 1,
              "application_id": str(seed["application_id"]),
              "interviewer_id": str(seed["interviewer_id"])}
    while True:
        response = client.get("/api/v1/interviews", headers=headers, params=params)
        assert response.status_code == 200
        page = response.json()
        assert len(page["data"]) == 1
        seen.extend(row["id"] for row in page["data"])
        cursor = page["meta"]["next_cursor"]
        if not cursor:
            break
        params["cursor"] = cursor
    assert set(seen) == set(ids)
    assert len(seen) == len(ids)
    scheduled = client.get("/api/v1/interviews", headers=headers, params={"status": "scheduled"})
    assert [row["id"] for row in scheduled.json()["data"]] == [future_id]
    assert client.get(f"/api/v1/interviews/{hidden_id}", headers=headers).status_code == 404
    unassigned = login(client, "unassigned@example.test")
    assert client.get("/api/v1/interviews?status=pending_feedback", headers=unassigned).json()["data"] == []
    outsider_login = client.post(
        "/api/v1/auth/login",
        json={"organization_slug": "elapsed-other", "email": "elapsed-other@example.test",
              "password": "correct horse"},
        headers={"Origin": "https://hr.example.test"},
    )
    assert outsider_login.status_code == 200
    outsider_headers = {"Origin": "https://hr.example.test",
                        "X-CSRF-Token": outsider_login.headers["X-CSRF-Token"]}
    assert client.get("/api/v1/interviews?status=pending_feedback", headers=outsider_headers).json()["data"] == []
    assert client.get(f"/api/v1/interviews/{ids[0]}", headers=outsider_headers).status_code == 404


def test_aware_timezone_offsets_and_naive_utc_have_same_boundary():
    from server.app.interviews.status import effective_status

    for end in (NOW, NOW.replace(tzinfo=None),
                datetime.fromisoformat("2030-01-02T20:00:00+08:00"),
                datetime.fromisoformat("2030-01-02T07:00:00-05:00")):
        interview = Interview(status="confirmed", ends_at=end)
        assert effective_status(interview, now=NOW) == "pending_feedback"
        assert effective_status(interview, now=NOW - timedelta(microseconds=1)) == "confirmed"


def test_list_filter_and_serialization_capture_one_clock_value(context, monkeypatch):
    from server.app.interviews import api

    app, seed, client = context
    ids = [add_interview(app, seed, end=NOW) for _ in range(2)]
    headers = login(client, "assigned@example.test")
    calls = []

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            calls.append(tz)
            return NOW if len(calls) == 1 else NOW - timedelta(seconds=1)

    monkeypatch.setattr(api, "datetime", Clock)
    response = client.get("/api/v1/interviews?status=pending_feedback", headers=headers)
    assert response.status_code == 200
    assert calls == [timezone.utc]
    assert {row["id"] for row in response.json()["data"]} == set(ids)
    assert {row["status"] for row in response.json()["data"]} == {"pending_feedback"}


@pytest.mark.parametrize("status", ["draft", "cancelled", "no_show", "completed", "feedback_completed", "pending_feedback"])
def test_terminal_and_other_stored_states_unchanged(context, status):
    app, seed, client = context
    interview_id = add_interview(app, seed, status=status)
    headers = login(client, "assigned@example.test")
    assert client.get(f"/api/v1/interviews/{interview_id}", headers=headers).json()["data"]["status"] == status
    assert client.get("/api/v1/interviews", params={"status": status}, headers=headers).json()["data"][0]["id"] == interview_id
    tasks = client.get("/api/v1/me/tasks", headers=headers).json()["data"]
    assert len(tasks) == (1 if status == "pending_feedback" else 0)


@pytest.mark.parametrize("required,submitted", [(False, False), (False, True), (True, True)])
def test_projection_does_not_derive_completion_from_feedback(context, required, submitted):
    app, seed, client = context
    interview_id = add_interview(app, seed, required=required, submitted=submitted)
    headers = login(client, "assigned@example.test")
    assert client.get(f"/api/v1/interviews/{interview_id}", headers=headers).json()["data"]["status"] == "pending_feedback"
    tasks = client.get("/api/v1/me/tasks", headers=headers).json()["data"]
    assert len(tasks) == (0 if submitted else 1)


@pytest.mark.parametrize("stored", ["scheduled", "rescheduled", "confirmed"])
@pytest.mark.parametrize("elapsed", [False, True])
def test_feedback_submission_still_works_before_and_after_end(context, stored, elapsed):
    app, seed, client = context
    interview_id = add_interview(app, seed, status=stored,
                                 end=NOW if elapsed else NOW + timedelta(hours=1))
    headers = login(client, "assigned@example.test")
    draft = client.put(f"/api/v1/interviews/{interview_id}/my-feedback",
                       headers={**headers, "If-Match": '"0"'}, json=feedback_payload())
    assert draft.status_code == 200
    result = client.post(f"/api/v1/interviews/{interview_id}/my-feedback/submit",
                         headers={**headers, "Idempotency-Key": "elapsed-submit"})
    assert result.status_code == 200
    assert result.json()["data"]["status"] == "submitted"
    assert client.get(f"/api/v1/interviews/{interview_id}", headers=headers).json()["data"]["status"] == "feedback_completed"
    assert client.get("/api/v1/me/tasks", headers=headers).json()["data"] == []


@pytest.mark.parametrize("stored", ["scheduled", "confirmed"])
@pytest.mark.parametrize("observer", [False, True])
def test_elapsed_management_workflow_status_is_read_only(context, stored, observer):
    app, seed, client = context
    interview_id = add_interview(app, seed, status=stored, required=not observer,
                                 role="observer" if observer else "interviewer")
    headers = login(client, "interview-admin@example.test")
    before = snapshot(app)
    detail = client.get(f"/api/v1/interviews/{interview_id}", headers=headers)
    listing = client.get("/api/v1/interviews?status=pending_feedback", headers=headers)
    for response in (detail, listing):
        assert response.status_code == 200
    for data in (detail.json()["data"], listing.json()["data"][0]):
        assert data["status"] == "pending_feedback"
        assert data["workflow_status"] == stored
        assert data["version"] == 1
    assert snapshot(app) == before


@pytest.mark.parametrize("stored,end", [
    ("pending_feedback", NOW),
    ("scheduled", NOW + timedelta(microseconds=1)),
    ("feedback_completed", NOW),
])
def test_workflow_status_absent_when_projection_matches_storage(context, stored, end):
    app, seed, client = context
    interview_id = add_interview(app, seed, status=stored, end=end)
    headers = login(client, "interview-admin@example.test")
    detail = client.get(f"/api/v1/interviews/{interview_id}", headers=headers)
    listing = client.get("/api/v1/interviews", headers=headers)
    for data in (detail.json()["data"], listing.json()["data"][0]):
        assert data["status"] == stored
        assert "workflow_status" not in data


@pytest.mark.parametrize("stored,target", [
    ("scheduled", "confirmed"), ("confirmed", "completed"),
    ("scheduled", "cancelled"), ("confirmed", "cancelled"),
    ("scheduled", "no_show"), ("confirmed", "no_show"),
])
def test_elapsed_management_transitions_remain_available(context, stored, target):
    app, seed, client = context
    interview_id = add_interview(app, seed, status=stored)
    headers = login(client, "interview-admin@example.test")
    current = client.get(f"/api/v1/interviews/{interview_id}", headers=headers).json()["data"]
    assert current["workflow_status"] == stored
    response = client.post(
        f"/api/v1/interviews/{interview_id}/transitions",
        headers={**headers, "If-Match": f'"{current["version"]}"',
                 "Idempotency-Key": f"elapsed-{target}"},
        json={"target": target, "reason": "Management regression"},
    )
    assert response.status_code == 200, response.json()
    expected = "pending_feedback" if target == "completed" else target
    with app.state.identity_store.sync_session() as db:
        interview = db.get(Interview, UUID(interview_id))
        assert interview.status == expected
        assert interview.version == 2
    data = response.json()["data"]
    assert data["status"] == ("pending_feedback" if target == "confirmed" else expected)
    if target == "confirmed":
        assert data["workflow_status"] == "confirmed"
    else:
        assert "workflow_status" not in data


def test_elapsed_observer_only_can_confirm_then_complete_without_feedback(context):
    app, seed, client = context
    interview_id = add_interview(app, seed, role="observer", required=False)
    headers = login(client, "interview-admin@example.test")
    for version, target in ((1, "confirmed"), (2, "completed")):
        current = client.get(f"/api/v1/interviews/{interview_id}", headers=headers).json()["data"]
        assert current["status"] == "pending_feedback"
        assert current["workflow_status"] == ("scheduled" if version == 1 else "confirmed")
        response = client.post(
            f"/api/v1/interviews/{interview_id}/transitions",
            headers={**headers, "If-Match": f'"{version}"',
                     "Idempotency-Key": f"observer-{target}"},
            json={"target": target},
        )
        assert response.status_code == 200
    assert response.json()["data"]["status"] == "feedback_completed"
    assert "workflow_status" not in response.json()["data"]
    with app.state.identity_store.sync_session() as db:
        interview = db.get(Interview, UUID(interview_id))
        assert interview.status == "feedback_completed"
        assert interview.version == 3
        assert db.scalar(select(InterviewFeedback).where(InterviewFeedback.interview_id == interview.id)) is None
