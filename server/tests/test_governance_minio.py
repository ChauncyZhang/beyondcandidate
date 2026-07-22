from __future__ import annotations

import io
import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from threading import Barrier
from uuid import uuid4

import pytest
from minio import Minio
from minio.error import S3Error


SIGNING_KEY = b"ledger-signing-key-with-independent-entropy"


def ledger_entry():
    from server.app.governance.storage import LedgerEntry

    return LedgerEntry(
        organization_id=uuid4(),
        deletion_request_id=uuid4(),
        candidate_id=uuid4(),
        completed_at=datetime(2026, 7, 15, 8, 30, tzinfo=timezone.utc),
        manifest_hash="a" * 64,
        object_keys=("resumes/clean/a.pdf", "exports/temporary/b.csv"),
        database_redaction_checksum="b" * 64,
    )


def test_ledger_v1_is_canonical_and_independently_hmac_verifiable() -> None:
    import hashlib
    import hmac

    entry = ledger_entry()
    document = entry.signed_document(SIGNING_KEY)
    signature = document.pop("signature")
    independent = json.dumps(
        document, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")

    assert document["schema_version"] == 1
    assert hmac.compare_digest(
        signature, hmac.new(SIGNING_KEY, independent, hashlib.sha256).hexdigest()
    )
    assert entry.signed_document(SIGNING_KEY)["signature"] == signature


def test_ledger_verification_rejects_malformed_tampered_and_unknown_schema() -> None:
    from server.app.governance.storage import GovernanceStorageError, LedgerEntry

    entry = ledger_entry()
    document = entry.signed_document(SIGNING_KEY)
    for changed in (
        {**document, "candidate_id": str(uuid4())},
        {**document, "signature": "0" * 64},
        {**document, "schema_version": 2},
        {key: value for key, value in document.items() if key != "manifest_hash"},
        {**document, "unexpected": "value"},
    ):
        with pytest.raises(GovernanceStorageError) as error:
            LedgerEntry.verify_document(changed, SIGNING_KEY)
        assert error.value.code in {"ledger_invalid", "ledger_signature_invalid"}
        assert str(entry.candidate_id) not in str(error.value)


def test_ledger_document_recursively_contains_only_allowlisted_non_pii_fields() -> None:
    entry = ledger_entry()
    document = entry.signed_document(SIGNING_KEY)
    prohibited_fragments = {
        "name",
        "email",
        "phone",
        "note",
        "text",
        "filename",
        "url",
        "provider",
        "credential",
        "password",
        "secret",
    }

    def inspect(value):
        if isinstance(value, dict):
            for key, nested in value.items():
                assert not any(fragment in key.lower() for fragment in prohibited_fragments)
                inspect(nested)
        elif isinstance(value, list):
            for nested in value:
                inspect(nested)

    inspect(document)
    serialized = json.dumps(document, sort_keys=True)
    assert SIGNING_KEY.decode() not in serialized


class MemoryMinio:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}

    def get_object(self, bucket: str, key: str):
        if (bucket, key) not in self.objects:
            raise S3Error("NoSuchKey", "missing", key, "request", "host", None)
        return io.BytesIO(self.objects[(bucket, key)])

    def put_object(self, bucket: str, key: str, body, length: int, **_):
        self.objects[(bucket, key)] = body.read(length)

    def remove_object(self, bucket: str, key: str):
        self.objects.pop((bucket, key), None)


class RacingMemoryMinio(MemoryMinio):
    def _execute(self, method: str, bucket_name: str, object_name: str, **_):
        assert method == "PUT"
        self.objects[(bucket_name, object_name)] = b'{"schema_version":1}'
        raise S3Error(
            "PreconditionFailed", "already exists", object_name,
            "request", "host", None,
        )


class ConditionalMemoryMinio(MemoryMinio):
    def _execute(self, method: str, bucket_name: str, object_name: str, **kwargs):
        assert method == "PUT"
        assert kwargs["headers"]["If-None-Match"] == "*"
        key = (bucket_name, object_name)
        if key in self.objects:
            raise S3Error(
                "PreconditionFailed", "already exists", object_name,
                "request", "host", None,
            )
        self.objects[key] = kwargs["body"]
        return io.BytesIO()


def test_ledger_write_is_idempotent_and_fails_closed_on_existing_mismatch() -> None:
    from server.app.governance.storage import GovernanceStorageError, SignedLedgerAdapter

    client = ConditionalMemoryMinio()
    entry = ledger_entry()
    adapter = SignedLedgerAdapter(client, "ledger", "deletions/", SIGNING_KEY)

    first = adapter.write(entry)
    second = adapter.write(entry)
    assert first == second

    client.objects[("ledger", first.object_key)] = b'{"schema_version":1}'
    with pytest.raises(GovernanceStorageError) as error:
        adapter.write(entry)
    assert error.value.code == "ledger_existing_mismatch"


def test_ledger_conditional_write_never_overwrites_a_concurrent_object() -> None:
    from server.app.governance.storage import GovernanceStorageError, SignedLedgerAdapter

    client = RacingMemoryMinio()
    adapter = SignedLedgerAdapter(client, "ledger", "deletions/", SIGNING_KEY)
    entry = ledger_entry()

    with pytest.raises(GovernanceStorageError) as error:
        adapter.write(entry)
    assert error.value.code == "ledger_existing_mismatch"
    assert client.objects[("ledger", adapter.object_key(entry))] == b'{"schema_version":1}'


def test_ledger_write_fails_closed_when_client_cannot_conditionally_create() -> None:
    from server.app.governance.storage import GovernanceStorageError, SignedLedgerAdapter

    client = MemoryMinio()
    adapter = SignedLedgerAdapter(client, "ledger", "deletions/", SIGNING_KEY)

    with pytest.raises(GovernanceStorageError) as error:
        adapter.write(ledger_entry())

    assert error.value.code == "ledger_conditional_create_unsupported"
    assert client.objects == {}


def test_delete_only_adapter_treats_missing_object_as_success() -> None:
    from server.app.governance.storage import DeleteOnlyObjectAdapter

    client = MemoryMinio()
    client.objects[("resumes", "clean/a.pdf")] = b"private"
    adapter = DeleteOnlyObjectAdapter(client)

    adapter.delete("resumes", "clean/a.pdf")
    adapter.delete("resumes", "clean/a.pdf")
    assert client.objects == {}


pytestmark_minio = pytest.mark.skipif(
    not os.getenv("GOVERNANCE_MINIO_ENDPOINT"), reason="governance MinIO smoke not configured"
)


@pytestmark_minio
def test_real_minio_separate_credentials_enforce_cross_policy_denials() -> None:
    from server.app.governance.storage import (
        DeleteOnlyObjectAdapter,
        GovernanceStorageError,
        SignedLedgerAdapter,
    )

    endpoint = os.environ["GOVERNANCE_MINIO_ENDPOINT"]
    root = Minio(
        endpoint,
        access_key=os.environ["MINIO_SMOKE_ROOT_ACCESS_KEY"],
        secret_key=os.environ["MINIO_SMOKE_ROOT_SECRET_KEY"],
        secure=False,
    )
    delete_client = Minio(
        endpoint,
        access_key=os.environ["GOVERNANCE_DELETE_ACCESS_KEY"],
        secret_key=os.environ["GOVERNANCE_DELETE_SECRET_KEY"],
        secure=False,
    )
    ledger_client = Minio(
        endpoint,
        access_key=os.environ["GOVERNANCE_LEDGER_ACCESS_KEY"],
        secret_key=os.environ["GOVERNANCE_LEDGER_SECRET_KEY"],
        secure=False,
    )
    resume_bucket = os.environ["GOVERNANCE_RESUME_BUCKET"]
    export_bucket = os.environ["GOVERNANCE_EXPORT_BUCKET"]
    ledger_bucket = os.environ["GOVERNANCE_LEDGER_BUCKET"]
    resume_key = f"clean/{uuid4()}.pdf"
    export_key = f"exports/{uuid4()}.csv"
    forbidden_resume_key = f"quarantine/{uuid4()}.pdf"
    for bucket, key in (
        (resume_bucket, resume_key),
        (export_bucket, export_key),
        (resume_bucket, forbidden_resume_key),
    ):
        root.put_object(bucket, key, io.BytesIO(b"private"), 7)

    for bucket, key in ((resume_bucket, resume_key), (export_bucket, export_key)):
        with pytest.raises(S3Error, match="AccessDenied"):
            delete_client.get_object(bucket, key)

    deleter = DeleteOnlyObjectAdapter(delete_client)
    deleter.delete(resume_bucket, resume_key)
    deleter.delete(resume_bucket, resume_key)
    deleter.delete(export_bucket, export_key)
    with pytest.raises(S3Error, match="AccessDenied"):
        delete_client.get_object(resume_bucket, forbidden_resume_key)
    with pytest.raises(S3Error, match="AccessDenied"):
        delete_client.put_object(resume_bucket, resume_key, io.BytesIO(b"x"), 1)
    with pytest.raises(GovernanceStorageError) as denied_delete:
        deleter.delete(resume_bucket, forbidden_resume_key)
    assert denied_delete.value.code == "object_delete_failed"
    assert forbidden_resume_key not in str(denied_delete.value)

    ledger = SignedLedgerAdapter(ledger_client, ledger_bucket, "deletions/", SIGNING_KEY)
    receipt = ledger.write(ledger_entry())
    assert ledger.read(receipt.object_key) is not None
    assert list(ledger_client.list_objects(ledger_bucket, prefix="deletions/"))
    with pytest.raises(S3Error, match="AccessDenied"):
        ledger_client.remove_object(ledger_bucket, receipt.object_key)
    with pytest.raises(S3Error, match="AccessDenied"):
        ledger_client.get_object(resume_bucket, forbidden_resume_key)


@pytestmark_minio
def test_real_minio_conditional_ledger_create_is_race_safe() -> None:
    from server.app.governance.storage import SignedLedgerAdapter

    endpoint = os.environ["GOVERNANCE_MINIO_ENDPOINT"]
    ledger_bucket = os.environ["GOVERNANCE_LEDGER_BUCKET"]
    barrier = Barrier(2)

    class BarrierMinio(Minio):
        def _execute(self, method, bucket_name=None, object_name=None, **kwargs):
            if method == "PUT" and kwargs.get("headers", {}).get("If-None-Match") == "*":
                barrier.wait(timeout=10)
            return super()._execute(method, bucket_name, object_name, **kwargs)

    def adapter() -> SignedLedgerAdapter:
        client = BarrierMinio(
            endpoint,
            access_key=os.environ["GOVERNANCE_LEDGER_ACCESS_KEY"],
            secret_key=os.environ["GOVERNANCE_LEDGER_SECRET_KEY"],
            secure=False,
        )
        return SignedLedgerAdapter(client, ledger_bucket, "deletions/", SIGNING_KEY)

    entry = ledger_entry()
    first = adapter()
    second = adapter()
    with ThreadPoolExecutor(max_workers=2) as executor:
        receipts = list(executor.map(lambda item: item.write(entry), (first, second)))

    assert receipts[0] == receipts[1]
    root = Minio(
        endpoint,
        access_key=os.environ["MINIO_SMOKE_ROOT_ACCESS_KEY"],
        secret_key=os.environ["MINIO_SMOKE_ROOT_SECRET_KEY"],
        secure=False,
    )
    try:
        assert root.stat_object(ledger_bucket, receipts[0].object_key).size > 0
    finally:
        root.remove_object(ledger_bucket, receipts[0].object_key)


@pytest.mark.skipif(
    not os.getenv("PREVIOUS_GOVERNANCE_DELETE_ACCESS_KEY"),
    reason="rotated MinIO credentials not configured",
)
def test_real_minio_rotation_revokes_previous_governance_access_keys() -> None:
    endpoint = os.environ["GOVERNANCE_MINIO_ENDPOINT"]
    checks = (
        (
            os.environ["PREVIOUS_GOVERNANCE_DELETE_ACCESS_KEY"],
            os.environ["PREVIOUS_GOVERNANCE_DELETE_SECRET_KEY"],
            os.environ["GOVERNANCE_RESUME_BUCKET"],
            "clean/rotation-check",
        ),
        (
            os.environ["PREVIOUS_GOVERNANCE_LEDGER_ACCESS_KEY"],
            os.environ["PREVIOUS_GOVERNANCE_LEDGER_SECRET_KEY"],
            os.environ["GOVERNANCE_LEDGER_BUCKET"],
            "deletions/rotation-check.json",
        ),
    )
    for access_key, secret_key, bucket, object_key in checks:
        retired = Minio(
            endpoint,
            access_key=access_key,
            secret_key=secret_key,
            secure=False,
        )
        with pytest.raises(S3Error) as error:
            retired.stat_object(bucket, object_key)
        assert error.value.code in {"InvalidAccessKeyId", "AccessDenied"}
