from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from server.app.identity.models import Base, Job, Organization, User, UserRole
from server.app.offers.models import Offer, OfferAccessToken, OfferVersion
from server.app.offers.schemas import PublicOfferResponse
from server.app.offers.service import (
    OfferNotFound,
    OfferTokenCodec,
    OfferVersionConflict,
    issue_offer_access_token,
    public_offer_access,
    record_public_offer_response,
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
    job = Job(id=job_id, organization_id=org_id, title="Role", owner_id=user_id, status="open", offer_approver_id=user_id)
    candidate = Candidate(id=candidate_id, organization_id=org_id, display_name="Candidate")
    resume = Resume(id=UUID(int=16), organization_id=org_id, candidate_id=candidate_id, file_object_id=UUID(int=17), version_number=1)
    application = Application(id=application_id, organization_id=org_id, candidate_id=candidate_id, job_id=job_id, resume_id=resume.id, owner_id=user_id, stage="passed", source="manual")
    version_id = UUID(int=18); offer_id = UUID(int=19)
    offer = Offer(id=offer_id, organization_id=org_id, application_id=application_id, job_id=job_id, current_version_id=version_id, status="sent", candidate_response_deadline=datetime.now(timezone.utc) + timedelta(days=2))
    version = OfferVersion(id=version_id, organization_id=org_id, offer_id=offer_id, version_number=1, content={"body": "Offer"}, candidate_response_deadline=offer.candidate_response_deadline, is_special=False, special_reason=None, created_by=user_id, pdf_object_key=f"offers/{org_id}/offers/{offer_id}/versions/{version_id}.pdf", pdf_sha256="a" * 64, pdf_size_bytes=3, pdf_rendered_at=datetime.now(timezone.utc))
    db.add_all([job, candidate, resume, application, offer, version]); db.flush()
    codec = OfferTokenCodec(b"t" * 32)
    token, raw = issue_offer_access_token(db, org_id, offer, version, codec=codec, now=datetime.now(timezone.utc))
    db.flush()
    return codec, raw, token, offer, application


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
        assert replacement.id != token.id
        assert token.revoked_at is not None
        with pytest.raises(OfferNotFound):
            public_offer_access(db, raw, codec=codec, now=datetime.now(timezone.utc))
        assert public_offer_access(db, replacement_raw, codec=codec, now=datetime.now(timezone.utc))[0].id == replacement.id


def test_identical_public_response_replays_and_conflict_is_rejected(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        codec, raw, token, offer, application = _sent_offer(db)
        calls = []
        monkeypatch.setattr("server.app.recruiting.service.apply_application_workflow_action_record", lambda *args, **kwargs: calls.append((args, kwargs)) or application)
        accepted = PublicOfferResponse(decision="accepted", expected_start_date=datetime(2026, 9, 1, tzinfo=timezone.utc))
        response, duplicate = record_public_offer_response(db, raw, accepted, codec=codec, now=datetime.now(timezone.utc), trace_id="trace")
        replay, replay_duplicate = record_public_offer_response(db, raw, accepted, codec=codec, now=datetime.now(timezone.utc), trace_id="trace")
        assert response.id == replay.id and duplicate is False and replay_duplicate is True
        assert calls[0][0][3] == "offer_accepted"
        with pytest.raises(OfferVersionConflict):
            record_public_offer_response(db, raw, PublicOfferResponse(decision="declined", reason_text="No"), codec=codec, now=datetime.now(timezone.utc), trace_id="trace")
