from __future__ import annotations

import hashlib
import hmac
import io
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from minio.error import S3Error


LEDGER_FIELDS = frozenset(
    {
        "schema_version",
        "organization_id",
        "deletion_request_id",
        "candidate_id",
        "completed_at",
        "manifest_hash",
        "object_keys",
        "database_redaction_checksum",
    }
)
LEDGER_V2_FIELDS = frozenset(
    {
        "schema_version",
        "organization_id",
        "deletion_request_id",
        "candidate_id",
        "completed_request_version",
        "completed_at",
        "requested_at",
        "reason_code",
        "impact_manifest",
        "manifest_hash",
        "recovery_generation",
        "artifacts",
        "database_redaction_checksum",
    }
)
_REASON_CODES = frozenset(
    {"retention_expired", "candidate_request", "administrator_request"}
)
_ARTIFACT_KINDS = frozenset({"resume_object", "report_export_object"})
_ROW_ID_FIELDS = frozenset(
    {
        "contacts",
        "resumes",
        "applications",
        "screening_items",
        "screening_results",
        "interviews",
        "feedback",
        "feedback_revisions",
        "talent_memberships",
    }
)
_OBJECT_FIELDS = frozenset({"resume_objects", "temporary_exports"})
_COUNT_FIELDS = frozenset(
    {
        "contacts",
        "resumes",
        "applications",
        "screening_records",
        "interviews",
        "feedback_records",
        "talent_memberships",
        "resume_objects",
        "temporary_exports",
    }
)
_MANIFEST_FIELDS = frozenset(
    {
        "schema_version",
        "candidate_id",
        "candidate_version",
        "policy_version",
        "backup_window_ends_at",
        "row_ids",
        "objects",
        "counts",
    }
)
MAX_LEDGER_ARTIFACTS = 2_000
MAX_MANIFEST_ROWS_PER_KIND = 10_000


class GovernanceStorageError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _canonical(value: dict[str, Any]) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _utc(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("ledger completion time must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _sha256(value: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and value == value.lower()
        and all(char in "0123456789abcdef" for char in value)
    )


def _parse_utc(value: object) -> datetime:
    if not isinstance(value, str) or len(value) > 40 or not value.endswith("Z"):
        raise ValueError
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or _utc(parsed) != value:
        raise ValueError
    return parsed


def _uuid(value: object) -> UUID:
    if not isinstance(value, str) or len(value) != 36:
        raise ValueError
    parsed = UUID(value)
    if str(parsed) != value:
        raise ValueError
    return parsed


def _positive_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError
    return value


def _nonnegative_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError
    return value


def _validate_manifest(manifest: object, candidate_id: UUID, manifest_hash: str) -> dict[str, Any]:
    if not isinstance(manifest, dict) or set(manifest) != _MANIFEST_FIELDS:
        raise ValueError
    if manifest["schema_version"] != 1 or _uuid(manifest["candidate_id"]) != candidate_id:
        raise ValueError
    _positive_int(manifest["candidate_version"])
    _positive_int(manifest["policy_version"])
    _parse_utc(manifest["backup_window_ends_at"])
    row_ids = manifest["row_ids"]
    if not isinstance(row_ids, dict) or set(row_ids) != _ROW_ID_FIELDS:
        raise ValueError
    for values in row_ids.values():
        if not isinstance(values, list) or len(values) > MAX_MANIFEST_ROWS_PER_KIND:
            raise ValueError
        parsed = [str(_uuid(value)) for value in values]
        if parsed != sorted(set(parsed)):
            raise ValueError
    objects = manifest["objects"]
    if not isinstance(objects, dict) or set(objects) != _OBJECT_FIELDS:
        raise ValueError
    for values in objects.values():
        if not isinstance(values, list) or len(values) > MAX_LEDGER_ARTIFACTS:
            raise ValueError
        normalized = []
        for item in values:
            if not isinstance(item, dict) or set(item) != {"row_id", "storage_key"}:
                raise ValueError
            row_id = str(_uuid(item["row_id"]))
            key = item["storage_key"]
            if not isinstance(key, str) or not key or len(key) > 512:
                raise ValueError
            normalized.append((key, row_id))
        if normalized != sorted(set(normalized)):
            raise ValueError
        if len({key for key, _ in normalized}) != len(normalized):
            raise ValueError
    counts = manifest["counts"]
    if not isinstance(counts, dict) or set(counts) != _COUNT_FIELDS:
        raise ValueError
    if any(_nonnegative_int(value) > MAX_MANIFEST_ROWS_PER_KIND * 2 for value in counts.values()):
        raise ValueError
    expected_counts = {
        "contacts": len(row_ids["contacts"]),
        "resumes": len(row_ids["resumes"]),
        "applications": len(row_ids["applications"]),
        "screening_records": len(row_ids["screening_items"]) + len(row_ids["screening_results"]),
        "interviews": len(row_ids["interviews"]),
        "feedback_records": len(row_ids["feedback"]) + len(row_ids["feedback_revisions"]),
        "talent_memberships": len(row_ids["talent_memberships"]),
        "resume_objects": len(objects["resume_objects"]),
        "temporary_exports": len(objects["temporary_exports"]),
    }
    if counts != expected_counts or not hmac.compare_digest(_sha256(manifest), manifest_hash):
        raise ValueError
    return manifest


@dataclass(frozen=True)
class LedgerEntry:
    organization_id: UUID
    deletion_request_id: UUID
    candidate_id: UUID
    completed_at: datetime
    manifest_hash: str
    object_keys: tuple[str, ...]
    database_redaction_checksum: str
    schema_version: int = 1

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ValueError("ledger schema version is unsupported")
        if any(
            len(value) != 64
            or value.lower() != value
            or any(char not in "0123456789abcdef" for char in value)
            for value in (self.manifest_hash, self.database_redaction_checksum)
        ):
            raise ValueError("ledger checksums must be lowercase sha256")
        normalized = tuple(sorted(set(self.object_keys)))
        if len(normalized) != len(self.object_keys) or any(not key for key in normalized):
            raise ValueError("ledger object keys must be unique and non-empty")
        object.__setattr__(self, "object_keys", normalized)
        _utc(self.completed_at)

    def unsigned_document(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "organization_id": str(self.organization_id),
            "deletion_request_id": str(self.deletion_request_id),
            "candidate_id": str(self.candidate_id),
            "completed_at": _utc(self.completed_at),
            "manifest_hash": self.manifest_hash,
            "object_keys": list(self.object_keys),
            "database_redaction_checksum": self.database_redaction_checksum,
        }

    def signed_document(self, signing_key: bytes) -> dict[str, Any]:
        unsigned = self.unsigned_document()
        return {
            **unsigned,
            "signature": hmac.new(signing_key, _canonical(unsigned), hashlib.sha256).hexdigest(),
        }

    @classmethod
    def verify_document(cls, document: dict[str, Any], signing_key: bytes) -> "LedgerEntry":
        try:
            if not isinstance(document, dict) or set(document) != LEDGER_FIELDS | {"signature"}:
                raise ValueError
            signature = document["signature"]
            if not isinstance(signature, str) or len(signature) != 64:
                raise ValueError
            unsigned = {key: document[key] for key in LEDGER_FIELDS}
            expected = hmac.new(signing_key, _canonical(unsigned), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(signature, expected):
                raise GovernanceStorageError("ledger_signature_invalid")
            if document["schema_version"] != 1 or not isinstance(document["object_keys"], list):
                raise ValueError
            completed_at = datetime.fromisoformat(document["completed_at"].replace("Z", "+00:00"))
            return cls(
                organization_id=UUID(document["organization_id"]),
                deletion_request_id=UUID(document["deletion_request_id"]),
                candidate_id=UUID(document["candidate_id"]),
                completed_at=completed_at,
                manifest_hash=document["manifest_hash"],
                object_keys=tuple(document["object_keys"]),
                database_redaction_checksum=document["database_redaction_checksum"],
            )
        except GovernanceStorageError:
            raise
        except (KeyError, TypeError, ValueError, AttributeError):
            raise GovernanceStorageError("ledger_invalid") from None

    @staticmethod
    def verify_recovery_document(
        document: dict[str, Any], signing_key: bytes, *, allowed_buckets: set[str],
        allowed_locations: dict[str, tuple[str, str]] | None = None,
    ) -> "LedgerEntryV2":
        if isinstance(document, dict) and document.get("schema_version") == 1:
            LedgerEntry.verify_document(document, signing_key)
            raise GovernanceStorageError("recovery_ledger_unsupported")
        return LedgerEntryV2.verify_document(
            document,
            signing_key,
            allowed_buckets=allowed_buckets,
            allowed_locations=allowed_locations,
        )


@dataclass(frozen=True, order=True)
class LedgerArtifact:
    kind: str
    bucket: str
    storage_key: str

    def __post_init__(self) -> None:
        if self.kind not in _ARTIFACT_KINDS:
            raise ValueError("ledger artifact kind is unsupported")
        if not self.bucket or len(self.bucket) > 63:
            raise ValueError("ledger artifact bucket is invalid")
        if not self.storage_key or len(self.storage_key) > 512:
            raise ValueError("ledger artifact key is invalid")

    def document(self) -> dict[str, str]:
        return {"kind": self.kind, "bucket": self.bucket, "storage_key": self.storage_key}


@dataclass(frozen=True)
class LedgerEntryV2:
    organization_id: UUID
    deletion_request_id: UUID
    candidate_id: UUID
    completed_request_version: int
    completed_at: datetime
    requested_at: datetime
    reason_code: str
    impact_manifest: dict[str, Any]
    manifest_hash: str
    recovery_generation: int
    artifacts: tuple[LedgerArtifact, ...]
    database_redaction_checksum: str
    schema_version: int = 2

    def __post_init__(self) -> None:
        if self.schema_version != 2:
            raise ValueError("ledger schema version is unsupported")
        _positive_int(self.completed_request_version)
        if self.reason_code not in _REASON_CODES:
            raise ValueError("ledger reason code is unsupported")
        _nonnegative_int(self.recovery_generation)
        if not _is_sha256(self.manifest_hash) or not _is_sha256(self.database_redaction_checksum):
            raise ValueError("ledger checksums must be lowercase sha256")
        _utc(self.completed_at)
        _utc(self.requested_at)
        _validate_manifest(self.impact_manifest, self.candidate_id, self.manifest_hash)
        if len(self.artifacts) > MAX_LEDGER_ARTIFACTS:
            raise ValueError("ledger artifact collection is outside bounds")
        if tuple(sorted(set(self.artifacts))) != self.artifacts:
            raise ValueError("ledger artifacts must be unique and canonical")
        if len({(item.kind, item.storage_key) for item in self.artifacts}) != len(self.artifacts):
            raise ValueError("ledger artifacts must identify unique deletion checkpoints")
        manifest_keys = {
            "resume_object": {
                item["storage_key"] for item in self.impact_manifest["objects"]["resume_objects"]
            },
            "report_export_object": {
                item["storage_key"] for item in self.impact_manifest["objects"]["temporary_exports"]
            },
        }
        artifact_keys = {
            kind: {artifact.storage_key for artifact in self.artifacts if artifact.kind == kind}
            for kind in _ARTIFACT_KINDS
        }
        if artifact_keys != manifest_keys:
            raise ValueError("ledger artifacts do not match manifest")

    def unsigned_document(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "organization_id": str(self.organization_id),
            "deletion_request_id": str(self.deletion_request_id),
            "candidate_id": str(self.candidate_id),
            "completed_request_version": self.completed_request_version,
            "completed_at": _utc(self.completed_at),
            "requested_at": _utc(self.requested_at),
            "reason_code": self.reason_code,
            "impact_manifest": self.impact_manifest,
            "manifest_hash": self.manifest_hash,
            "recovery_generation": self.recovery_generation,
            "artifacts": [artifact.document() for artifact in self.artifacts],
            "database_redaction_checksum": self.database_redaction_checksum,
        }

    def signed_document(self, signing_key: bytes) -> dict[str, Any]:
        unsigned = self.unsigned_document()
        return {
            **unsigned,
            "signature": hmac.new(signing_key, _canonical(unsigned), hashlib.sha256).hexdigest(),
        }

    @classmethod
    def verify_document(
        cls, document: dict[str, Any], signing_key: bytes, *, allowed_buckets: set[str],
        allowed_locations: dict[str, tuple[str, str]] | None = None,
    ) -> "LedgerEntryV2":
        try:
            if not isinstance(document, dict) or set(document) != LEDGER_V2_FIELDS | {"signature"}:
                raise ValueError
            signature = document["signature"]
            if not _is_sha256(signature):
                raise ValueError
            unsigned = {key: document[key] for key in LEDGER_V2_FIELDS}
            expected = hmac.new(signing_key, _canonical(unsigned), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(signature, expected):
                raise GovernanceStorageError("ledger_signature_invalid")
            if document["schema_version"] != 2 or not isinstance(document["artifacts"], list):
                raise ValueError
            artifacts = tuple(
                LedgerArtifact(
                    kind=item["kind"], bucket=item["bucket"], storage_key=item["storage_key"]
                )
                for item in document["artifacts"]
                if isinstance(item, dict) and set(item) == {"kind", "bucket", "storage_key"}
            )
            if len(artifacts) != len(document["artifacts"]):
                raise ValueError
            if any(artifact.bucket not in allowed_buckets for artifact in artifacts):
                raise ValueError
            if allowed_locations is not None and any(
                allowed_locations.get(artifact.kind) is None
                or artifact.bucket != allowed_locations[artifact.kind][0]
                or not artifact.storage_key.startswith(
                    allowed_locations[artifact.kind][1]
                )
                for artifact in artifacts
            ):
                raise ValueError
            return cls(
                organization_id=_uuid(document["organization_id"]),
                deletion_request_id=_uuid(document["deletion_request_id"]),
                candidate_id=_uuid(document["candidate_id"]),
                completed_request_version=_positive_int(document["completed_request_version"]),
                completed_at=_parse_utc(document["completed_at"]),
                requested_at=_parse_utc(document["requested_at"]),
                reason_code=document["reason_code"],
                impact_manifest=document["impact_manifest"],
                manifest_hash=document["manifest_hash"],
                recovery_generation=_nonnegative_int(document["recovery_generation"]),
                artifacts=artifacts,
                database_redaction_checksum=document["database_redaction_checksum"],
            )
        except GovernanceStorageError:
            raise
        except (KeyError, TypeError, ValueError, AttributeError):
            raise GovernanceStorageError("ledger_invalid") from None


@dataclass(frozen=True)
class LedgerReceipt:
    object_key: str
    sha256: str


def _read_object(response) -> bytes:
    try:
        return response.read()
    finally:
        close = getattr(response, "close", None)
        if close is not None:
            close()
        release = getattr(response, "release_conn", None)
        if release is not None:
            release()


class SignedLedgerAdapter:
    def __init__(
        self,
        client,
        bucket: str,
        prefix: str,
        signing_key: bytes,
        *,
        allowed_buckets: set[str] | None = None,
        allowed_locations: dict[str, tuple[str, str]] | None = None,
    ) -> None:
        self.client = client
        self.bucket = bucket
        self.prefix = prefix.rstrip("/") + "/"
        self.signing_key = bytes(signing_key)
        self.allowed_buckets = frozenset(allowed_buckets or ())
        self.allowed_locations = dict(allowed_locations) if allowed_locations else None

    def object_key(self, entry: LedgerEntry | LedgerEntryV2) -> str:
        return (
            f"{self.prefix}v{entry.schema_version}/{entry.organization_id}/"
            f"{entry.deletion_request_id}.json"
        )

    def validate_entry(self, entry: LedgerEntry | LedgerEntryV2) -> None:
        document = entry.signed_document(self.signing_key)
        if isinstance(entry, LedgerEntry):
            if LedgerEntry.verify_document(document, self.signing_key) != entry:
                raise GovernanceStorageError("ledger_invalid")
            return
        if LedgerEntryV2.verify_document(
            document,
            self.signing_key,
            allowed_buckets=set(self.allowed_buckets),
            allowed_locations=self.allowed_locations,
        ) != entry:
            raise GovernanceStorageError("ledger_invalid")

    def _encoded(self, entry: LedgerEntry | LedgerEntryV2) -> bytes:
        return _canonical(entry.signed_document(self.signing_key))

    def _put_if_absent(self, object_key: str, encoded: bytes) -> None:
        conditional = getattr(self.client, "_execute", None)
        if conditional is None:
            raise GovernanceStorageError("ledger_conditional_create_unsupported")
        response = conditional(
            "PUT",
            self.bucket,
            object_key,
            body=encoded,
            headers={
                "Content-Type": "application/json",
                "If-None-Match": "*",
            },
        )
        close = getattr(response, "close", None)
        if close is not None:
            close()
        release = getattr(response, "release_conn", None)
        if release is not None:
            release()

    def _read_raw(self, object_key: str) -> bytes | None:
        try:
            return _read_object(self.client.get_object(self.bucket, object_key))
        except S3Error as error:
            if error.code in {"NoSuchKey", "NoSuchObject"}:
                return None
            raise GovernanceStorageError("ledger_read_failed") from None
        except Exception:
            raise GovernanceStorageError("ledger_read_failed") from None

    def read(self, object_key: str) -> LedgerEntry | LedgerEntryV2 | None:
        raw = self._read_raw(object_key)
        if raw is None:
            return None
        try:
            document = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise GovernanceStorageError("ledger_invalid") from None
        if isinstance(document, dict) and document.get("schema_version") == 1:
            return LedgerEntry.verify_document(document, self.signing_key)
        return LedgerEntryV2.verify_document(
            document,
            self.signing_key,
            allowed_buckets=set(self.allowed_buckets),
            allowed_locations=self.allowed_locations,
        )

    def discover_recovery_ledgers(
        self, restored_at: datetime, *, maximum: int
    ) -> tuple[object, ...]:
        if maximum < 1:
            raise GovernanceStorageError("recovery_ledger_limit_exceeded")
        if restored_at.tzinfo is None:
            raise GovernanceStorageError("recovery_timestamp_invalid")
        restored_at = restored_at.astimezone(timezone.utc)
        discovered: list[tuple[str, bytes]] = []
        try:
            for item in self.client.list_objects(
                self.bucket, prefix=self.prefix, recursive=True
            ):
                object_key = getattr(item, "object_name", None)
                if (
                    not isinstance(object_key, str)
                    or not object_key.startswith(self.prefix)
                    or len(object_key) > 512
                ):
                    raise GovernanceStorageError("recovery_ledger_invalid")
                raw = self._read_raw(object_key)
                if raw is None:
                    raise GovernanceStorageError("recovery_ledger_invalid")
                discovered.append((object_key, raw))
                if len(discovered) > maximum:
                    raise GovernanceStorageError("recovery_ledger_limit_exceeded")
        except GovernanceStorageError:
            raise
        except Exception:
            raise GovernanceStorageError("recovery_ledger_discovery_failed") from None

        prepared = []
        for object_key, raw in discovered:
            try:
                document = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                raise GovernanceStorageError("recovery_ledger_invalid") from None
            if isinstance(document, dict) and document.get("schema_version") == 1:
                entry_v1 = LedgerEntry.verify_document(document, self.signing_key)
                if self.object_key(entry_v1) != object_key:
                    raise GovernanceStorageError("recovery_ledger_invalid")
                if entry_v1.completed_at.astimezone(timezone.utc) > restored_at:
                    raise GovernanceStorageError("recovery_ledger_unsupported")
                continue
            entry = LedgerEntryV2.verify_document(
                document,
                self.signing_key,
                allowed_buckets=set(self.allowed_buckets),
                allowed_locations=self.allowed_locations,
            )
            if self.object_key(entry) != object_key:
                raise GovernanceStorageError("recovery_ledger_invalid")
            if entry.completed_at.astimezone(timezone.utc) > restored_at:
                from server.app.governance.recovery import PreparedLedger

                prepared.append(
                    PreparedLedger(object_key, hashlib.sha256(raw).hexdigest(), entry)
                )
        return tuple(prepared)

    def read_recovery(self, object_key: str, expected_sha256: str) -> LedgerEntryV2:
        raw = self._read_raw(object_key)
        if raw is None or not hmac.compare_digest(
            hashlib.sha256(raw).hexdigest(), expected_sha256
        ):
            raise GovernanceStorageError("recovery_ledger_changed")
        try:
            document = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise GovernanceStorageError("recovery_ledger_invalid") from None
        entry = LedgerEntry.verify_recovery_document(
            document,
            self.signing_key,
            allowed_buckets=set(self.allowed_buckets),
            allowed_locations=self.allowed_locations,
        )
        if self.object_key(entry) != object_key:
            raise GovernanceStorageError("recovery_ledger_invalid")
        return entry

    def write(self, entry: LedgerEntry | LedgerEntryV2) -> LedgerReceipt:
        object_key = self.object_key(entry)
        encoded = self._encoded(entry)
        try:
            existing = self.read(object_key)
        except GovernanceStorageError as error:
            if error.code in {"ledger_invalid", "ledger_signature_invalid"}:
                raise GovernanceStorageError("ledger_existing_mismatch") from None
            raise
        if existing is not None:
            if existing != entry:
                raise GovernanceStorageError("ledger_existing_mismatch")
        else:
            try:
                self._put_if_absent(object_key, encoded)
            except S3Error as error:
                if error.code not in {"PreconditionFailed", "ConditionalRequestConflict"}:
                    raise GovernanceStorageError("ledger_write_failed") from None
                try:
                    if self.read(object_key) != entry:
                        raise GovernanceStorageError("ledger_existing_mismatch")
                except GovernanceStorageError:
                    raise GovernanceStorageError("ledger_existing_mismatch") from None
            except GovernanceStorageError:
                raise
            except Exception:
                raise GovernanceStorageError("ledger_write_failed") from None
            try:
                if self.read(object_key) != entry:
                    raise GovernanceStorageError("ledger_existing_mismatch")
            except GovernanceStorageError as error:
                if error.code in {
                    "ledger_invalid",
                    "ledger_signature_invalid",
                    "ledger_existing_mismatch",
                }:
                    raise GovernanceStorageError("ledger_existing_mismatch") from None
                raise
        return LedgerReceipt(object_key, hashlib.sha256(encoded).hexdigest())


class DeleteOnlyObjectAdapter:
    def __init__(self, client) -> None:
        self.client = client

    def delete(self, bucket: str, object_key: str) -> None:
        if not bucket or not object_key:
            raise GovernanceStorageError("object_reference_invalid")
        try:
            self.client.remove_object(bucket, object_key)
        except Exception:
            raise GovernanceStorageError("object_delete_failed") from None
