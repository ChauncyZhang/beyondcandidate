import os
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from fastapi.testclient import TestClient

from server.app.identity.models import Job, JobCollaborator
from server.app.recruiting.models import Application, ApplicationReviewTask, ApplicationStageEvent, IdempotencyRecord
from server.tests.test_interview_api import seed_application
from server.tests.test_interview_api_postgres import postgres_app
from server.tests.test_recruiting_api import login, seed_user
from server.tests.test_talent_api import create_pool_and_membership


pytestmark = pytest.mark.skipif(
    not os.getenv("POSTGRES_SMOKE_URL"), reason="PostgreSQL smoke URL not configured"
)


def test_postgres_concurrent_same_key_deferred_referral_returns_one_result() -> None:
    app=postgres_app(); seed=seed_application(app)
    with app.state.identity_store.sync_session() as database:
        source=database.get(Application,seed["application_id"]); source.stage="deferred"; source.version=1
        job=database.get(Job,source.job_id); job.status="open"; job.hiring_owner_id=None
        database.commit()
    with TestClient(app) as client:
        _,_,membership=create_pool_and_membership(client,seed); membership_id=membership.json()["data"]["id"]
    barrier=Barrier(2)
    def refer():
        with TestClient(app) as client:
            headers=login(client,"interview-admin@example.test"); barrier.wait()
            return client.post(f"/api/v1/talent-pool-memberships/{membership_id}/review-referrals",json={},headers={**headers,"Idempotency-Key":"same-referral","If-Match":'"1"'})
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses=[future.result() for future in (pool.submit(refer),pool.submit(refer))]
    assert [response.status_code for response in responses]==[200,200]
    assert responses[0].json()==responses[1].json()
    with app.state.identity_store.sync_session() as database:
        source=database.get(Application,seed["application_id"])
        assert source.stage=="review" and source.version==2
        assert database.query(ApplicationReviewTask).filter_by(application_id=source.id,status="open").count()==1
        assert database.query(ApplicationStageEvent).filter_by(application_id=source.id,event_type="application.stage_changed").count()==1
        assert database.query(IdempotencyRecord).filter_by(operation="talent_pool.review_referral",idempotency_key="same-referral").count()==1


def test_postgres_concurrent_reactivations_create_only_one_active_application() -> None:
    app = postgres_app()
    seed = seed_application(app)
    with app.state.identity_store.sync_session() as database:
        source = database.get(Application, seed["application_id"])
        source.stage = "rejected"
        source.version += 1
        target = Job(
            organization_id=source.organization_id,
            title="Concurrent RAG Engineer",
            owner_id=seed["admin_id"],
            status="open",
        )
        database.add(target)
        database.flush()
        database.add(
            JobCollaborator(
                organization_id=source.organization_id,
                job_id=target.id,
                user_id=seed["admin_id"],
                access_role="job_owner",
            )
        )
        database.commit()
        target_id = target.id

    with TestClient(app) as client:
        _, _, membership = create_pool_and_membership(client, seed)
        membership_id = membership.json()["data"]["id"]

    def reactivate(key):
        with TestClient(app) as client:
            return client.post(
                f"/api/v1/talent-pool-memberships/{membership_id}/reactivations",
                json={"job_id": str(target_id)},
                headers={**login(client, "interview-admin@example.test"), "Idempotency-Key": key},
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(reactivate, ("concurrent-reactivate-a", "concurrent-reactivate-b")))

    assert sorted(response.status_code for response in responses) == [201, 409]
    conflict = next(response for response in responses if response.status_code == 409)
    assert conflict.json()["code"] == "active_application_exists"

    with app.state.identity_store.sync_session() as database:
        active = database.query(Application).filter(
            Application.organization_id == source.organization_id,
            Application.candidate_id == seed["candidate_id"],
            Application.job_id == target_id,
            Application.stage.not_in(("hired", "rejected", "withdrawn")),
        ).all()
        assert len(active) == 1
        assert active[0].source_application_id == seed["application_id"]


def test_postgres_reactivation_racing_ordinary_creation_returns_controlled_conflict() -> None:
    app = postgres_app()
    seed = seed_application(app)
    recruiter_id = seed_user(app, "recruiter", "mixed-writer@example.test")
    with app.state.identity_store.sync_session() as database:
        source = database.get(Application, seed["application_id"])
        source.stage = "rejected"
        source.version += 1
        target = Job(
            organization_id=source.organization_id,
            title="Mixed writer RAG Engineer",
            owner_id=recruiter_id,
            status="open",
        )
        database.add(target)
        database.flush()
        database.add_all(
            [JobCollaborator(
                organization_id=source.organization_id,
                job_id=target.id,
                user_id=recruiter_id,
                access_role="job_owner",
            ), JobCollaborator(
                organization_id=source.organization_id,
                job_id=source.job_id,
                user_id=recruiter_id,
                access_role="job_recruiter",
            )]
        )
        database.commit()
        target_id = target.id
        resume_id = source.resume_id

    with TestClient(app) as client:
        _, _, membership = create_pool_and_membership(client, seed)
        membership_id = membership.json()["data"]["id"]

    barrier = Barrier(2)

    def reactivate():
        with TestClient(app) as client:
            headers = login(client, "interview-admin@example.test")
            barrier.wait()
            return client.post(
                f"/api/v1/talent-pool-memberships/{membership_id}/reactivations",
                json={"job_id": str(target_id)},
                headers={**headers, "Idempotency-Key": "mixed-writer-reactivation"},
            )

    def create_ordinary():
        with TestClient(app) as client:
            headers = login(client, "mixed-writer@example.test")
            barrier.wait()
            return client.post(
                f"/api/v1/jobs/{target_id}/applications",
                json={"candidate_id": str(seed["candidate_id"]), "resume_id": str(resume_id)},
                headers={**headers, "Idempotency-Key": "mixed-writer-ordinary"},
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = [pool.submit(reactivate), pool.submit(create_ordinary)]
        responses = [future.result() for future in responses]

    assert sorted(response.status_code for response in responses) == [201, 409], [
        (response.status_code, response.json()) for response in responses
    ]
    conflict = next(response for response in responses if response.status_code == 409)
    assert conflict.json()["code"] == "active_application_exists"
    with app.state.identity_store.sync_session() as database:
        active = database.query(Application).filter(
            Application.organization_id == source.organization_id,
            Application.candidate_id == seed["candidate_id"],
            Application.job_id == target_id,
            Application.stage.not_in(("hired", "rejected", "withdrawn")),
        ).all()
        assert len(active) == 1
