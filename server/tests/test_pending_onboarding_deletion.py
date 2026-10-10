from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import aliased

from server.app.governance.deletion_models import DeletionRequest
from server.app.identity.models import Job, Organization
from server.app.offers.models import Offer, OfferResponse, OfferVersion
from server.app.onboarding.eligibility import candidate_deletion_pending, pending_candidate_deletion
from server.app.recruiting import api as recruiting_api
from server.app.recruiting.models import Application, Candidate
from server.tests.test_workbench_api import make_app, principal, seed_application, seed_user


@pytest.fixture
def deletion_context(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    now = datetime(2026, 7, 1, tzinfo=timezone.utc)
    with app.state.identity_store.sync_session() as db:
        org = Organization(slug="deletion", name="Deletion", status="active")
        other = Organization(slug="foreign", name="Foreign", status="active")
        owner = seed_user(db, org, "recruiting_admin", "owner@example.test")
        other_owner = seed_user(db, other, "recruiting_admin", "foreign@example.test")
        job = Job(organization_id=org.id, title="Target", owner_id=owner.id, status="open")
        foreign_job = Job(organization_id=other.id, title="Foreign", owner_id=other_owner.id, status="open")
        db.add_all([job, foreign_job])
        db.flush()
        applications = []
        for index, target in enumerate([job, job, foreign_job]):
            actor = owner if target is job else other_owner
            application = seed_application(db, target, actor, index, "hired", now)
            applications.append(application)
            version_id = uuid4()
            offer = Offer(organization_id=target.organization_id, application_id=application.id,
                          job_id=target.id, current_version_id=version_id, status="accepted",
                          candidate_response_deadline=now + timedelta(days=7))
            db.add(offer)
            db.flush()
            db.add(OfferVersion(id=version_id, organization_id=target.organization_id,
                                offer_id=offer.id, version_number=1, content={}, is_special=False,
                                candidate_response_deadline=offer.candidate_response_deadline,
                                created_by=actor.id))
            db.flush()
            db.add(OfferResponse(organization_id=target.organization_id, offer_id=offer.id,
                                 status="accepted", responded_at=now))
        requests = []
        for application, actor in [(applications[0], owner), (applications[2], other_owner)]:
            row = DeletionRequest(organization_id=application.organization_id,
                                  candidate_id=application.candidate_id, status="requested",
                                  reason_code="candidate_request", requested_by=actor.id,
                                  impact_manifest={}, manifest_hash="0" * 64,
                                  policy_version=1, candidate_version=1)
            db.add(row)
            requests.append(row)
        db.commit()
        actor_principal = principal(owner)
        context = dict(app=app, org_id=org.id, other_org_id=other.id, job_id=str(job.id),
                       applications=[(row.id, row.candidate_id) for row in applications],
                       request_id=requests[0].id)
    monkeypatch.setattr(recruiting_api, "_principal", lambda request: actor_principal)
    return context


def assert_pending_surfaces(client, context, expected):
    response = client.get("/api/v1/workbench")
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    tasks = data["tasks"]["onboarding"]
    assert tasks["count"] == len(expected)
    assert {item["application_id"] for item in tasks["items"]} == expected
    board = next(job for job in data["jobs"] if job["id"] == context["job_id"])
    group = board["stages"]["pending_onboarding"]
    assert group["count"] == board["active_count"] == len(expected)
    assert {item["application_id"] for item in group["items"]} == expected
    listing = client.get("/api/v1/candidates", params={"job_id": context["job_id"],
                                                    "stage": "pending_onboarding"})
    assert listing.status_code == 200, listing.text
    assert {row["application"]["id"] for row in listing.json()["data"]} == expected
    funnel = client.get(f"/api/v1/jobs/{context['job_id']}/funnel")
    assert funnel.status_code == 200, funnel.text
    assert funnel.json()["data"] == {"job_id": context["job_id"], "total": 2,
                                     "stages": {"hired": 2, "pending_onboarding": len(expected)}}
    for application_id, candidate_id in context["applications"][:2]:
        history = client.get(f"/api/v1/candidates/{candidate_id}/applications")
        assert history.status_code == 200, history.text
        assert history.json()["data"][0]["stage"] == "hired"
        assert history.json()["data"][0]["pending_onboarding"] == (str(application_id) in expected)


@pytest.mark.parametrize("status", ["requested", "approved", "executing", "failed", "completed"])
def test_deletion_states_use_same_tenant_scoped_pending_predicate(deletion_context, status):
    context = deletion_context
    app = context["app"]
    blocked = status != "completed"
    target_id, candidate_id = context["applications"][0]
    with app.state.identity_store.sync_session() as db:
        db.get(DeletionRequest, context["request_id"]).status = status
        db.commit()
        assert candidate_deletion_pending(db, context["org_id"], candidate_id) is blocked
        assert candidate_deletion_pending(db, context["other_org_id"], candidate_id) is False
        assert candidate_deletion_pending(db, context["org_id"], context["applications"][2][1]) is False
        expected_candidates = {candidate_id} if blocked else set()
        for model in [Candidate, aliased(Candidate)]:
            matches = set(db.scalars(select(model.id).where(
                model.organization_id == context["org_id"],
                pending_candidate_deletion(model.organization_id, model.id),
            )))
            assert matches == expected_candidates
        for model in [Application, aliased(Application)]:
            matches = set(db.scalars(select(model.id).where(
                model.organization_id == context["org_id"],
                pending_candidate_deletion(model.organization_id, model.candidate_id),
            )))
            assert matches == ({target_id} if blocked else set())
    expected = {str(row[0]) for row in context["applications"][:2]}
    if blocked:
        expected.remove(str(target_id))
    with TestClient(app) as client:
        assert_pending_surfaces(client, context, expected)


def test_completed_request_does_not_override_candidate_deleted_at(deletion_context):
    context = deletion_context
    app = context["app"]
    target_id, candidate_id = context["applications"][0]
    expected = {str(context["applications"][1][0])}
    with TestClient(app) as client:
        assert_pending_surfaces(client, context, expected)
        with app.state.identity_store.sync_session() as db:
            db.get(DeletionRequest, context["request_id"]).status = "completed"
            db.commit()
        # Predicate restoration is dynamic; no application stage writes are needed.
        assert_pending_surfaces(client, context, expected | {str(target_id)})
        with app.state.identity_store.sync_session() as db:
            db.get(Candidate, candidate_id).deleted_at = datetime.now(timezone.utc)
            db.commit()
        listing = client.get("/api/v1/candidates", params={"job_id": context["job_id"],
                                                        "stage": "pending_onboarding"})
        assert listing.status_code == 200
        assert {row["application"]["id"] for row in listing.json()["data"]} == expected
        with app.state.identity_store.sync_session() as db:
            assert db.get(Application, target_id).stage == "hired"
