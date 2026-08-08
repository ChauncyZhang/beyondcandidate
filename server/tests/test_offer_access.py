from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from server.app.identity.models import Base
from server.app.offers.models import OfferAccessToken
from server.app.offers.service import OfferTokenCodec


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
