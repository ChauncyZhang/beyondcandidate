"""Termination fences; normal Offer contracts remain in the existing suites."""
from datetime import datetime, timezone

import pytest

from server.app.offers import service
from server.app.offers.models import OfferApproval, OfferResponse
from server.app.offers.schemas import OfferVersionCommand, PublicOfferResponse
from server.app.recruiting.models import Application
from server.tests.test_offer_access import _sent_offer
from server.tests.test_offer_service import (
    APPLICATION, DEFAULT_APPROVER, ORG, OWNER, command, make_session, seed_application,
)


def test_approval_cancellation_is_not_a_rejection_or_fabricated_decision():
    with make_session() as db:
        seed_application(db)
        offer = service.create_offer(db, ORG, OWNER, command(), trace_id="test")
        service.submit_offer(db, ORG, offer.id, OWNER, expected_version=1, trace_id="test")
        approval = db.query(OfferApproval).one()
        approval.status = "cancelled"
        db.flush()
        db.expire(approval)
        assert approval.status == "cancelled"
        assert approval.assignee_id == DEFAULT_APPROVER
        assert approval.reason is None
        assert approval.decided_at is None


def test_accepted_offer_requires_full_termination_but_unaccepted_can_be_replaced():
    with make_session() as db:
        seed_application(db)
        offer = service.create_offer(db, ORG, OWNER, command(), trace_id="test")
        service.withdraw_offer(db, ORG, offer.id, OWNER, expected_version=1, trace_id="test")
        replacement = service.create_offer(db, ORG, OWNER, command(), trace_id="test")
        replacement.status = "accepted"
        db.flush()
        with pytest.raises(service.OfferApprovalError) as error:
            service.withdraw_offer(db, ORG, replacement.id, OWNER, expected_version=1, trace_id="test")
        assert error.value.code == "application_termination_required"
        assert replacement.status == "accepted"


@pytest.mark.parametrize("operation", ["create", "update", "submit", "decide", "issue"])
def test_forward_operations_check_termination_even_if_resource_state_is_stale(monkeypatch, operation):
    from server.app.recruiting import termination

    with make_session() as db:
        seed_application(db)
        offer = service.create_offer(db, ORG, OWNER, command(), trace_id="test")
        monkeypatch.setattr(termination, "application_is_terminated", lambda db, org, app: app == APPLICATION)
        with pytest.raises(service.OfferApprovalError) as error:
            if operation == "create":
                service.create_offer(db, ORG, OWNER, command(), trace_id="test")
            elif operation == "update":
                service.update_offer_version(db, ORG, offer.id, OWNER, OfferVersionCommand(content={"x": "y"}), expected_version=1, trace_id="test")
            elif operation == "submit":
                service.submit_offer(db, ORG, offer.id, OWNER, expected_version=1, trace_id="test")
            elif operation == "decide":
                service.decide_approval(db, ORG, offer.id, DEFAULT_APPROVER, "approved", expected_version=1, trace_id="test")
            else:
                service.issue_offer_access_token(db, ORG, offer, service._current_version(db, offer), codec=service.OfferTokenCodec(b"t" * 32), now=datetime.now(timezone.utc))
        assert error.value.code == "application_terminated"


def test_public_access_and_acceptance_are_fenced_but_historical_response_remains(monkeypatch):
    from server.app.recruiting import termination

    with make_session() as db:
        codec, raw, token, offer, application = _sent_offer(db)
        monkeypatch.setattr(termination, "application_is_terminated", lambda db, org, app: True)
        with pytest.raises(service.OfferNotFound):
            service.public_offer_access(db, raw, codec=codec, now=datetime.now(timezone.utc))
        with pytest.raises(service.OfferNotFound):
            service.record_public_offer_response(db, raw, PublicOfferResponse(decision="declined"), codec=codec, now=datetime.now(timezone.utc), trace_id="test")
        assert db.query(OfferResponse).count() == 0
        assert application.stage == "passed"
        # Historical status pages can still resolve the old capability.
        assert service.public_offer_access(db, raw, codec=codec, now=datetime.now(timezone.utc), allow_inactive=True)[1].id == offer.id


def test_api_pending_and_allowed_actions_exclude_terminated_application(tmp_path, monkeypatch):
    from uuid import UUID
    from fastapi.testclient import TestClient
    from server.app.offers.models import Offer
    from server.app.recruiting import termination
    from server.tests.test_offer_api import make_app, seed_offer_application, seed_sent_offer
    from server.tests.test_recruiting_api import login

    app = make_app(tmp_path)
    seed = seed_offer_application(app)
    offer_id = seed_sent_offer(app, seed["application_id"])
    with app.state.identity_store.sync_session() as db:
        offer = db.get(Offer, UUID(offer_id))
        offer.status = "draft"
        service.submit_offer(db, offer.organization_id, offer.id, db.get(Application, UUID(seed["application_id"])).owner_id,
            expected_version=1, trace_id="test")
        db.commit()
    with TestClient(app) as client:
        approver = login(client, seed["approver"])
        assert len(client.get("/api/v1/offer-approvals/pending", headers=approver).json()["data"]) == 1
        monkeypatch.setattr(termination, "application_is_terminated", lambda db, org, app: True)
        assert client.get("/api/v1/offer-approvals/pending", headers=approver).json()["data"] == []
        admin = login(client, seed["admin"])
        view = client.get(f"/api/v1/offers/{offer_id}", headers=admin).json()["data"]
        assert not any(view["allowed_actions"].values())
        assert view["pending_approval_id"] is None
        response = client.post(f"/api/v1/offers/{offer_id}/approvals", headers={
            **admin, "If-Match": '"2"', "Idempotency-Key": "stopped"})
        assert response.status_code == 409
        assert response.json()["code"] == "application_terminated"
