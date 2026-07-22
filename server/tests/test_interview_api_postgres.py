import os
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text

from server.app.core.settings import Settings
from server.app.interviews.models import Interview, InterviewFeedback, InterviewFeedbackRevision
from server.app.main import create_app
from server.app.recruiting.models import Application
from server.tests.test_interview_api import (
    Probe,
    create_interview,
    feedback_payload,
    interview_payload,
    seed_application,
)
from server.tests.test_recruiting_api import login


pytestmark = pytest.mark.skipif(
    not os.getenv("POSTGRES_SMOKE_URL"), reason="PostgreSQL smoke URL not configured"
)


def postgres_app():
    url = os.environ["POSTGRES_SMOKE_URL"]
    subprocess.run(
        ["python", "-m", "alembic", "-c", "server/alembic.ini", "upgrade", "head"],
        check=True,
        env={**os.environ, "DATABASE_URL": url},
    )
    app = create_app(
        settings=Settings(
            environment="test",
            database_url=url,
            cors_origins=["https://hr.example.test"],
        ),
        database_probe=Probe(),
        storage_probe=Probe(),
    )
    with app.state.identity_store.sync_session() as database:
        database.execute(text("TRUNCATE organizations CASCADE"))
        database.commit()
    return app


def test_postgres_feedback_amendment_uses_the_database_history_guard() -> None:
    app = postgres_app()
    seed = seed_application(app)

    with TestClient(app) as client:
        created, admin_headers = create_interview(client, seed, key="postgres-create")
        interview_id = created.json()["data"]["id"]
        assert client.post(
            f"/api/v1/interviews/{interview_id}/transitions",
            json={"target": "confirmed"},
            headers={**admin_headers, "If-Match": '"1"', "Idempotency-Key": "postgres-confirm"},
        ).status_code == 200
        assert client.post(
            f"/api/v1/interviews/{interview_id}/transitions",
            json={"target": "completed"},
            headers={**admin_headers, "If-Match": '"2"', "Idempotency-Key": "postgres-complete"},
        ).status_code == 200
        author_headers = login(client, "assigned@example.test")
        assert client.put(
            f"/api/v1/interviews/{interview_id}/my-feedback",
            json=feedback_payload(),
            headers={**author_headers, "If-Match": '"0"'},
        ).status_code == 200
        submitted = client.post(
            f"/api/v1/interviews/{interview_id}/my-feedback/submit",
            headers={**author_headers, "Idempotency-Key": "postgres-submit"},
        )
        feedback = submitted.json()["data"]
        amended = client.post(
            f"/api/v1/interview-feedback/{feedback['id']}/amendments",
            json={
                **feedback_payload("strong_recommend"),
                "notes": "PostgreSQL trigger amendment",
                "reason": "Verified production evidence",
            },
            headers={**author_headers, "If-Match": '"2"'},
        )
        assert amended.status_code == 200
        assert amended.json()["data"]["status"] == "amended"
        assert amended.json()["data"]["version"] == 3

    with app.state.identity_store.sync_session() as database:
        stored = database.get(InterviewFeedback, UUID(feedback["id"]))
        revision = database.scalar(
            select(InterviewFeedbackRevision).where(
                InterviewFeedbackRevision.feedback_id == stored.id
            )
        )
        assert stored.notes == "PostgreSQL trigger amendment"
        assert revision.previous_payload["notes"] == "建议进入下一轮"
        assert revision.new_payload["notes"] == "PostgreSQL trigger amendment"
        assert revision.reason == "Verified production evidence"


def test_postgres_concurrent_overlapping_schedules_create_only_one_interview() -> None:
    app = postgres_app()
    seed = seed_application(app)
    barrier = threading.Barrier(2)

    def schedule(index: int):
        with TestClient(app) as client:
            headers = login(client, "interview-admin@example.test")
            barrier.wait()
            return client.post(
                "/api/v1/interviews",
                json=interview_payload(seed),
                headers={**headers, "Idempotency-Key": f"concurrent-schedule-{index}"},
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(schedule, (1, 2)))

    assert sorted(response.status_code for response in responses) == [201, 409]
    conflict = next(response for response in responses if response.status_code == 409)
    assert conflict.json()["code"] == "schedule_hard_conflict"
    with app.state.identity_store.sync_session() as database:
        assert database.scalar(select(func.count(Interview.id))) == 1


def test_postgres_concurrent_candidate_overlap_with_different_interviewers_creates_one() -> None:
    app = postgres_app()
    seed = seed_application(app)
    barrier = threading.Barrier(2)

    def schedule(index: int):
        payload = interview_payload(seed)
        if index == 2:
            payload["participants"] = [
                {
                    "user_id": str(seed["other_interviewer_id"]),
                    "role": "interviewer",
                    "required_feedback": True,
                }
            ]
        with TestClient(app) as client:
            headers = login(client, "interview-admin@example.test")
            barrier.wait()
            return client.post(
                "/api/v1/interviews",
                json=payload,
                headers={**headers, "Idempotency-Key": f"concurrent-candidate-{index}"},
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(schedule, (1, 2)))

    assert sorted(response.status_code for response in responses) == [201, 409]
    assert next(response for response in responses if response.status_code == 409).json()["code"] == "schedule_hard_conflict"
    with app.state.identity_store.sync_session() as database:
        assert database.scalar(select(func.count(Interview.id))) == 1


def test_postgres_concurrent_final_feedback_submissions_advance_once() -> None:
    app = postgres_app()
    seed = seed_application(app)
    payload = interview_payload(seed)
    payload["participants"].append(
        {
            "user_id": str(seed["other_interviewer_id"]),
            "role": "interviewer",
            "required_feedback": True,
        }
    )
    with TestClient(app) as client:
        created, admin_headers = create_interview(client, seed, key="concurrent-feedback-create", payload=payload)
        interview_id = created.json()["data"]["id"]
        assert client.post(
            f"/api/v1/interviews/{interview_id}/transitions",
            json={"target": "confirmed"},
            headers={**admin_headers, "If-Match": '"1"', "Idempotency-Key": "concurrent-feedback-confirm"},
        ).status_code == 200
        assert client.post(
            f"/api/v1/interviews/{interview_id}/transitions",
            json={"target": "completed"},
            headers={**admin_headers, "If-Match": '"2"', "Idempotency-Key": "concurrent-feedback-complete"},
        ).status_code == 200
        first_headers = login(client, "assigned@example.test")
        assert client.put(
            f"/api/v1/interviews/{interview_id}/my-feedback",
            json=feedback_payload(),
            headers={**first_headers, "If-Match": '"0"'},
        ).status_code == 200
        second_headers = login(client, "unassigned@example.test")
        assert client.put(
            f"/api/v1/interviews/{interview_id}/my-feedback",
            json=feedback_payload("strong_recommend"),
            headers={**second_headers, "If-Match": '"0"'},
        ).status_code == 200

    barrier = threading.Barrier(2)

    def submit(identity):
        email, key = identity
        with TestClient(app) as client:
            headers = login(client, email)
            barrier.wait()
            return client.post(
                f"/api/v1/interviews/{interview_id}/my-feedback/submit",
                headers={**headers, "Idempotency-Key": key},
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(
            pool.map(
                submit,
                (
                    ("assigned@example.test", "concurrent-first-submit"),
                    ("unassigned@example.test", "concurrent-second-submit"),
                ),
            )
        )
    assert [response.status_code for response in responses] == [200, 200]
    with app.state.identity_store.sync_session() as database:
        interview = database.get(Interview, UUID(interview_id))
        application = database.get(Application, seed["application_id"])
        assert interview.status == "feedback_completed"
        assert application.stage == "decision"


def test_postgres_concurrent_first_feedback_drafts_return_one_version_conflict() -> None:
    app = postgres_app()
    seed = seed_application(app)
    with TestClient(app) as client:
        created, admin_headers = create_interview(client, seed, key="concurrent-draft-create")
        interview_id = created.json()["data"]["id"]
        assert client.post(
            f"/api/v1/interviews/{interview_id}/transitions",
            json={"target": "confirmed"},
            headers={**admin_headers, "If-Match": '"1"', "Idempotency-Key": "concurrent-draft-confirm"},
        ).status_code == 200
        assert client.post(
            f"/api/v1/interviews/{interview_id}/transitions",
            json={"target": "completed"},
            headers={**admin_headers, "If-Match": '"2"', "Idempotency-Key": "concurrent-draft-complete"},
        ).status_code == 200

    barrier = threading.Barrier(2)

    def save(conclusion):
        with TestClient(app) as client:
            headers = login(client, "assigned@example.test")
            barrier.wait()
            return client.put(
                f"/api/v1/interviews/{interview_id}/my-feedback",
                json=feedback_payload(conclusion),
                headers={**headers, "If-Match": '"0"'},
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(save, ("recommend", "strong_recommend")))

    assert sorted(response.status_code for response in responses) == [200, 409]
    conflict = next(response for response in responses if response.status_code == 409)
    assert conflict.json()["code"] == "resource_version_conflict"
    with app.state.identity_store.sync_session() as database:
        assert database.scalar(
            select(func.count(InterviewFeedback.id)).where(
                InterviewFeedback.interview_id == UUID(interview_id),
                InterviewFeedback.author_id == seed["interviewer_id"],
            )
        ) == 1
