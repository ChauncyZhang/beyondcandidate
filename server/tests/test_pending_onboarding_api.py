from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from server.app.identity.models import Department, Job, JobCollaborator, Organization
from server.app.onboarding.models import OnboardingRecord
from server.app.offers.models import Offer, OfferResponse, OfferVersion
from server.app.recruiting import api as recruiting_api
from server.app.recruiting.models import Candidate
from server.tests.test_workbench_api import make_app, principal, seed_application, seed_user


@pytest.mark.parametrize("role", ["recruiting_admin", "recruiter", "hiring_manager"])
def test_pending_onboarding_consistent_across_lists_history_and_funnel(tmp_path, monkeypatch, role):
    app = make_app(tmp_path)
    now = datetime(2026, 7, 1, tzinfo=timezone.utc)
    with app.state.identity_store.sync_session() as db:
        org = Organization(slug="pending", name="Pending", status="active")
        other_org = Organization(slug="other", name="Other", status="active")
        owner = seed_user(db, org, "recruiting_admin", "owner@example.test")
        actor = seed_user(db, org, role, "actor@example.test")
        other_owner = seed_user(db, other_org, "recruiting_admin", "other@example.test")
        department = Department(organization_id=org.id, name="Engineering")
        db.add(department)
        db.flush()
        job = Job(organization_id=org.id, title="Target", owner_id=owner.id,
                  department_id=department.id, status="open")
        hidden = Job(organization_id=org.id, title="Hidden", owner_id=owner.id, status="open")
        foreign = Job(organization_id=other_org.id, title="Foreign", owner_id=other_owner.id, status="open")
        db.add_all([job, hidden, foreign])
        db.flush()
        db.add(JobCollaborator(organization_id=org.id, job_id=job.id, user_id=actor.id,
                              access_role="job_manager" if role == "hiring_manager" else "job_recruiter"))
        cases = ["missing", "ready", "submitting", "failed", "submitted", "deleted",
                 "plain_hired", "offer_sent", "response_declined", "not_hired"]
        entries = {}
        for index, case in enumerate([*cases, "hidden", "foreign"]):
            target = hidden if case == "hidden" else foreign if case == "foreign" else job
            target_owner = other_owner if case == "foreign" else owner
            application = seed_application(db, target, target_owner, index,
                                           "passed" if case == "not_hired" else "hired",
                                           now + timedelta(minutes=index))
            entries[case] = (str(application.id), str(application.candidate_id))
            if case == "plain_hired":
                continue
            version_id = uuid4()
            offer = Offer(organization_id=target.organization_id, application_id=application.id,
                          job_id=target.id, current_version_id=version_id,
                          status="sent" if case == "offer_sent" else "accepted",
                          candidate_response_deadline=now + timedelta(days=7))
            db.add(offer)
            db.flush()
            db.add(OfferVersion(id=version_id, organization_id=target.organization_id, offer_id=offer.id,
                                version_number=1, content={"title": "Offer"}, is_special=False,
                                candidate_response_deadline=offer.candidate_response_deadline,
                                created_by=target_owner.id))
            db.flush()
            response = OfferResponse(organization_id=target.organization_id, offer_id=offer.id,
                                     status="declined" if case == "response_declined" else "accepted",
                                     responded_at=now)
            db.add(response)
            db.flush()
            if case in {"ready", "submitting", "failed", "submitted"}:
                started = case != "ready"
                db.add(OnboardingRecord(
                    organization_id=org.id, offer_response_id=response.id, offer_id=offer.id,
                    application_id=application.id, candidate_id=application.candidate_id, job_id=job.id,
                    department_id=department.id, job_title=job.title, department_name=department.name,
                    expected_start_date=now.date(), pii_ciphertext=b"not-needed-for-projection", status=case,
                    generation=uuid4() if started else None, started_by=owner.id if started else None,
                    started_at=now if started else None, submitted_at=now if case == "submitted" else None,
                    feishu_instance_code="submitted" if case == "submitted" else None,
                ))
            if case == "deleted":
                db.get(Candidate, application.candidate_id).deleted_at = now
        db.commit()
        actor_principal = principal(actor)
        job_id, hidden_id, foreign_id = str(job.id), str(hidden.id), str(foreign.id)

    expected = {entries[case][0] for case in ("missing", "ready", "submitting", "failed")} if role != "hiring_manager" else set()
    monkeypatch.setattr(recruiting_api, "_principal", lambda request: actor_principal)
    with TestClient(app) as client:
        filtered = client.get("/api/v1/candidates", params={"job_id": job_id, "stage": "pending_onboarding", "limit": 2})
        assert filtered.status_code == 200, filtered.text
        collected = filtered.json()["data"]
        cursor = filtered.json()["meta"].get("next_cursor")
        while cursor:
            page = client.get("/api/v1/candidates", params={"job_id": job_id, "stage": "pending_onboarding", "limit": 2, "cursor": cursor})
            assert page.status_code == 200, page.text
            collected.extend(page.json()["data"])
            cursor = page.json()["meta"].get("next_cursor")
        assert {row["application"]["id"] for row in collected} == expected
        assert all(row["application"]["pending_onboarding"] is True for row in collected)

        listing = client.get("/api/v1/candidates", params={"job_id": job_id})
        assert listing.status_code == 200, listing.text
        summaries = {row["application"]["id"]: row["application"] for row in listing.json()["data"]}
        assert len(summaries) == len(cases) - 1
        for case in cases:
            application_id, candidate_id = entries[case]
            if case == "deleted":
                assert application_id not in summaries
                continue
            assert summaries[application_id]["pending_onboarding"] == (application_id in expected)
            history = client.get(f"/api/v1/candidates/{candidate_id}/applications")
            assert history.status_code == 200, history.text
            assert history.json()["data"][0]["pending_onboarding"] == (application_id in expected)
            assert history.json()["data"][0]["stage"] == ("passed" if case == "not_hired" else "hired")

        funnel = client.get(f"/api/v1/jobs/{job_id}/funnel")
        assert funnel.status_code == 200, funnel.text
        assert funnel.json()["data"] == {"job_id": job_id, "total": 10,
                                         "stages": {"hired": 9, "passed": 1, "pending_onboarding": len(expected)}}
        for inaccessible_id in [foreign_id, *([hidden_id] if role != "recruiting_admin" else [])]:
            denied_list = client.get("/api/v1/candidates", params={"job_id": inaccessible_id, "stage": "pending_onboarding"})
            assert denied_list.status_code == 200
            assert denied_list.json()["data"] == []
            assert client.get(f"/api/v1/jobs/{inaccessible_id}/funnel").status_code == 404
        hired = client.get("/api/v1/candidates", params={"job_id": job_id, "stage": "hired"})
        assert hired.status_code == 200
        assert len(hired.json()["data"]) == 8
