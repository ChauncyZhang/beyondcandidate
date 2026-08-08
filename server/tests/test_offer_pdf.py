from __future__ import annotations

import hashlib
import inspect
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from importlib.metadata import version as installed_version
from io import BytesIO
from pathlib import Path
from threading import Barrier, Lock
from types import SimpleNamespace
from uuid import UUID

import pytest
from minio import Minio
from pypdf import PdfReader
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from server.app.identity.models import Base
from server.app.offers.models import OfferVersion
from server.app.offers.pdf import (
    MAX_PDF_BYTES,
    MAX_TEMPLATE_BYTES,
    MinioOfferPdfStorage,
    OfferPdfError,
    OfferPdfStorageError,
    OfferPdfStorageConflict,
    REQUIRED_PLACEHOLDERS,
    render_offer_pdf,
)
from server.app.offers.service import OfferNotFound, persist_offer_version_pdf


TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN"><head><title>录用通知书</title>
<style>
body { color: #202124; }
h1 { text-align: center; }
.facts { border: 1px solid #ddd; padding: 12px; }
</style></head><body>
<h1>{{ organization_name }}录用通知书</h1>
<p>尊敬的{{ candidate_name }}：</p>
<div class="facts">
<p>职位：{{ job_title }}</p><p>工作地点：{{ work_location }}</p>
<p>确认截止：{{ response_deadline }}</p><p>招聘联系人：{{ hr_name }}（{{ hr_email }}）</p>
</div></body></html>"""

VARIABLES = {
    "organization_name": "示例科技有限公司",
    "candidate_name": "张三",
    "job_title": "后端工程师",
    "work_location": "上海",
    "response_deadline": "2026年8月20日",
    "hr_name": "李经理",
    "hr_email": "hr@example.test",
}


def _font_names(reader: PdfReader) -> set[str]:
    names: set[str] = set()
    for page in reader.pages:
        fonts = page["/Resources"].get("/Font", {})
        for font in fonts.values():
            resolved = font.get_object()
            names.add(str(resolved.get("/BaseFont", "")))
            descendants = resolved.get("/DescendantFonts", [])
            for descendant in descendants:
                names.add(str(descendant.get_object().get("/BaseFont", "")))
    return names


def test_windows_renderer_produces_deterministic_extractable_chinese_with_fixed_font() -> None:
    first = render_offer_pdf(TEMPLATE, VARIABLES)
    second = render_offer_pdf(TEMPLATE, VARIABLES)

    assert first == second
    assert len(first) <= MAX_PDF_BYTES
    reader = PdfReader(BytesIO(first), strict=True)
    assert len(reader.pages) == 1
    text = "".join(page.extract_text() or "" for page in reader.pages)
    assert "示例科技有限公司录用通知书" in text
    assert "尊敬的张三" in text
    assert "后端工程师" in text
    assert any("NotoSansCJK" in name.replace(" ", "").replace("-", "") for name in _font_names(reader))


def test_variables_are_html_escaped_and_do_not_create_active_pdf_content() -> None:
    variables = dict(VARIABLES, candidate_name='<script>alert("x")</script>&候选人')
    pdf = render_offer_pdf(TEMPLATE, variables)
    reader = PdfReader(BytesIO(pdf), strict=True)
    text = "".join(page.extract_text() or "" for page in reader.pages)
    root = reader.trailer["/Root"]

    assert '<script>alert("x")</script>&候选人' in text
    assert "/AcroForm" not in root
    assert "/OpenAction" not in root
    assert "/AA" not in root
    assert "/Annots" not in reader.pages[0]
    assert not root.get("/Names")


@pytest.mark.parametrize(
    "template",
    [
        TEMPLATE.replace("</body>", '<img src="https://example.test/logo.png"></body>'),
        TEMPLATE.replace("</body>", '<a href="file:///etc/passwd">secret</a></body>'),
        TEMPLATE.replace("</style>", "p { background: url(file:///etc/passwd); }</style>"),
        TEMPLATE.replace("</style>", "@import '../private/secret.css';</style>"),
        TEMPLATE.replace("</body>", "<script>alert(1)</script></body>"),
        TEMPLATE.replace("</body>", "<form><input name=x></form></body>"),
    ],
)
def test_external_file_traversal_and_active_template_content_are_rejected(template: str) -> None:
    with pytest.raises(OfferPdfError):
        render_offer_pdf(template, VARIABLES)


def test_placeholder_allowlist_and_required_values_are_strict() -> None:
    assert REQUIRED_PLACEHOLDERS == set(VARIABLES)
    with pytest.raises(OfferPdfError, match="required placeholders"):
        render_offer_pdf(TEMPLATE.replace("{{ hr_email }}", "HR"), {k: v for k, v in VARIABLES.items() if k != "hr_email"})
    with pytest.raises(OfferPdfError, match="unknown placeholder"):
        render_offer_pdf(TEMPLATE.replace("</body>", "{{ secret_path }}</body>"), dict(VARIABLES, secret_path="x"))
    with pytest.raises(OfferPdfError, match="exactly match"):
        render_offer_pdf(TEMPLATE, dict(VARIABLES, salary="100"))
    with pytest.raises(OfferPdfError, match="non-blank"):
        render_offer_pdf(TEMPLATE, dict(VARIABLES, candidate_name="  "))


def test_template_page_and_output_bounds_are_enforced(monkeypatch) -> None:
    pages = "".join(f'<section class="page">第 {index} 页</section>' for index in range(13))
    oversized_pages = TEMPLATE.replace("</style>", ".page { break-after: page; }</style>").replace("</body>", pages + "</body>")
    with pytest.raises(OfferPdfError, match="page count"):
        render_offer_pdf(oversized_pages, VARIABLES)

    monkeypatch.setattr("server.app.offers.pdf.MAX_PDF_BYTES", 100)
    with pytest.raises(OfferPdfError, match="size"):
        render_offer_pdf(TEMPLATE, VARIABLES)
    with pytest.raises(OfferPdfError, match="template size"):
        render_offer_pdf("x" * (MAX_TEMPLATE_BYTES + 1), VARIABLES)


class FakeStorageFailure(Exception):
    def __init__(self, code: str, status: int, message: str = "provider detail") -> None:
        super().__init__(message)
        self.code = code
        self.response = SimpleNamespace(status=status)


class AtomicFakeMinio:
    def __init__(self, concurrent_writers: int | None = None) -> None:
        self.objects: dict[tuple[str, str], tuple[bytes, dict[str, str], str]] = {}
        self.put_calls: list[tuple[str, str, bytes, dict[str, str]]] = []
        self.stat_count = 0
        self._barrier = Barrier(concurrent_writers) if concurrent_writers else None
        self._lock = Lock()

    def _put_object(self, bucket: str, key: str, content: bytes, headers: dict[str, str]):
        with self._lock:
            self.put_calls.append((bucket, key, content, dict(headers)))
        if self._barrier is not None:
            self._barrier.wait(timeout=10)
        with self._lock:
            if (bucket, key) in self.objects:
                raise FakeStorageFailure("PreconditionFailed", 412)
            metadata = {name: value for name, value in headers.items() if name.casefold().startswith("x-amz-meta-")}
            self.objects[(bucket, key)] = (content, metadata, headers["Content-Type"])
        return SimpleNamespace()

    def stat_object(self, bucket: str, key: str):
        with self._lock:
            self.stat_count += 1
            content, metadata, content_type = self.objects[(bucket, key)]
            return SimpleNamespace(size=len(content), metadata=dict(metadata), content_type=content_type)


class FailingMinio:
    def __init__(self, failure: Exception) -> None:
        self.failure = failure
        self.stat_count = 0

    def _put_object(self, bucket: str, key: str, content: bytes, headers: dict[str, str]):
        raise self.failure

    def stat_object(self, bucket: str, key: str):
        self.stat_count += 1
        raise RuntimeError("sensitive stat detail")


def _assert_atomic_headers(client: AtomicFakeMinio, digest_by_content: dict[bytes, str]) -> None:
    assert client.put_calls
    for bucket, _, content, headers in client.put_calls:
        assert bucket == "private-offers"
        assert headers == {
            "Content-Type": "application/pdf",
            "If-None-Match": "*",
            "X-Amz-Meta-Sha256": digest_by_content[content],
            "X-Amz-Meta-Immutable": "true",
        }


def test_minio_7_2_15_private_single_put_compatibility_contract() -> None:
    requirements = Path("server/requirements.txt").read_text(encoding="utf-8").splitlines()
    parameters = tuple(inspect.signature(Minio._put_object).parameters)

    assert "minio==7.2.15" in requirements
    assert installed_version("minio") == "7.2.15"
    assert parameters == ("self", "bucket_name", "object_name", "data", "headers", "query_params")
    assert 'self._execute(\n            "PUT"' in inspect.getsource(Minio._put_object)


def test_minio_offer_storage_is_private_immutable_and_idempotent() -> None:
    client = AtomicFakeMinio()
    storage = MinioOfferPdfStorage(client, "private-offers")
    content = b"private-pdf"
    digest = hashlib.sha256(content).hexdigest()
    key = "offers/tenant/offers/offer/versions/version.pdf"

    storage.write_immutable(key, content, digest)
    storage.write_immutable(key, content, digest)

    assert len(client.put_calls) == 2
    assert client.stat_count == 1
    _assert_atomic_headers(client, {content: digest})
    with pytest.raises(OfferPdfStorageConflict):
        storage.write_immutable(key, b"changed", hashlib.sha256(b"changed").hexdigest())


def test_public_pdf_read_requires_offer_scope_and_persisted_hash():
    content = b"private-pdf"
    key = "offers/tenant/offers/offer/versions/version.pdf"

    class Response:
        def read(self): return content
        def close(self): pass
        def release_conn(self): pass

    class Reader:
        def get_object(self, bucket, object_key):
            assert (bucket, object_key) == ("private-offers", key)
            return Response()

    storage = MinioOfferPdfStorage(Reader(), "private-offers")
    assert storage.read_verified(key, hashlib.sha256(content).hexdigest()) == content
    with pytest.raises(OfferPdfStorageError):
        storage.read_verified("exports/untrusted.pdf", hashlib.sha256(content).hexdigest())
    with pytest.raises(OfferPdfStorageError):
        storage.read_verified(key, "0" * 64)


def test_atomic_different_payload_writers_persist_one_and_conflict_the_loser() -> None:
    client = AtomicFakeMinio(concurrent_writers=2)
    storage = MinioOfferPdfStorage(client, "private-offers")
    key = "offers/tenant/offers/offer/versions/version.pdf"
    payloads = (b"first-pdf", b"second-pdf")
    digests = {payload: hashlib.sha256(payload).hexdigest() for payload in payloads}

    def write(payload: bytes):
        try:
            storage.write_immutable(key, payload, digests[payload])
            return "created"
        except OfferPdfStorageConflict:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(write, payloads))

    assert sorted(outcomes) == ["conflict", "created"]
    persisted, metadata, content_type = client.objects[("private-offers", key)]
    assert persisted in payloads
    assert metadata["X-Amz-Meta-Sha256"] == digests[persisted]
    assert content_type == "application/pdf"
    _assert_atomic_headers(client, digests)


def test_atomic_same_payload_writers_are_both_idempotent_successes() -> None:
    client = AtomicFakeMinio(concurrent_writers=2)
    storage = MinioOfferPdfStorage(client, "private-offers")
    key = "offers/tenant/offers/offer/versions/version.pdf"
    content = b"same-private-pdf"
    digest = hashlib.sha256(content).hexdigest()

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(lambda _: storage.write_immutable(key, content, digest), range(2)))

    assert outcomes == [None, None]
    assert client.objects[("private-offers", key)][0] == content
    assert client.stat_count == 1
    _assert_atomic_headers(client, {content: digest})


@pytest.mark.parametrize(
    "failure",
    [
        FakeStorageFailure("AccessDenied", 403, "sensitive access detail"),
        FakeStorageFailure("NoSuchBucket", 404, "sensitive bucket detail"),
        RuntimeError("sensitive transport detail"),
    ],
)
def test_non_precondition_storage_failures_are_safe_and_never_stat(failure: Exception) -> None:
    client = FailingMinio(failure)
    storage = MinioOfferPdfStorage(client, "private-offers")

    with pytest.raises(OfferPdfStorageError) as raised:
        storage.write_immutable("offers/tenant/version.pdf", b"pdf", hashlib.sha256(b"pdf").hexdigest())

    assert str(raised.value) == "private offer PDF storage is unavailable"
    assert "sensitive" not in str(raised.value)
    assert client.stat_count == 0


def test_precondition_collision_stat_failure_is_safe() -> None:
    client = FailingMinio(FakeStorageFailure("PreconditionFailed", 412))
    storage = MinioOfferPdfStorage(client, "private-offers")

    with pytest.raises(OfferPdfStorageError, match="private offer PDF storage is unavailable"):
        storage.write_immutable("offers/tenant/version.pdf", b"pdf", hashlib.sha256(b"pdf").hexdigest())

    assert client.stat_count == 1


class RecordingStorage:
    def __init__(self) -> None:
        self.writes: list[tuple[str, bytes, str]] = []

    def write_immutable(self, storage_key: str, content: bytes, sha256: str) -> None:
        self.writes.append((storage_key, content, sha256))


def test_offer_version_pdf_receipt_is_tenant_scoped_and_same_version_idempotent(monkeypatch) -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    organization_id, offer_id, version_id, actor_id = (UUID(int=index) for index in range(1, 5))
    original_content = {"salary": "100", "body": "immutable"}
    version = OfferVersion(
        id=version_id,
        organization_id=organization_id,
        offer_id=offer_id,
        version_number=1,
        content=original_content,
        candidate_response_deadline=datetime(2026, 8, 20, tzinfo=timezone.utc),
        is_special=False,
        special_reason=None,
        created_by=actor_id,
        submitted_at=datetime(2026, 8, 8, tzinfo=timezone.utc),
    )
    storage = RecordingStorage()
    pdf = b"%PDF-1.7 deterministic private offer"

    with Session(engine) as db:
        db.add(version)
        db.commit()
        monkeypatch.setattr("server.app.offers.service.render_offer_pdf", lambda *_: pdf)
        receipt = persist_offer_version_pdf(db, organization_id, version_id, TEMPLATE, VARIABLES, storage)
        db.commit()

        assert receipt.pdf_object_key == f"offers/{organization_id}/offers/{offer_id}/versions/{version_id}.pdf"
        assert receipt.pdf_sha256 == hashlib.sha256(pdf).hexdigest()
        assert receipt.pdf_size_bytes == len(pdf)
        assert receipt.pdf_rendered_at is not None
        assert receipt.content == original_content
        assert receipt.submitted_at is not None
        assert storage.writes == [(receipt.pdf_object_key, pdf, receipt.pdf_sha256)]

        monkeypatch.setattr("server.app.offers.service.render_offer_pdf", lambda *_: pytest.fail("idempotent receipt rendered again"))
        assert persist_offer_version_pdf(db, organization_id, version_id, "changed", {}, storage).id == version_id
        assert len(storage.writes) == 1
        with pytest.raises(OfferNotFound):
            persist_offer_version_pdf(db, UUID(int=99), version_id, TEMPLATE, VARIABLES, storage)


def test_migration_0034_adds_and_reverses_receipt_columns_and_preserves_snapshot_guard() -> None:
    migration = Path("server/migrations/versions/0034_offer_version_pdf_receipts.py").read_text(encoding="utf-8")
    for column in ("pdf_object_key", "pdf_sha256", "pdf_size_bytes", "pdf_rendered_at"):
        assert f'op.add_column("offer_versions", sa.Column("{column}"' in migration
        assert f'op.drop_column("offer_versions", "{column}")' in migration
    assert 'down_revision = "0033_offer_workflow"' in migration
    assert "OLD.pdf_object_key IS NULL" in migration
    assert "NEW.content IS NOT DISTINCT FROM OLD.content" in migration
    assert "submitted offer versions are immutable" in migration
    assert migration.count("CREATE TRIGGER offer_versions_immutable_after_submission") == 2
