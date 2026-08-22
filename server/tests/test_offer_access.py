from datetime import date, datetime, timedelta, timezone
from uuid import UUID

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from server.app.core.settings import Settings
from server.app.identity.models import Base, Department, Job, Organization, User, UserRole
from server.app.onboarding.models import OnboardingRecord
from server.app.onboarding.schemas import OnboardingUpdateCommand
from server.app.onboarding.security import OnboardingPiiCipher
from server.app.onboarding.service import create_onboarding_from_accepted_offer
from server.app.offers.models import Offer, OfferAccessToken, OfferResponse, OfferVersion
from server.app.offers.schemas import ProxyOfferResponse, PublicOfferResponse
from server.app.offers.service import (
    OfferNotFound,
    OfferTokenCodec,
    OfferVersionConflict,
    issue_offer_access_token,
    public_offer_access,
    record_proxy_offer_response,
    record_public_offer_response,
    withdraw_offer,
)
from server.app.recruiting.models import Application, Candidate, Resume


def test_offer_access_token_persists_only_digest_and_reconstructs_token_from_row_id():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    codec = OfferTokenCodec(b"x" * 32)
    with Session(engine) as db:
        access, raw = codec.issue(
            organization_id="00000000-0000-0000-0000-000000000001",
            offer_id="00000000-0000-0000-0000-000000000002",
            offer_version_id="00000000-0000-0000-0000-000000000003",
            expires_at=datetime.now(timezone.utc) + timedelta(days=1),
        )
        db.add(access)
        db.commit()
        stored = db.scalar(select(OfferAccessToken))
        assert raw not in str(stored.__dict__)
        assert stored.token_hash != raw
        assert len(raw) >= 43  # base64url encoding of a 256-bit token
        assert codec.matches(stored, raw)
        assert not codec.matches(stored, raw + "x")


def _sent_offer(db):
    org_id, user_id, job_id, candidate_id, application_id = (UUID(int=value) for value in range(11, 16))
    db.add(Organization(id=org_id, slug="offer-access", name="Offer access"))
    user = User(id=user_id, organization_id=org_id, email="hr@example.test", normalized_email="hr@example.test", display_name="HR", password_hash="x")
    user.roles.append(UserRole(role="recruiting_admin")); db.add(user)
    department = Department(id=UUID(int=20), organization_id=org_id, name="Engineering", status="active")
    job = Job(id=job_id, organization_id=org_id, title="Role", department_id=department.id, owner_id=user_id, status="open", offer_approver_id=user_id)
    candidate = Candidate(id=candidate_id, organization_id=org_id, display_name="Candidate")
    resume = Resume(id=UUID(int=16), organization_id=org_id, candidate_id=candidate_id, file_object_id=UUID(int=17), version_number=1)
    application = Application(id=application_id, organization_id=org_id, candidate_id=candidate_id, job_id=job_id, resume_id=resume.id, owner_id=user_id, stage="passed", source="manual")
    version_id = UUID(int=18); offer_id = UUID(int=19)
    offer = Offer(id=offer_id, organization_id=org_id, application_id=application_id, job_id=job_id, current_version_id=version_id, status="sent", candidate_response_deadline=datetime.now(timezone.utc) + timedelta(days=2))
    version = OfferVersion(id=version_id, organization_id=org_id, offer_id=offer_id, version_number=1, content={"body": "Offer"}, candidate_response_deadline=offer.candidate_response_deadline, is_special=False, special_reason=None, created_by=user_id, pdf_object_key=f"offers/{org_id}/offers/{offer_id}/versions/{version_id}.pdf", pdf_sha256="a" * 64, pdf_size_bytes=3, pdf_rendered_at=datetime.now(timezone.utc))
    db.add_all([department, job, candidate, resume, application, offer, version]); db.flush()
    codec = OfferTokenCodec(b"t" * 32)
    token, raw = issue_offer_access_token(db, org_id, offer, version, codec=codec, now=datetime.now(timezone.utc))
    token.delivered_at = datetime.now(timezone.utc)
    db.flush()
    return codec, raw, token, offer, application


def _onboarding_data():
    return {
        "gender": "other",
        "phone": "+8613800138000",
        "email": "candidate@example.com",
        "home_address": "Shenzhen",
    }


def _onboarding_cipher():
    return OnboardingPiiCipher(b"MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY=")


def test_expired_revoked_and_superseded_tokens_are_publicly_invalid():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        codec, raw, token, offer, versioned_application = _sent_offer(db)
        token.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        with pytest.raises(OfferNotFound):
            public_offer_access(db, raw, codec=codec, now=datetime.now(timezone.utc))
        token.expires_at = datetime.now(timezone.utc) + timedelta(days=1); token.revoked_at = datetime.now(timezone.utc)
        with pytest.raises(OfferNotFound):
            public_offer_access(db, raw, codec=codec, now=datetime.now(timezone.utc))
        token.revoked_at = None
        replacement, replacement_raw = issue_offer_access_token(db, offer.organization_id, offer, db.get(OfferVersion, offer.current_version_id), codec=codec, now=datetime.now(timezone.utc))
        replacement.delivered_at = datetime.now(timezone.utc)
        assert replacement.id != token.id
        assert token.revoked_at is not None
        with pytest.raises(OfferNotFound):
            public_offer_access(db, raw, codec=codec, now=datetime.now(timezone.utc))
        assert public_offer_access(db, replacement_raw, codec=codec, now=datetime.now(timezone.utc))[0].id == replacement.id


def test_read_only_access_can_classify_inactive_links_without_making_them_mutable():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        codec, raw, token, offer, _ = _sent_offer(db)
        token.revoked_at = datetime.now(timezone.utc)
        assert public_offer_access(db, raw, codec=codec, now=datetime.now(timezone.utc), allow_inactive=True)[1].id == offer.id
        with pytest.raises(OfferNotFound):
            record_public_offer_response(db, raw, PublicOfferResponse(decision="declined"), codec=codec, now=datetime.now(timezone.utc), trace_id="trace")


def test_withdrawal_revokes_the_public_capability_and_blocks_a_response():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        codec, raw, token, offer, application = _sent_offer(db)
        withdraw_offer(db, offer.organization_id, offer.id, application.owner_id, trace_id="trace", expected_version=offer.version)
        assert token.revoked_at is not None
        with pytest.raises(OfferNotFound):
            record_public_offer_response(db, raw, PublicOfferResponse(decision="accepted", expected_start_date=date(2026, 9, 1), onboarding_data=_onboarding_data()), codec=codec, onboarding_cipher=_onboarding_cipher(), now=datetime.now(timezone.utc), trace_id="trace")


def test_identical_public_response_replays_conflicts_and_transitions_application():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        codec, raw, token, offer, application = _sent_offer(db)
        accepted = PublicOfferResponse(decision="accepted", expected_start_date=date(2026, 9, 1), onboarding_data=_onboarding_data())
        response, duplicate = record_public_offer_response(db, raw, accepted, codec=codec, onboarding_cipher=_onboarding_cipher(), now=datetime.now(timezone.utc), trace_id="trace")
        replay, replay_duplicate = record_public_offer_response(db, raw, accepted, codec=codec, onboarding_cipher=_onboarding_cipher(), now=datetime.now(timezone.utc), trace_id="trace")
        assert response.id == replay.id and duplicate is False and replay_duplicate is True
        assert db.get(Application, application.id).stage == "hired"
        onboarding = db.scalar(select(OnboardingRecord).where(OnboardingRecord.offer_response_id == response.id))
        assert onboarding is not None and onboarding.status == "ready"
        assert b"candidate@example.com" not in onboarding.pii_ciphertext
        assert _onboarding_cipher().decrypt(onboarding.pii_ciphertext)["name"] == "Candidate"
        with pytest.raises(OfferVersionConflict):
            record_public_offer_response(db, raw, PublicOfferResponse(decision="declined", reason_text="No"), codec=codec, now=datetime.now(timezone.utc), trace_id="trace")


def test_decline_uses_real_workflow_and_stable_internal_reason_when_omitted():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        codec, raw, _, offer, application = _sent_offer(db)
        response, duplicate = record_public_offer_response(db, raw, PublicOfferResponse(decision="declined"), codec=codec, now=datetime.now(timezone.utc), trace_id="trace")
        assert (response.status, duplicate, db.get(Application, application.id).stage) == ("declined", False, "withdrawn")


def test_proxy_response_records_immutable_source_snapshot_and_wins_against_candidate():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        codec, raw, token, offer, application = _sent_offer(db)
        communicated_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        payload = ProxyOfferResponse(
            decision="accepted", expected_start_date=date(2026, 9, 1),
            channel="phone", communicated_at=communicated_at, note="候选人电话确认",
        )
        response, duplicate = record_proxy_offer_response(
            db, offer, payload, actor_user_id=application.owner_id,
            now=datetime.now(timezone.utc), trace_id="trace",
        )
        assert duplicate is False
        assert response.source == "hr_proxy" and response.actor_user_id == application.owner_id
        assert response.offer_version_id == offer.current_version_id and response.version_number == 1
        assert response.communication_channel == "phone" and response.note == "候选人电话确认"
        assert token.revoked_at is not None and db.get(Application, application.id).stage == "hired"
        with pytest.raises(OfferVersionConflict):
            record_public_offer_response(db, raw, PublicOfferResponse(decision="accepted", expected_start_date=date(2026, 9, 1), onboarding_data=_onboarding_data()), codec=codec, onboarding_cipher=_onboarding_cipher(), now=datetime.now(timezone.utc), trace_id="candidate")


def test_proxy_response_validation_rejects_future_time_and_missing_start_date():
    with pytest.raises(ValueError):
        ProxyOfferResponse(decision="accepted", channel="phone", communicated_at=datetime.now(timezone.utc))
    with pytest.raises(ValueError):
        ProxyOfferResponse(decision="declined", channel="phone", communicated_at=datetime.now(timezone.utc) + timedelta(minutes=1))
    assert "ck_offer_responses_source" in {constraint.name for constraint in OfferResponse.__table__.constraints}


def test_hr_can_restore_historical_accepted_offer_with_a_missing_start_date():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        _, _, _, offer, application = _sent_offer(db)
        offer.status = "accepted"
        response = OfferResponse(
            organization_id=offer.organization_id,
            offer_id=offer.id,
            status="accepted",
            source=None,
            responded_at=datetime.now(timezone.utc),
        )
        db.add(response)
        db.flush()
        record = create_onboarding_from_accepted_offer(
            db,
            application,
            OnboardingUpdateCommand.model_validate({
                "onboarding_data": _onboarding_data(),
                "expected_start_date": "2026-09-01",
            }),
            cipher=_onboarding_cipher(),
            expected_version=0,
            actor_user_id=application.owner_id,
            trace_id="historical-offer",
        )

        assert record.offer_response_id == response.id
        assert record.expected_start_date == date(2026, 9, 1)


@pytest.mark.parametrize("value", ["https://careers.example.test/path", "https://user@careers.example.test", "https://careers.example.test/?q=1", "https://careers.example.test/#x", "ftp://careers.example.test"])
def test_offer_public_base_url_rejects_non_origins(value):
    with pytest.raises(ValueError):
        Settings(environment="test", offer_public_base_url=value)


def test_offer_public_base_url_normalizes_and_requires_https_in_production():
    assert Settings(environment="test", offer_public_base_url="http://careers.example.test/").offer_public_base_url == "http://careers.example.test"
    with pytest.raises(ValueError):
        Settings(environment="production", offer_public_base_url="http://careers.example.test")
