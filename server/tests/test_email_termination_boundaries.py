import asyncio
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from server.app.communications.models import EmailDelivery
from server.app.communications.provider import PermanentMailError, ProviderReceipt, TemporaryMailError
from server.app.communications.worker import EmailDeliveryJobHandler
from server.app.identity.models import Job
from server.app.interviews.models import Interview
from server.app.offers.models import Offer, OfferAccessToken, OfferResponse, OfferVersion
from server.app.offers.service import OfferTokenCodec, issue_offer_access_token
from server.app.recruiting.models import Application, Candidate, Resume
from server.app.recruiting.termination import dependent_resources, preview_version, terminate_application
from server.app.recruiting.termination_models import ApplicationTermination
from server.tests.test_email_worker import FakeMailProvider, delivery_store


def recruiting_delivery(tmp_path, kind):
    sessions, cipher, delivery_id, job = delivery_store(tmp_path)
    now = datetime.now(timezone.utc)
    codec = OfferTokenCodec(b"t" * 32)
    with sessions.begin() as db:
        delivery = db.get(EmailDelivery, delivery_id)
        org, owner = delivery.organization_id, delivery.created_by
        candidate = Candidate(organization_id=org, display_name="Test")
        job_row = Job(organization_id=org, title="Test", owner_id=owner, status="open")
        db.add_all([candidate, job_row]); db.flush()
        resume = Resume(organization_id=org, candidate_id=candidate.id, file_object_id=uuid.uuid4(), version_number=1)
        db.add(resume); db.flush()
        application = Application(organization_id=org, candidate_id=candidate.id, job_id=job_row.id,
            resume_id=resume.id, owner_id=owner, stage="passed", source="manual")
        db.add(application); db.flush()
        offer = Offer(organization_id=org, application_id=application.id, job_id=job_row.id,
            current_version_id=uuid.uuid4(), status="ready_to_send", candidate_response_deadline=now + timedelta(days=2))
        db.add(offer); db.flush()
        version = OfferVersion(id=offer.current_version_id, organization_id=org, offer_id=offer.id,
            version_number=1, content={"body": "Offer"}, candidate_response_deadline=offer.candidate_response_deadline,
            is_special=False, created_by=owner)
        db.add(version); db.flush()
        token, _ = issue_offer_access_token(db, org, offer, version, codec=codec, now=now)
        if kind == "offer_access_token":
            delivery.resource_id = token.id
        elif kind == "offer_response":
            response = OfferResponse(organization_id=org, offer_id=offer.id, offer_version_id=version.id,
                version_number=1, status="accepted", source="candidate", responded_at=now,
                expected_start_date=(now + timedelta(days=7)).date(), request_hash="a" * 64)
            db.add(response); db.flush()
            offer.status, application.stage = "accepted", "hired"
            delivery.resource_id = response.id
        else:
            interview = Interview(organization_id=org, application_id=application.id, round_name="Round 1",
                method="phone", timezone="UTC", starts_at=now + timedelta(days=1), ends_at=now + timedelta(days=1, hours=1),
                status="scheduled", owner_id=owner, created_by=owner)
            db.add(interview); db.flush()
            delivery.resource_id = interview.id
        delivery.resource_type = kind
        ids = (application.id, offer.id, token.id)
    return sessions, cipher, delivery_id, job, codec, ids


def terminate(sessions, application_id):
    with sessions.begin() as db:
        application = db.get(Application, application_id)
        terminate_application(db, application, expected_version=application.version,
            expected_preview=preview_version(application, dependent_resources(db, application)),
            reason_text="Candidate withdrew", confirm_not_onboarded=True,
            actor_user_id=application.owner_id, trace_id="controlled-smtp")


@pytest.mark.parametrize("kind", ["interview_invitation", "interview_rescheduled", "offer_access_token", "offer_response"])
@pytest.mark.parametrize("status", ["queued", "failed"])
def test_terminated_mail_never_reaches_smtp(tmp_path, kind, status):
    sessions, cipher, delivery_id, job, codec, ids = recruiting_delivery(tmp_path, kind)
    with sessions.begin() as db:
        db.get(EmailDelivery, delivery_id).status = status
    terminate(sessions, ids[0])
    provider = FakeMailProvider()
    asyncio.run(EmailDeliveryJobHandler(sessions, provider, cipher, offer_token_codec=codec,
        offer_public_base_url="https://careers.example.test")(job))
    assert provider.messages == []
    with sessions() as db:
        assert db.get(EmailDelivery, delivery_id).status == "cancelled"
        assert db.get(Application, ids[0]).stage == "withdrawn"


def test_revoked_offer_token_is_checked_before_smtp(tmp_path):
    sessions, cipher, delivery_id, job, codec, ids = recruiting_delivery(tmp_path, "offer_access_token")
    with sessions.begin() as db:
        db.get(OfferAccessToken, ids[2]).revoked_at = datetime.now(timezone.utc)
    provider = FakeMailProvider()
    asyncio.run(EmailDeliveryJobHandler(sessions, provider, cipher, offer_token_codec=codec,
        offer_public_base_url="https://careers.example.test")(job))
    assert provider.messages == []
    with sessions() as db:
        assert db.get(EmailDelivery, delivery_id).status == "cancelled"


@pytest.mark.parametrize("kind", ["interview_invitation", "offer_access_token", "offer_response"])
def test_normal_recruiting_mail_still_sends(tmp_path, kind):
    sessions, cipher, delivery_id, job, codec, ids = recruiting_delivery(tmp_path, kind)
    provider = FakeMailProvider()
    asyncio.run(EmailDeliveryJobHandler(sessions, provider, cipher, offer_token_codec=codec,
        offer_public_base_url="https://careers.example.test")(job))
    assert len(provider.messages) == 1
    with sessions() as db:
        assert db.get(EmailDelivery, delivery_id).status == "sent"
        if kind == "offer_access_token":
            assert db.get(Offer, ids[1]).status == "sent"
            assert db.get(OfferAccessToken, ids[2]).delivered_at is not None
        if kind == "offer_response":
            assert db.get(Offer, ids[1]).status == "accepted"
            assert db.get(Application, ids[0]).stage == "hired"


def test_final_preflight_catches_revocation_after_message_materialization(tmp_path):
    sessions, cipher, delivery_id, job, codec, ids = recruiting_delivery(tmp_path, "offer_access_token")

    class RevokeAfterPreparation:
        calls = 0

        @contextmanager
        def begin(self):
            self.calls += 1
            with sessions.begin() as db:
                yield db
            if self.calls == 1:
                with sessions.begin() as db:
                    db.get(OfferAccessToken, ids[2]).revoked_at = datetime.now(timezone.utc)

    provider = FakeMailProvider()
    asyncio.run(EmailDeliveryJobHandler(RevokeAfterPreparation(), provider, cipher,
        offer_token_codec=codec, offer_public_base_url="https://careers.example.test")(job))
    assert provider.messages == []
    with sessions() as db:
        assert db.get(EmailDelivery, delivery_id).status == "cancelled"


@pytest.mark.parametrize("outcome,expected_status,fact_state", [
    ("success", "sent", "needs_action"), ("temporary", "cancelled", "unknown"),
    ("permanent", "cancelled", "resolved"),
])
def test_smtp_in_flight_result_is_factual_and_does_not_reactivate(tmp_path, outcome, expected_status, fact_state):
    sessions, cipher, delivery_id, job, codec, ids = recruiting_delivery(tmp_path, "offer_access_token")

    class ControlledProvider:
        calls = 0

        async def send(self, message):
            self.calls += 1
            with sessions() as db:
                assert db.get(EmailDelivery, delivery_id).safe_error_code == "email_smtp_in_flight"
            terminate(sessions, ids[0])
            with sessions() as db:
                record = db.scalar(select(ApplicationTermination))
                assert record.external_status == "pending"
                assert any(item["kind"] == "email" and item["state"] == "in_flight"
                    for item in record.external_items)
            if outcome == "temporary":
                raise TemporaryMailError("smtp_timeout")
            if outcome == "permanent":
                raise PermanentMailError("smtp_recipient_rejected")
            return ProviderReceipt("controlled-receipt")

    provider = ControlledProvider()
    handler = EmailDeliveryJobHandler(sessions, provider, cipher, offer_token_codec=codec,
        offer_public_base_url="https://careers.example.test")
    asyncio.run(handler(job))
    asyncio.run(handler(job))
    assert provider.calls == 1
    with sessions() as db:
        delivery = db.get(EmailDelivery, delivery_id)
        assert delivery.status == expected_status
        assert db.get(Application, ids[0]).stage == "withdrawn"
        assert db.get(Offer, ids[1]).status == "withdrawn"
        assert db.get(OfferAccessToken, ids[2]).revoked_at is not None
        record = db.scalar(select(ApplicationTermination))
        fact = next(item for item in record.external_items if item["kind"] == "email")
        assert fact["state"] == fact_state
        assert fact["generation"] == "1"
        if outcome == "success":
            assert delivery.provider_receipt_id == "controlled-receipt"
            assert delivery.sent_at is not None
            assert db.get(OfferAccessToken, ids[2]).delivered_at is not None
