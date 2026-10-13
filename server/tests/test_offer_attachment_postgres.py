"""Narrow smoke against an already migrated disposable DB; never resets schema."""

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from server.app.communications.models import EmailDelivery, EmailProviderConfig
from server.app.offers.models import OfferAccessToken, OfferAttachment
from server.tests.test_migrations import _seed_offer_rows


pytestmark = pytest.mark.skipif(not os.getenv("POSTGRES_SMOKE_URL"), reason="isolated PostgreSQL smoke URL not configured")


def test_attachment_schema_preserves_approved_content_and_purges_private_bytes():
    engine = create_engine(os.environ["POSTGRES_SMOKE_URL"].replace("+asyncpg", "+psycopg"))
    try:
        with engine.connect() as connection:
            transaction = connection.begin()
            try:
                assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "0042_offer_attachments"
                ids = _seed_offer_rows(connection)
                connection.execute(text("UPDATE offers SET status='ready_to_send' WHERE id=:offer"), ids)
                connection.execute(text("UPDATE offer_versions SET submitted_at=now() WHERE id=:version"), ids)
                snapshot = connection.scalar(text("SELECT row_to_json(v)::text FROM offer_versions v WHERE id=:version"), ids)
                with Session(bind=connection) as db:
                    attachment = OfferAttachment(
                        organization_id=ids["organization"], offer_id=ids["offer"], offer_version_id=ids["version"],
                        filename="stamped.pdf", content_type="application/pdf", size_bytes=100,
                        sha256="a" * 64, ciphertext=b"encrypted-formal-pdf",
                    )
                    db.add(attachment)
                    db.flush()
                    attachment.ciphertext = b"encrypted-replacement-pdf"
                    db.flush()
                    assert connection.scalar(text("SELECT row_to_json(v)::text FROM offer_versions v WHERE id=:version"), ids) == snapshot
                    for statement in (
                        "UPDATE offer_versions SET content='{}' WHERE id=:version",
                        "UPDATE offer_attachments SET organization_id=:other_organization WHERE offer_version_id=:version",
                        "UPDATE offer_attachments SET size_bytes=10485761 WHERE offer_version_id=:version",
                        "UPDATE offer_attachments SET sha256=repeat('g',64) WHERE offer_version_id=:version",
                    ):
                        with pytest.raises(DBAPIError):
                            with connection.begin_nested():
                                connection.execute(text(statement), ids)
                    provider = EmailProviderConfig(
                        organization_id=ids["organization"], host="smtp.example.test", port=587,
                        tls_mode="starttls", username="mailer", encrypted_password=b"encrypted-password",
                        default_reply_to_email="hr@example.com", default_reply_to_name="HR",
                        enabled=True, version=1, created_by=ids["owner"], updated_by=ids["owner"],
                    )
                    token = OfferAccessToken(
                        organization_id=ids["organization"], offer_id=ids["offer"], offer_version_id=ids["version"],
                        token_hash=uuid.uuid4().hex * 2, expires_at=datetime.now(timezone.utc) + timedelta(days=7),
                    )
                    db.add_all([provider, token])
                    db.flush()
                    delivery = EmailDelivery(
                        organization_id=ids["organization"], provider_config_id=provider.id, provider_config_version=1,
                        recipient_ciphertext=b"encrypted-recipient", recipient_masked="c***@example.test",
                        sender_email="hr@example.test", sender_name="HR", reply_to_email="hr@example.test", reply_to_name="HR",
                        rendered_subject="Offer", rendered_body="Offer link", attachment_filename=attachment.filename,
                        attachment_content_type=attachment.content_type, attachment_ciphertext=attachment.ciphertext,
                        resource_type="offer_access_token", resource_id=token.id,
                        business_dedupe_key="attachment-smoke", request_fingerprint="b" * 64,
                    )
                    db.add(delivery)
                    db.flush()
                    connection.execute(text("UPDATE candidates SET deleted_at=now() WHERE id=:candidate"), ids)
                    assert connection.scalar(text("SELECT count(*) FROM offer_attachments WHERE offer_version_id=:version"), ids) == 0
                    row = connection.execute(text("SELECT attachment_filename,attachment_content_type,attachment_ciphertext FROM email_deliveries WHERE id=:id"), {"id": delivery.id}).one()
                    assert tuple(row) == (None, None, None)
                    assert connection.scalar(text("SELECT row_to_json(v)::text FROM offer_versions v WHERE id=:version"), ids) == snapshot
                    # Migration copies existing parent DML ACLs, without introducing a broader role.
                    assert connection.scalar(text("""
                        SELECT count(*) FROM pg_class c
                        CROSS JOIN LATERAL aclexplode(c.relacl) a
                        JOIN pg_roles r ON r.oid=a.grantee
                        WHERE c.oid='public.offers'::regclass
                          AND a.privilege_type IN ('SELECT','INSERT','UPDATE','DELETE')
                          AND NOT has_table_privilege(r.oid, 'public.offer_attachments', a.privilege_type)
                    """)) == 0
            finally:
                transaction.rollback()
    finally:
        engine.dispose()
