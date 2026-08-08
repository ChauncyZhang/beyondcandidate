import uuid

from sqlalchemy import select

from server.app.communications.extraction import (
    extract_email_candidates,
    persist_extracted_emails,
)
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
