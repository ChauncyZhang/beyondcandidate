import uuid

import pytest
from sqlalchemy import select

from server.app.communications.extraction import (
    extract_email_candidates,
    persist_extracted_emails,
)
from server.app.governance.deletion_models import DeletionRequest
from server.app.identity.models import User
from server.app.recruiting.models import Candidate, CandidateContact
from server.tests.test_screening_api import app_and_seed


def test_extract_email_candidates_filters_masked_example_and_invalid_addresses() -> None:
    assert extract_email_candidates(
        "邮箱 candidate@example.com，备用 c***@mail.com，示例 user@example.test，无效 user@localhost"
    ) == ("candidate@example.com",)


def test_extract_email_candidates_normalizes_ocr_spacing_and_deduplicates_case_insensitively() -> None:
    assert extract_email_candidates(
        "联系：ALICE @ Example.com；alice@example.com；bob @ example.org"
    ) == ("ALICE@Example.com", "bob@example.org")


def test_extract_email_candidates_returns_at_most_ten_valid_addresses() -> None:
    text = " ".join(f"person{index}@example.com" for index in range(12))

    assert extract_email_candidates(text) == tuple(
        f"person{index}@example.com" for index in range(10)
    )


def test_extract_email_candidates_does_not_consider_values_after_ten_regex_candidates() -> None:
    text = " ".join(
        [*(f"sample{index}@example.test" for index in range(10)), "accepted@example.com"]
    )

    assert extract_email_candidates(text) == ()


def test_persist_extracted_emails_preserves_confirmed_contacts_and_skips_cross_candidate_conflicts(tmp_path) -> None:
    app, _, _ = app_and_seed(tmp_path)
    with app.state.identity_store.sync_session() as database:
        organization_id = database.scalar(select(User.organization_id))
        candidate = Candidate(
            id=uuid.uuid4(),
            organization_id=organization_id,
            display_name="Extraction target",
        )
        other_candidate = Candidate(
            id=uuid.uuid4(),
            organization_id=organization_id,
            display_name="Existing owner",
        )
        database.add_all((candidate, other_candidate))
        confirmed = app.state.contact_cipher.protect("email", "manual@example.com")
        conflict = app.state.contact_cipher.protect("email", "owned@example.com")
        database.add_all(
            (
                CandidateContact(
                    organization_id=organization_id,
                    candidate_id=candidate.id,
                    kind="email",
                    ciphertext=confirmed.ciphertext,
                    lookup_hash=confirmed.lookup_hash,
                    masked_value=confirmed.masked_value,
                    source="manual",
                    confirmation_status="confirmed",
                ),
                CandidateContact(
                    organization_id=organization_id,
                    candidate_id=other_candidate.id,
                    kind="email",
                    ciphertext=conflict.ciphertext,
                    lookup_hash=conflict.lookup_hash,
                    masked_value=conflict.masked_value,
                    source="native",
                ),
            )
        )
        database.flush()

        created = persist_extracted_emails(
            database,
            organization_id=organization_id,
            candidate_id=candidate.id,
            values=("manual@example.com", "owned@example.com", "new@example.com"),
            source="ocr",
            cipher=app.state.contact_cipher,
        )
        database.commit()

        contacts = list(
            database.scalars(
                select(CandidateContact)
                .where(CandidateContact.candidate_id == candidate.id)
                .order_by(CandidateContact.created_at)
            )
        )
        assert created == 1
        assert [(contact.source, contact.confirmation_status) for contact in contacts] == [
            ("manual", "confirmed"),
            ("ocr", "unconfirmed"),
        ]
        assert app.state.contact_cipher.decrypt(contacts[0].ciphertext) == "manual@example.com"
        assert app.state.contact_cipher.decrypt(contacts[1].ciphertext) == "new@example.com"


@pytest.mark.parametrize("status", ("approved", "executing", "completed"))
def test_persist_extracted_emails_skips_candidate_under_active_deletion_governance(tmp_path, status) -> None:
    app, _, _ = app_and_seed(tmp_path)
    with app.state.identity_store.sync_session() as database:
        user = database.scalar(select(User))
        candidate = Candidate(
            id=uuid.uuid4(),
            organization_id=user.organization_id,
            display_name="Deletion governed",
            owner_id=user.id,
        )
        database.add(candidate)
        database.flush()
        database.add(
            DeletionRequest(
                organization_id=user.organization_id,
                candidate_id=candidate.id,
                status=status,
                reason_code="administrator_request",
                requested_by=user.id,
                approved_by=user.id,
                impact_manifest={},
                manifest_hash="0" * 64,
                policy_version=1,
                candidate_version=candidate.version,
            )
        )
        database.flush()

        assert persist_extracted_emails(
            database,
            organization_id=user.organization_id,
            candidate_id=candidate.id,
            values=("blocked@example.com",),
            source="native",
            cipher=app.state.contact_cipher,
        ) == 0
        database.commit()

        assert database.scalar(
            select(CandidateContact.id).where(CandidateContact.candidate_id == candidate.id)
        ) is None
