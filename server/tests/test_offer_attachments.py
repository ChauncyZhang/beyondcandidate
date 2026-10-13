import asyncio
import hashlib
import io
from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from pypdf import PdfWriter
from pypdf.generic import ArrayObject, DictionaryObject, NameObject, NumberObject, TextStringObject
from sqlalchemy import select

from server.app.communications.models import EmailDelivery, EmailProviderConfig
from server.app.communications.security import EmailSecretCipher
from server.app.communications.worker import EmailDeliveryJobHandler
from server.app.identity.models import Organization, User
from server.app.offers.models import (
    Offer, OfferAccessToken, OfferApproval, OfferAttachment, OfferVersion,
)
from server.app.offers.attachments import AttachmentError, validate_pdf
from server.app.queue.models import BackgroundJob
from server.app.recruiting.models import Application
from server.app.screening.scanner import ScanResult
from server.tests.test_email_worker import FakeMailProvider
from server.tests.test_offer_api import make_app, seed_offer_application, seed_sent_offer
from server.tests.test_recruiting_api import login, seed_user


MIB = 1024 * 1024
MAX_BYTES = 10 * MIB


def active_pdf_bytes(location):
    writer = PdfWriter()
    page = writer.add_blank_page(width=595, height=842)
    action = DictionaryObject({NameObject("/S"): NameObject("/JavaScript"), NameObject("/JS"): TextStringObject("app.alert('test')")})
    action_ref = writer._add_object(action)
    additional = DictionaryObject({NameObject("/O"): action_ref})
    if location == "page":
        page[NameObject("/AA")] = additional
    elif location == "annotation":
        annotation = DictionaryObject({NameObject("/Type"): NameObject("/Annot"), NameObject("/Subtype"): NameObject("/Widget"), NameObject("/A"): action_ref})
        page[NameObject("/Annots")] = ArrayObject([writer._add_object(annotation)])
    elif location == "form":
        field = DictionaryObject({NameObject("/FT"): NameObject("/Tx"), NameObject("/AA"): additional})
        writer._root_object[NameObject("/AcroForm")] = DictionaryObject({NameObject("/Fields"): ArrayObject([writer._add_object(field)])})
    elif location == "embedded_file":
        page[NameObject("/Resources")] = DictionaryObject({NameObject("/Nested"): DictionaryObject({NameObject("/EF"): DictionaryObject()})})
    elif location == "launch_action":
        action[NameObject("/S")] = NameObject("/Launch")
        del action["/JS"]
        page[NameObject("/Resources")] = DictionaryObject({NameObject("/Nested"): action_ref})
    elif location == "indirect_launch_type":
        action[NameObject("/S")] = writer._add_object(NameObject("/Launch"))
        del action["/JS"]
        page[NameObject("/Resources")] = DictionaryObject({NameObject("/Nested"): action_ref})
    elif location == "chained_next":
        benign = DictionaryObject({NameObject("/S"): NameObject("/GoTo"), NameObject("/D"): TextStringObject("FirstPage"), NameObject("/Next"): ArrayObject([action_ref])})
        page[NameObject("/Annots")] = ArrayObject([writer._add_object(DictionaryObject({NameObject("/A"): writer._add_object(benign)}))])
    elif location == "indirect_unknown_action":
        unknown = DictionaryObject({NameObject("/Type"): writer._add_object(NameObject("/Action")), NameObject("/S"): NameObject("/Unknown")})
        page[NameObject("/Resources")] = DictionaryObject({NameObject("/Nested"): writer._add_object(unknown)})
    elif location == "indirect_embedded_subtype":
        annotation = DictionaryObject({NameObject("/Subtype"): writer._add_object(NameObject("/FileAttachment"))})
        page[NameObject("/Annots")] = ArrayObject([writer._add_object(annotation)])
    elif location == "tagged_attribute_javascript":
        attributes = DictionaryObject({NameObject("/JS"): TextStringObject("app.alert('test')")})
        element = DictionaryObject({NameObject("/Type"): NameObject("/StructElem"), NameObject("/S"): NameObject("/P"), NameObject("/A"): writer._add_object(attributes)})
        writer._root_object[NameObject("/StructTreeRoot")] = DictionaryObject({NameObject("/K"): ArrayObject([writer._add_object(element)])})
    stream = io.BytesIO()
    writer.write(stream)
    return stream.getvalue()


@pytest.mark.parametrize("location", ["page", "annotation", "form", "embedded_file", "launch_action", "indirect_launch_type", "chained_next", "indirect_unknown_action", "indirect_embedded_subtype", "tagged_attribute_javascript"])
def test_validator_rejects_active_objects_outside_catalog(location):
    content = active_pdf_bytes(location)
    with pytest.raises(AttachmentError) as error:
        validate_pdf(content)
    assert error.value.code == "offer_attachment_invalid_pdf"


def inert_signature_pdf_bytes(*, nested=0, passive_action=None, tagged=False):
    writer = PdfWriter()
    page = writer.add_blank_page(width=595, height=842)
    signature = DictionaryObject({NameObject("/FT"): NameObject("/Sig"), NameObject("/T"): TextStringObject("Official signature"), NameObject("/ByteRange"): ArrayObject([NumberObject(0), NumberObject(1), NumberObject(2), NumberObject(3)])})
    reference = writer._add_object(signature)
    # A reference cycle must terminate without discarding an inert signature field.
    signature[NameObject("/Parent")] = reference
    writer._root_object[NameObject("/AcroForm")] = DictionaryObject({NameObject("/Fields"): ArrayObject([reference])})
    child = DictionaryObject()
    for _ in range(nested):
        child = DictionaryObject({NameObject("/Child"): writer._add_object(child)})
    page[NameObject("/Resources")] = child
    if passive_action is not None:
        page[NameObject("/Annots")] = ArrayObject([writer._add_object(DictionaryObject({NameObject("/Subtype"): NameObject("/Link"), NameObject("/A"): writer._add_object(passive_action)}))])
    if tagged:
        attributes = DictionaryObject({NameObject("/O"): NameObject("/Layout"), NameObject("/Placement"): NameObject("/Block")})
        element = DictionaryObject({NameObject("/Type"): NameObject("/StructElem"), NameObject("/S"): NameObject("/P"), NameObject("/A"): writer._add_object(attributes)})
        writer._root_object[NameObject("/StructTreeRoot")] = DictionaryObject({NameObject("/Type"): NameObject("/StructTreeRoot"), NameObject("/K"): ArrayObject([writer._add_object(element)])})
    stream = io.BytesIO()
    writer.write(stream)
    return stream.getvalue()


@pytest.mark.parametrize("budget", ["MAX_PDF_DEPTH", "MAX_PDF_OBJECTS"])
def test_pdf_object_traversal_exhaustion_fails_closed(monkeypatch, budget):
    content = inert_signature_pdf_bytes(nested=10)
    monkeypatch.setattr(f"server.app.offers.attachments.{budget}", 5)
    with pytest.raises(AttachmentError) as error:
        validate_pdf(content)
    assert error.value.code == "offer_attachment_invalid_pdf"


def test_inert_signature_and_reference_cycle_preserve_exact_uploaded_bytes(ready_offer):
    app, client, _, offer_id, headers = ready_offer
    content = inert_signature_pdf_bytes()
    validate_pdf(content)
    response = upload(client, offer_id, command_headers(headers), content)
    assert response.status_code == 200, response.text
    assert_unchanged(app, offer_id, version=2, attachment=content)


def test_tagged_signature_style_letter_attributes_preserve_exact_bytes(ready_offer):
    app, client, _, offer_id, headers = ready_offer
    content = inert_signature_pdf_bytes(tagged=True)
    response = upload(client, offer_id, command_headers(headers), content)
    assert response.status_code == 200, response.text
    assert_unchanged(app, offer_id, version=2, attachment=content)


@pytest.mark.parametrize("destination", ["http://example.com", "https://example.com/offers", "mailto:hr@example.com", "local"])
def test_signature_style_pdf_with_passive_action_keeps_original_bytes(ready_offer, destination):
    app, client, _, offer_id, headers = ready_offer
    action = DictionaryObject({NameObject("/Type"): NameObject("/Action")})
    if destination == "local":
        action.update({NameObject("/S"): NameObject("/GoTo"), NameObject("/D"): TextStringObject("FirstPage")})
    else:
        action.update({NameObject("/S"): NameObject("/URI"), NameObject("/URI"): TextStringObject(destination)})
    content = inert_signature_pdf_bytes(passive_action=action)
    response = upload(client, offer_id, command_headers(headers), content)
    assert response.status_code == 200, response.text
    assert_unchanged(app, offer_id, version=2, attachment=content)


@pytest.mark.parametrize("kind", ["unknown", "missing", "javascript_uri", "file_uri"])
def test_annotation_unknown_or_nonpassive_actions_fail_closed(kind):
    action = DictionaryObject({NameObject("/Type"): NameObject("/Action")})
    if kind == "unknown":
        action[NameObject("/S")] = NameObject("/Unknown")
    elif kind != "missing":
        action.update({NameObject("/S"): NameObject("/URI"), NameObject("/URI"): TextStringObject("javascript:app.alert(1)" if kind == "javascript_uri" else "file:///private/document")})
    with pytest.raises(AttachmentError):
        validate_pdf(inert_signature_pdf_bytes(passive_action=action))


def test_clean_scanned_page_javascript_does_not_replace_attachment(ready_offer):
    app, client, _, offer_id, headers = ready_offer
    original = pdf_bytes()
    assert upload(client, offer_id, command_headers(headers), original).status_code == 200
    response = upload(client, offer_id, command_headers(headers, 2, "page-javascript"), active_pdf_bytes("page"))
    assert response.status_code == 422
    assert response.json()["code"] == "offer_attachment_invalid_pdf"
    assert_unchanged(app, offer_id, version=2, attachment=original)


def pdf_bytes(*, label="stamped offer", size=None, encrypted=False):
    writer = PdfWriter()
    writer.add_blank_page(width=595, height=842)
    writer.add_metadata({"/Title": label})
    if encrypted:
        writer.encrypt("private-test-password")
    stream = io.BytesIO()
    writer.write(stream)
    content = stream.getvalue()
    # Trailing whitespace preserves a parseable PDF while exercising byte limits.
    if size is not None:
        assert size >= len(content)
        content += b" " * (size - len(content))
    return content


@pytest.fixture
def ready_offer(tmp_path, monkeypatch):
    async def clean_scan(self, stream, max_bytes):
        assert max_bytes == MAX_BYTES
        assert len(stream.read()) <= max_bytes
        assert self.host == app.state.settings.clamav_host
        assert self.port == app.state.settings.clamav_port
        assert self.connect_timeout == app.state.settings.clamav_connect_timeout_seconds
        assert self.read_timeout == app.state.settings.clamav_read_timeout_seconds
        assert self.total_timeout == app.state.settings.clamav_total_timeout_seconds
        return ScanResult.CLEAN

    monkeypatch.setattr("server.app.offers.attachments.ClamAvScanner.scan", clean_scan)
    app = make_app(tmp_path)
    seed = seed_offer_application(app)
    offer_id = seed_sent_offer(app, seed["application_id"])
    with app.state.identity_store.sync_session() as db:
        offer = db.get(Offer, UUID(offer_id))
        offer.status = "ready_to_send"
        version = db.get(OfferVersion, offer.current_version_id)
        version.content = {"title": "Offer", "body": "Welcome", "compensation": "100000"}
        approver = db.scalar(select(User).where(User.email == seed["approver"]))
        db.add(OfferApproval(
            organization_id=offer.organization_id, offer_id=offer.id,
            offer_version_id=version.id, version_number=1, round_number=1,
            sequence=1, assignee_id=approver.id, status="approved",
        ))
        db.commit()
    with TestClient(app) as client:
        headers = login(client, seed["admin"])
        yield app, client, seed, offer_id, headers


def command_headers(headers, version=1, key="attachment-upload"):
    return {**headers, "If-Match": f'"{version}"', "Idempotency-Key": key}


def upload(client, offer_id, headers, content=None, filename="stamped.pdf", content_type="application/pdf"):
    return client.post(
        f"/api/v1/offers/{offer_id}/attachment", headers=headers,
        files={"file": (filename, pdf_bytes() if content is None else content, content_type)},
    )


def metadata(content, filename="stamped.pdf"):
    return {"filename": filename, "size_bytes": len(content),
            "content_type": "application/pdf", "sha256": hashlib.sha256(content).hexdigest()}


def attachment_rows(app, offer_id):
    with app.state.identity_store.sync_session() as db:
        return db.scalars(select(OfferAttachment).where(OfferAttachment.offer_id == UUID(offer_id))).all()


def assert_unchanged(app, offer_id, *, version=1, attachment=None):
    with app.state.identity_store.sync_session() as db:
        assert db.get(Offer, UUID(offer_id)).version == version
    rows = attachment_rows(app, offer_id)
    if attachment is None:
        assert rows == []
    else:
        assert len(rows) == 1
        assert rows[0].sha256 == hashlib.sha256(attachment).hexdigest()
        assert app.state.email_secret_cipher.decrypt_attachment(rows[0].ciphertext) == attachment


def test_upload_replace_remove_are_versioned_and_durable(ready_offer):
    app, client, seed, offer_id, headers = ready_offer
    content = pdf_bytes()
    before = client.get(f"/api/v1/offers/{offer_id}", headers=headers).json()["data"]
    assert before["attachment"] is None
    assert before["allowed_actions"]["manage_attachment"] is True
    first = upload(client, offer_id, command_headers(headers), content)
    assert first.status_code == 200, first.text
    assert first.json()["data"]["attachment"] == metadata(content)
    assert first.json()["data"]["version"] == 2
    assert first.headers["ETag"] == '"2"'
    assert first.headers["Cache-Control"] == "no-store"
    app.state.identity_store.engine.dispose()
    rows = attachment_rows(app, offer_id)
    assert len(rows) == 1
    assert rows[0].offer_version_id == UUID(before["current_version_id"])
    assert rows[0].ciphertext != content and content not in rows[0].ciphertext
    assert app.state.email_secret_cipher.decrypt_attachment(rows[0].ciphertext) == content
    reloaded = client.get(f"/api/v1/offers/{offer_id}", headers=headers)
    assert reloaded.json()["data"]["attachment"] == metadata(content)

    replacement = pdf_bytes(label="replacement")
    replaced = upload(client, offer_id, command_headers(headers, 2, "replace"), replacement, "replacement.pdf")
    assert replaced.status_code == 200, replaced.text
    assert replaced.json()["data"]["version"] == 3
    assert replaced.json()["data"]["attachment"] == metadata(replacement, "replacement.pdf")
    assert len(attachment_rows(app, offer_id)) == 1
    assert_unchanged(app, offer_id, version=3, attachment=replacement)
    removed = client.delete(f"/api/v1/offers/{offer_id}/attachment", headers=command_headers(headers, 3, "remove"))
    assert removed.status_code == 200, removed.text
    assert removed.json()["data"]["attachment"] is None
    assert removed.json()["data"]["version"] == 4
    assert removed.headers["ETag"] == '"4"'
    assert_unchanged(app, offer_id, version=4)


@pytest.mark.parametrize("method", ["POST", "DELETE"])
@pytest.mark.parametrize("missing", ["If-Match", "Idempotency-Key"])
def test_attachment_requires_both_management_headers(ready_offer, method, missing):
    app, client, _, offer_id, headers = ready_offer
    headers = command_headers(headers)
    del headers[missing]
    response = upload(client, offer_id, headers) if method == "POST" else client.delete(
        f"/api/v1/offers/{offer_id}/attachment", headers=headers)
    assert response.status_code == 428, response.text
    assert response.json()["code"] == ("precondition_required" if missing == "If-Match" else "idempotency_key_required")
    assert_unchanged(app, offer_id)


def test_upload_and_delete_replay_conflict_and_stale_requests(ready_offer, monkeypatch):
    app, client, _, offer_id, headers = ready_offer
    content = pdf_bytes()
    first = upload(client, offer_id, command_headers(headers), content)
    replay = upload(client, offer_id, command_headers(headers), content)
    assert first.status_code == replay.status_code == 200
    assert replay.json() == first.json()

    async def unavailable_scan(self, stream, max_bytes):
        raise OSError("scanner unavailable during replay")

    with monkeypatch.context() as patch:
        patch.setattr("server.app.offers.attachments.ClamAvScanner.scan", unavailable_scan)
        offline_replay = upload(client, offer_id, command_headers(headers), content)
        assert offline_replay.status_code == 200
        assert offline_replay.json() == first.json()
    for changed, filename in [(pdf_bytes(label="changed"), "stamped.pdf"), (content, "renamed.pdf")]:
        conflict = upload(client, offer_id, command_headers(headers), changed, filename)
        assert conflict.status_code == 409 and conflict.json()["code"] == "idempotency_conflict"
    stale = upload(client, offer_id, command_headers(headers, 1, "stale"), content)
    assert stale.status_code == 409 and stale.json()["code"] == "resource_version_conflict"
    stale_delete = client.delete(f"/api/v1/offers/{offer_id}/attachment", headers=command_headers(headers, 1, "stale-delete"))
    assert stale_delete.status_code == 409 and stale_delete.json()["code"] == "resource_version_conflict"
    assert_unchanged(app, offer_id, version=2, attachment=content)
    remove_headers = command_headers(headers, 2, "delete-replay")
    deleted = client.delete(f"/api/v1/offers/{offer_id}/attachment", headers=remove_headers)
    replay = client.delete(f"/api/v1/offers/{offer_id}/attachment", headers=remove_headers)
    assert deleted.status_code == replay.status_code == 200
    assert deleted.json() == replay.json()
    conflict = client.delete(f"/api/v1/offers/{offer_id}/attachment", headers=command_headers(headers, 3, "delete-replay"))
    assert conflict.status_code == 409 and conflict.json()["code"] == "idempotency_conflict"
    assert_unchanged(app, offer_id, version=3)


@pytest.mark.parametrize("case,status,code", [
    ("empty", 422, "offer_attachment_invalid_pdf"),
    ("invalid", 422, "offer_attachment_invalid_pdf"),
    ("truncated", 422, "offer_attachment_invalid_pdf"),
    ("encrypted", 422, "offer_attachment_invalid_pdf"),
    ("extension", 422, "offer_attachment_invalid_type"),
    ("mime", 422, "offer_attachment_invalid_type"),
    ("oversize", 413, "offer_attachment_too_large"),
])
def test_invalid_upload_preserves_existing_attachment(ready_offer, case, status, code):
    app, client, _, offer_id, headers = ready_offer
    original = pdf_bytes()
    assert upload(client, offer_id, command_headers(headers), original).status_code == 200
    content, filename, mime = pdf_bytes(), "stamped.pdf", "application/pdf"
    if case == "empty": content = b""
    elif case == "invalid": content = b"not a PDF"
    elif case == "truncated": content = b"%PDF-1.7\ntruncated"
    elif case == "encrypted": content = pdf_bytes(encrypted=True)
    elif case == "extension": filename = "stamped.txt"
    elif case == "mime": mime = "text/plain"
    elif case == "oversize": content = pdf_bytes(size=MAX_BYTES + 1)
    response = upload(client, offer_id, command_headers(headers, 2, "invalid-upload"), content, filename, mime)
    assert response.status_code == status, response.text
    assert response.json()["code"] == code
    assert_unchanged(app, offer_id, version=2, attachment=original)


@pytest.mark.parametrize("result,status,code", [
    (ScanResult.INFECTED, 422, "offer_attachment_malware_detected"),
    (ScanResult.UNAVAILABLE, 503, "offer_attachment_scanner_unavailable"),
    (ScanResult.ERROR, 503, "offer_attachment_scanner_unavailable"),
    (None, 503, "offer_attachment_scanner_unavailable"),
])
def test_scanner_fails_closed_without_replacing_file(ready_offer, monkeypatch, result, status, code):
    app, client, _, offer_id, headers = ready_offer
    original = pdf_bytes()
    assert upload(client, offer_id, command_headers(headers), original).status_code == 200

    async def failed_scan(self, stream, max_bytes):
        if result is None:
            raise OSError("private scanner details")
        return result

    monkeypatch.setattr("server.app.offers.attachments.ClamAvScanner.scan", failed_scan)
    response = upload(client, offer_id, command_headers(headers, 2, "scan-failure"), pdf_bytes(label="new"))
    assert response.status_code == status, response.text
    assert response.json()["code"] == code
    assert "private scanner details" not in response.text
    assert_unchanged(app, offer_id, version=2, attachment=original)


def test_one_pdf_only_and_exact_ten_mib_boundary(ready_offer):
    app, client, _, offer_id, headers = ready_offer
    duplicate = client.post(f"/api/v1/offers/{offer_id}/attachment", headers=command_headers(headers), files=[
        ("file", ("first.pdf", pdf_bytes(), "application/pdf")),
        ("file", ("second.pdf", pdf_bytes(label="second"), "application/pdf")),
    ])
    assert duplicate.status_code == 422, duplicate.text
    assert_unchanged(app, offer_id)
    content = pdf_bytes(size=MAX_BYTES)
    response = upload(client, offer_id, command_headers(headers, key="boundary"), content)
    assert response.status_code == 200, response.text
    assert response.json()["data"]["attachment"] == metadata(content)
    assert_unchanged(app, offer_id, version=2, attachment=content)


def test_filename_is_sanitized_without_changing_pdf_bytes(ready_offer):
    app, client, _, offer_id, headers = ready_offer
    content = pdf_bytes()
    response = upload(client, offer_id, command_headers(headers), content, "../../private\\signed:copy?.pdf")
    assert response.status_code == 200, response.text
    assert response.json()["data"]["attachment"] == metadata(content, "signed_copy_.pdf")
    assert attachment_rows(app, offer_id)[0].filename == "signed_copy_.pdf"
    assert_unchanged(app, offer_id, version=2, attachment=content)


def test_streamed_multipart_total_limit_rejects_oversize_framing_before_scan(ready_offer, monkeypatch):
    app, client, _, offer_id, headers = ready_offer
    scanned = False

    async def clean_scan(self, stream, max_bytes):
        nonlocal scanned
        scanned = True
        return ScanResult.CLEAN

    monkeypatch.setattr("server.app.offers.attachments.ClamAvScanner.scan", clean_scan)
    content = pdf_bytes(size=MAX_BYTES)
    prefix = (
        b'--offer-boundary\r\nContent-Disposition: form-data; name="file"; filename="'
        + b"x" * (64 * 1024)
        + b'.pdf"\r\nContent-Type: application/pdf\r\n\r\n'
    )
    suffix = b"\r\n--offer-boundary--\r\n"
    assert len(prefix) + len(content) + len(suffix) > MAX_BYTES + 64 * 1024

    def chunks():
        yield prefix
        for offset in range(0, len(content), 64 * 1024):
            yield content[offset:offset + 64 * 1024]
        yield suffix

    response = client.post(
        f"/api/v1/offers/{offer_id}/attachment",
        headers={**command_headers(headers), "Content-Type": "multipart/form-data; boundary=offer-boundary"},
        content=chunks(),
    )
    assert "content-length" not in response.request.headers
    assert response.status_code == 413, response.text
    assert response.json()["code"] == "offer_attachment_too_large"
    assert scanned is False
    assert_unchanged(app, offer_id)


def test_metadata_is_sensitive_and_approver_cannot_manage(ready_offer):
    app, client, seed, offer_id, headers = ready_offer
    content = pdf_bytes()
    assert upload(client, offer_id, command_headers(headers), content).status_code == 200
    for email, sensitive in [(seed["approver"], True), (seed["viewer"], False)]:
        actor = login(client, email)
        detail = client.get(f"/api/v1/offers/{offer_id}", headers=actor).json()["data"]
        assert detail["can_view_sensitive_content"] is sensitive
        assert detail["attachment"] == (metadata(content) if sensitive else None)
        assert detail["allowed_actions"]["manage_attachment"] is False
        listing = client.get("/api/v1/offers", headers=actor).json()["data"]
        for item in listing:
            assert item["attachment"] == (metadata(content) if sensitive else None)
        denied = upload(client, offer_id, command_headers(actor, 2, f"denied-{email}"))
        removed = client.delete(f"/api/v1/offers/{offer_id}/attachment", headers=command_headers(actor, 2, f"remove-{email}"))
        assert denied.status_code == removed.status_code == 404
    assert_unchanged(app, offer_id, version=2, attachment=content)


def test_recruiter_application_owner_can_manage_attachment(ready_offer):
    app, client, seed, offer_id, _ = ready_offer
    with app.state.identity_store.sync_session() as db:
        owner = db.scalar(select(User).where(User.email == seed["viewer"]))
        db.get(Application, UUID(seed["application_id"])).owner_id = owner.id
        db.commit()
    headers = login(client, seed["viewer"])
    assert client.get(f"/api/v1/offers/{offer_id}", headers=headers).json()["data"]["allowed_actions"]["manage_attachment"] is True
    assert upload(client, offer_id, command_headers(headers)).status_code == 200
    assert client.delete(f"/api/v1/offers/{offer_id}/attachment", headers=command_headers(headers, 2, "owner-remove")).status_code == 200
    assert_unchanged(app, offer_id, version=3)


def test_cross_tenant_and_anonymous_cannot_mutate_attachment(ready_offer):
    app, client, _, offer_id, headers = ready_offer
    content = pdf_bytes()
    assert upload(client, offer_id, command_headers(headers), content).status_code == 200
    other_id = seed_user(app, "recruiting_admin", "other-admin@example.test")
    with app.state.identity_store.sync_session() as db:
        other = Organization(slug="other", name="Other", status="active")
        db.add(other); db.flush()
        db.get(User, other_id).organization_id = other.id
        db.commit()
    authenticated = client.post("/api/v1/auth/login", json={"organization_slug": "other", "email": "other-admin@example.test", "password": "correct horse"}, headers={"Origin": "https://hr.example.test"})
    assert authenticated.status_code == 200
    actor = {"Origin": "https://hr.example.test", "X-CSRF-Token": authenticated.headers["X-CSRF-Token"]}
    for response in [
        upload(client, offer_id, command_headers(actor, 2)),
        client.delete(f"/api/v1/offers/{offer_id}/attachment", headers=command_headers(actor, 2, "other-remove")),
        client.get(f"/api/v1/offers/{offer_id}", headers=actor),
    ]:
        assert response.status_code == 404 and response.json()["code"] == "resource_not_found"
    client.cookies.clear()
    anonymous = {"Origin": "https://hr.example.test", "If-Match": '"2"', "Idempotency-Key": "anonymous"}
    for response in [upload(client, offer_id, anonymous), client.delete(f"/api/v1/offers/{offer_id}/attachment", headers=anonymous)]:
        assert response.status_code == 403
        assert response.json()["code"] == "csrf_validation_failed"
    assert_unchanged(app, offer_id, version=2, attachment=content)


@pytest.mark.parametrize("state", ["draft", "pending_approval", "changes_requested", "sent", "accepted", "declined", "withdrawn", "expired", "deadline_expired", "queued"])
def test_attachment_mutations_blocked_outside_ready_unexpired_unqueued_offer(ready_offer, state):
    app, client, _, offer_id, headers = ready_offer
    content = pdf_bytes()
    assert upload(client, offer_id, command_headers(headers), content).status_code == 200
    with app.state.identity_store.sync_session() as db:
        offer = db.get(Offer, UUID(offer_id))
        current = db.get(OfferVersion, offer.current_version_id)
        if state == "deadline_expired":
            offer.candidate_response_deadline = current.candidate_response_deadline = datetime.now(timezone.utc) - timedelta(minutes=1)
        elif state == "queued":
            db.add(OfferAccessToken(organization_id=offer.organization_id, offer_id=offer.id, offer_version_id=current.id, token_hash="a" * 64, expires_at=current.candidate_response_deadline))
        else:
            offer.status = state
        db.commit()
    detail = client.get(f"/api/v1/offers/{offer_id}", headers=headers).json()["data"]
    assert detail["allowed_actions"]["manage_attachment"] is False
    changed = upload(client, offer_id, command_headers(headers, 2, "blocked-replace"), pdf_bytes(label="new"))
    removed = client.delete(f"/api/v1/offers/{offer_id}/attachment", headers=command_headers(headers, 2, "blocked-remove"))
    assert changed.status_code == removed.status_code == 409
    expected_code = "offer_response_deadline_expired" if state == "deadline_expired" else "offer_attachment_locked"
    assert changed.json()["code"] == removed.json()["code"] == expected_code
    assert_unchanged(app, offer_id, version=2, attachment=content)


def test_revision_does_not_inherit_previous_version_attachment(ready_offer):
    app, client, _, offer_id, headers = ready_offer
    content = pdf_bytes()
    uploaded = upload(client, offer_id, command_headers(headers), content)
    assert uploaded.status_code == 200
    old_version_id = uploaded.json()["data"]["current_version_id"]
    revised = client.patch(f"/api/v1/offers/{offer_id}", headers=command_headers(headers, 2, "revise"), json={"content": {"title": "Revised", "body": "New terms", "compensation": "120000"}})
    assert revised.status_code == 200, revised.text
    data = revised.json()["data"]
    assert data["version"] == 3 and data["current_version_number"] == 2
    assert data["current_version_id"] != old_version_id
    assert data["attachment"] is None
    rows = attachment_rows(app, offer_id)
    assert len(rows) == 1 and rows[0].offer_version_id == UUID(old_version_id)
    assert app.state.email_secret_cipher.decrypt_attachment(rows[0].ciphertext) == content


def test_send_snapshots_encrypted_pdf_and_worker_delivers_same_bytes(ready_offer, monkeypatch):
    app, client, seed, offer_id, headers = ready_offer
    content = pdf_bytes(size=MIB + 1)
    assert upload(client, offer_id, command_headers(headers), content).status_code == 200
    cipher = app.state.email_secret_cipher
    with app.state.identity_store.sync_session() as db:
        user = db.scalar(select(User).where(User.email == seed["admin"]))
        db.add(EmailProviderConfig(organization_id=user.organization_id, host="smtp.example.test", port=587, tls_mode="starttls", username="mailer", encrypted_password=cipher.encrypt_smtp_password("test-password"), default_reply_to_email="hr@example.com", default_reply_to_name="HR", enabled=True, version=1, created_by=user.id, updated_by=user.id))
        db.commit()
    monkeypatch.setattr("server.app.offers.api.resolve_confirmed_candidate_email", lambda *args, **kwargs: "candidate@example.com")
    sent = client.post(f"/api/v1/offers/{offer_id}/send", headers=command_headers(headers, 2, "send"))
    assert sent.status_code == 202, sent.text
    assert sent.json()["data"]["allowed_actions"]["manage_attachment"] is False
    with app.state.identity_store.sync_session() as db:
        delivery = db.scalar(select(EmailDelivery).where(EmailDelivery.resource_type == "offer_access_token"))
        row = db.scalar(select(OfferAttachment).where(OfferAttachment.offer_id == UUID(offer_id)))
        assert delivery.attachment_ciphertext == row.ciphertext
        assert delivery.attachment_filename == "stamped.pdf"
        assert delivery.attachment_content_type == "application/pdf"
        assert content not in delivery.attachment_ciphertext
        delivery_id = delivery.id
        job = db.scalar(select(BackgroundJob).where(BackgroundJob.type == "communications.send_email"))
        # A separate durable snapshot must survive source-row removal.
        db.delete(row)
        db.commit()
    app.state.identity_store.engine.dispose()
    provider = FakeMailProvider()
    handler = EmailDeliveryJobHandler(app.state.identity_store.sync_session, provider, cipher, offer_token_codec=app.state.offer_token_codec, offer_public_base_url=app.state.settings.offer_public_base_url)
    asyncio.run(handler(job))
    asyncio.run(handler(job))
    assert len(provider.messages) == 1
    message = provider.messages[0]
    assert message.attachment_content == content
    assert message.attachment_filename == "stamped.pdf"
    assert message.attachment_content_type == "application/pdf"
    with app.state.identity_store.sync_session() as db:
        assert db.get(EmailDelivery, delivery_id).status == "sent"
        assert db.get(Offer, UUID(offer_id)).status == "sent"


def test_cipher_default_limit_unchanged_and_offer_override_bounded():
    cipher = EmailSecretCipher(b"MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY=")
    assert cipher.decrypt_attachment(cipher.encrypt_attachment(b"x" * MIB)) == b"x" * MIB
    with pytest.raises(ValueError):
        cipher.encrypt_attachment(b"x" * (MIB + 1))
    content = b"x" * MAX_BYTES
    assert cipher.decrypt_attachment(cipher.encrypt_attachment(content, max_bytes=MAX_BYTES)) == content
    with pytest.raises(ValueError):
        cipher.encrypt_attachment(content + b"x", max_bytes=MAX_BYTES)
    for invalid_limit in [0, -1, MAX_BYTES + 1]:
        with pytest.raises(ValueError):
            cipher.encrypt_attachment(b"x", max_bytes=invalid_limit)
