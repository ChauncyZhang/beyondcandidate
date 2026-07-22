#!/usr/bin/env python3
"""Fail-closed Phase 6C backup and recovery contract helpers.

This module deliberately owns orchestration contracts, not B2B3 recovery logic.
Secrets are consumed only through mounted files and are never added to argv,
logs, manifests, or evidence documents.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
from pathlib import PurePosixPath
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
from typing import Any, Callable, Iterable, Mapping, Sequence
from urllib.parse import urlparse
from uuid import UUID, uuid4


UTC_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
RUN_ID_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{3,126}[A-Za-z0-9])$")
BUCKET_RE = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
MINIO_ALIAS_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
BACKUP_IMAGE_RE = re.compile(
    r"^(?=.{1,255}$)(?:[a-z0-9]+(?:[._-][a-z0-9]+)*(?::[0-9]+)?/)*"
    r"[a-z0-9]+(?:[._-][a-z0-9]+)*(?::[A-Za-z0-9_][A-Za-z0-9_.-]{0,127})?$"
)
BACKUP_IMAGE_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
SAFE_ERROR_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{2,63}$")
ALLOWED_REMOTE_SCHEMES = {"s3", "s3+https", "azure", "gcs"}
CHILD_BASE_ENV_ALLOWLIST = {
    "PATH", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "TEMP", "TMP", "HOME",
    "USERPROFILE", "SYSTEMROOT", "WINDIR",
}
PRIVATE_SECRET_ENV_ALLOWLIST = {"PGPASSFILE"}
SENSITIVE_ENV_NAME_RE = re.compile(
    r"(?:^AWS_|PASSWORD|SECRET|TOKEN|KEY|CREDENTIAL|RCLONE_CONFIG_PASS|PGPASSWORD)",
    re.IGNORECASE,
)
REFERENCE_VALIDATOR_ID = "ux09-reference-validator-v1"
REFERENCE_QUERY = """SELECT storage_key FROM file_objects WHERE storage_state <> 'deleted'
UNION
SELECT object_key FROM report_exports WHERE object_key IS NOT NULL AND status = 'succeeded'
ORDER BY 1"""
REFERENCE_QUERY_FINGERPRINT = hashlib.sha256(REFERENCE_QUERY.encode("utf-8")).hexdigest()
FORBIDDEN_MANIFEST_KEYS = {
    "access_key",
    "candidate_id",
    "content",
    "credential",
    "filename",
    "object_key",
    "password",
    "request_id",
    "secret",
}
LOCAL_DESTINATION_SCHEMES = {"", "file", "local"}
LOCAL_DESTINATION_HOSTS = {"localhost", "127.0.0.1", "::1", "postgres", "minio", "app", "ux09"}
DEFAULT_FORBIDDEN_DATA_PATHS = (
    "/var/lib/postgresql/data",
    "/var/lib/minio/data",
    "/data/postgres",
    "/data/minio",
)


class SecurityContractError(ValueError):
    """A safety contract intentionally blocks the requested state transition."""


def validate_run_id(value: str) -> str:
    if not isinstance(value, str) or not RUN_ID_RE.fullmatch(value):
        raise ValueError("run or generation fragment is invalid")
    return value


def _is_reparse_point(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(metadata.st_mode):
        return True
    is_junction = getattr(os.path, "isjunction", None)
    if is_junction is not None and is_junction(path):
        return True
    attributes = getattr(metadata, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return bool(attributes & reparse_flag)


def safe_run_path(root: Path, fragment: str, *, prefix: str = "") -> Path:
    validate_run_id(fragment)
    root_configured = Path(os.path.abspath(root))
    root_resolved = root.resolve(strict=False)
    if _is_reparse_point(root) or root_configured != root_resolved:
        raise ValueError("configured root cannot be a symlink, junction, or reparse point")
    if not root_resolved.is_dir():
        raise ValueError("configured root must be an existing directory")
    candidate = (root_resolved / f"{prefix}{fragment}").resolve(strict=False)
    try:
        common = os.path.commonpath(
            [os.path.normcase(str(root_resolved)), os.path.normcase(str(candidate))]
        )
    except ValueError as error:
        raise ValueError("joined run path escapes its root") from error
    if common != os.path.normcase(str(root_resolved)):
        raise ValueError("joined run path escapes its root")
    unresolved = root_resolved / f"{prefix}{fragment}"
    if _is_reparse_point(unresolved) or (unresolved.exists() and candidate != unresolved.absolute()):
        raise ValueError("joined run path resolves through a symlink, junction, or reparse point")
    return candidate


def validate_business_buckets(raw: str) -> list[str]:
    if not isinstance(raw, str) or not raw:
        raise ValueError("business bucket list is required")
    buckets = raw.split(",")
    if any(not bucket or not BUCKET_RE.fullmatch(bucket) for bucket in buckets):
        raise ValueError("business bucket name is invalid")
    if len(set(buckets)) != len(buckets):
        raise ValueError("business bucket names must be unique")
    return buckets


def validate_minio_alias(value: str) -> str:
    if not isinstance(value, str) or not MINIO_ALIAS_RE.fullmatch(value):
        raise ValueError("MinIO alias must be a non-sensitive configured alias name")
    return value


def validate_backup_image_reference(image: str, digest: str) -> str:
    if not isinstance(image, str) or not BACKUP_IMAGE_RE.fullmatch(image) or "@" in image:
        raise ValueError("backup image repository/tag reference is invalid")
    if not isinstance(digest, str) or not BACKUP_IMAGE_DIGEST_RE.fullmatch(digest):
        raise ValueError("backup image digest must be sha256 followed by 64 lowercase hex characters")
    return f"{image}@{digest}"


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_write_bytes(path: Path, payload: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.tmp-", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            descriptor = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def _atomic_write_json(path: Path, value: Mapping[str, Any]) -> None:
    _atomic_write_bytes(path, _canonical_json(value))


def _parse_utc(value: str) -> datetime:
    try:
        parsed = datetime.strptime(value, UTC_FORMAT).replace(tzinfo=timezone.utc)
    except (TypeError, ValueError) as error:
        raise ValueError(f"invalid UTC timestamp: {value!r}") from error
    return parsed


def _require_mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    return value


def _require_sha256(value: Any, label: str) -> None:
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        raise ValueError(f"{label} must be a lowercase sha256")


def _scan_manifest(value: Any, path: tuple[str, ...] = ()) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            normalized = str(key).lower()
            if normalized in FORBIDDEN_MANIFEST_KEYS:
                raise ValueError(f"forbidden sensitive manifest field: {'.'.join((*path, normalized))}")
            _scan_manifest(child, (*path, normalized))
        return
    if isinstance(value, list):
        for index, child in enumerate(value):
            _scan_manifest(child, (*path, str(index)))
        return
    if isinstance(value, str):
        lowered = value.lower()
        if "@" in value or "-----begin" in lowered or re.search(r"\b(candidate|request)[-_]?[0-9a-f]{6,}\b", lowered):
            raise ValueError(f"forbidden sensitive or PII value in manifest at {'.'.join(path)}")


def validate_backup_manifest(manifest: Mapping[str, Any]) -> None:
    """Validate the public, non-PII COMPLETE restore-point manifest."""
    root = _require_mapping(manifest, "manifest")
    _scan_manifest(root)
    required = {
        "schema_version",
        "backup_run_id",
        "state",
        "backup_cutoff_utc",
        "schedule_interval_hours",
        "toolchain",
        "database",
        "business_snapshot",
        "reference_validation",
        "ledger_archive",
        "retention",
        "gates",
    }
    missing = required.difference(root)
    if missing:
        raise ValueError(f"manifest missing required fields: {sorted(missing)}")
    if set(root) != required:
        raise ValueError("manifest contains fields outside the non-PII schema allowlist")
    if root["schema_version"] != 1 or root["state"] != "complete":
        raise ValueError("manifest is not a complete schema version 1 restore point")
    validate_run_id(root["backup_run_id"])
    _parse_utc(root["backup_cutoff_utc"])
    if root["schedule_interval_hours"] != 12:
        raise ValueError("backup schedule must be exactly 12 hours")

    toolchain = _require_mapping(root["toolchain"], "toolchain")
    if set(toolchain) != {"image", "image_digest", "postgres", "minio_client", "destination_client"}:
        raise ValueError("toolchain contains fields outside the schema allowlist")
    _require_sha256(str(toolchain.get("image_digest", "")).removeprefix("sha256:"), "toolchain.image_digest")
    for field in ("image", "postgres", "minio_client", "destination_client"):
        if not toolchain.get(field):
            raise ValueError(f"toolchain.{field} is required")

    database = _require_mapping(root["database"], "database")
    if set(database) != {"format", "sha256", "size_bytes", "restore_list_entries"}:
        raise ValueError("database evidence contains fields outside the schema allowlist")
    if database.get("format") != "custom" or int(database.get("size_bytes", 0)) <= 0:
        raise ValueError("database must be a non-empty custom-format dump")
    if int(database.get("restore_list_entries", 0)) <= 0:
        raise ValueError("pg_restore --list evidence is empty")
    _require_sha256(database.get("sha256"), "database.sha256")

    business = _require_mapping(root["business_snapshot"], "business_snapshot")
    if set(business) != {"sha256", "size_bytes", "object_count", "inventory_sha256"}:
        raise ValueError("business evidence contains fields outside the schema allowlist")
    if int(business.get("size_bytes", 0)) <= 0 or int(business.get("object_count", -1)) < 0:
        raise ValueError("business snapshot size/object count is invalid")
    _require_sha256(business.get("sha256"), "business_snapshot.sha256")
    _require_sha256(business.get("inventory_sha256"), "business_snapshot.inventory_sha256")

    references = _require_mapping(root["reference_validation"], "reference_validation")
    if set(references) != {
        "schema_version", "validator_id", "query_fingerprint", "inventory_sha256",
        "expected", "checked", "mismatches",
    }:
        raise ValueError("reference evidence contains fields outside the schema allowlist")
    validate_reference_proof(
        references,
        expected=references.get("expected"),
        inventory_sha256=str(business["inventory_sha256"]),
    )

    ledger = _require_mapping(root["ledger_archive"], "ledger_archive")
    if set(ledger) != {"archive_run_id", "cutoff_utc", "manifest_sha256", "signing_key_versions"}:
        raise ValueError("ledger evidence contains fields outside the schema allowlist")
    validate_run_id(str(ledger.get("archive_run_id", "")))
    _parse_utc(str(ledger.get("cutoff_utc", "")))
    _require_sha256(ledger.get("manifest_sha256"), "ledger_archive.manifest_sha256")
    versions = ledger.get("signing_key_versions")
    if not isinstance(versions, list) or not versions or not all(isinstance(item, str) and item for item in versions):
        raise ValueError("ledger signing-key versions/history is required")

    retention = _require_mapping(root["retention"], "retention")
    if set(retention) != {"backup_window_days", "policy_version"}:
        raise ValueError("retention evidence contains fields outside the schema allowlist")
    if int(retention.get("backup_window_days", 0)) <= 0 or not retention.get("policy_version"):
        raise ValueError("retention policy evidence is invalid")
    gates = _require_mapping(root["gates"], "gates")
    expected_gates = {"pg_restore_list", "hashes", "object_inventory", "references"}
    if set(gates) != expected_gates or not all(gates.values()):
        raise ValueError("all backup validation gates must pass")


def _canonical_json(value: Mapping[str, Any]) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n").encode("utf-8")


def atomic_publish_local(
    staging: Path,
    published: Path,
    manifest: Mapping[str, Any],
    final_validator: Callable[[], None],
) -> None:
    """Reference implementation for atomic directory publication in tests/drills."""
    if not staging.is_dir():
        raise ValueError("staging must exist")
    final_validator()
    validate_backup_manifest(manifest)
    payload = _canonical_json(manifest)
    manifest_tmp = staging / ".manifest.json.tmp"
    complete_tmp = staging / ".COMPLETE.tmp"
    _atomic_write_bytes(manifest_tmp, payload)
    _atomic_write_bytes(complete_tmp, (hashlib.sha256(payload).hexdigest() + "\n").encode("ascii"))
    os.replace(manifest_tmp, staging / "manifest.json")
    os.replace(complete_tmp, staging / "COMPLETE")
    published.parent.mkdir(parents=True, exist_ok=True)
    lease_directory = published.parent / ".leases"
    lease_directory.mkdir(mode=0o700, exist_ok=True)
    validate_run_id(str(manifest["backup_run_id"]))
    lease = safe_run_path(lease_directory, str(manifest["backup_run_id"]), prefix="lease-")
    descriptor = os.open(lease, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        os.write(descriptor, (str(manifest["backup_run_id"]) + "\n").encode("ascii"))
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    _fsync_directory(lease_directory)
    if published.exists():
        raise FileExistsError("atomic publication destination already exists")
    os.rename(staging, published)
    _fsync_directory(published.parent)


def validate_off_host_destination(destination: str, application_host: str, forbidden_paths: Iterable[str]) -> str:
    parsed = urlparse(destination)
    if parsed.scheme.lower() not in ALLOWED_REMOTE_SCHEMES or not parsed.netloc:
        raise ValueError("backup destination scheme is not approved for off-host storage")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("backup destination userinfo is forbidden")
    if parsed.query or parsed.fragment:
        raise ValueError("backup destination query and fragment are forbidden")
    if "%" in parsed.path or "\\" in parsed.path:
        raise ValueError("backup destination path encoding is ambiguous or unsafe")
    host = (parsed.hostname or "").lower().rstrip(".")
    app_host = application_host.lower().rstrip(".")
    if host in LOCAL_DESTINATION_HOSTS or (app_host and host == app_host):
        raise ValueError("backup destination resolves to the application host")
    normalized_path = parsed.path.replace("\\", "/").lower().rstrip("/")
    for forbidden in (*DEFAULT_FORBIDDEN_DATA_PATHS, *forbidden_paths):
        candidate = str(forbidden).replace("\\", "/").lower().rstrip("/")
        if candidate and (normalized_path == candidate or normalized_path.startswith(candidate + "/") or candidate in normalized_path):
            raise ValueError(f"backup destination uses forbidden data-volume path: {forbidden}")
    return destination


def validate_secret_files(paths: Sequence[Path]) -> None:
    if not paths:
        raise ValueError("secret files are required")
    identities: set[tuple[int, int]] = set()
    for path in paths:
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            raise ValueError("secret file symlinks are forbidden")
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("secret path must be a regular file")
        if metadata.st_nlink != 1:
            raise ValueError("secret file hardlinks or shared inodes are forbidden")
        if os.name != "nt" and stat.S_IMODE(metadata.st_mode) & 0o077:
            raise ValueError("secret file permissions must be 0600 or stricter")
        if metadata.st_size <= 0:
            raise ValueError("secret file is empty")
        identity = (metadata.st_dev, metadata.st_ino)
        if identity in identities:
            raise ValueError("all secret files must use distinct inodes")
        identities.add(identity)


@contextmanager
def secure_secret_copies(paths: Sequence[Path], private_root: Path):
    validate_secret_files(paths)
    private_root.mkdir(mode=0o700, parents=True, exist_ok=False)
    copies: list[Path] = []
    opened: list[int] = []
    try:
        for index, path in enumerate(paths):
            before = path.lstat()
            flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(path, flags)
            opened.append(descriptor)
            after = os.fstat(descriptor)
            if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino) or not stat.S_ISREG(after.st_mode):
                raise ValueError("secret file changed during secure open")
            target = private_root / f"secret-{index}"
            output = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0), 0o600)
            try:
                while True:
                    chunk = os.read(descriptor, 1024 * 1024)
                    if not chunk:
                        break
                    os.write(output, chunk)
                os.fsync(output)
            finally:
                os.close(output)
            target.chmod(0o600)
            copies.append(target)
        _fsync_directory(private_root)
        yield copies
    finally:
        for descriptor in opened:
            os.close(descriptor)
        shutil.rmtree(private_root, ignore_errors=True)


def validate_reference_proof(proof: Mapping[str, Any], *, expected: int, inventory_sha256: str) -> None:
    fields = {
        "schema_version", "validator_id", "query_fingerprint", "inventory_sha256",
        "expected", "checked", "mismatches",
    }
    if set(proof) != fields or proof.get("schema_version") != 1:
        raise ValueError("reference proof schema is invalid")
    if proof.get("validator_id") != REFERENCE_VALIDATOR_ID:
        raise ValueError("reference validator identity is not pinned")
    if proof.get("query_fingerprint") != REFERENCE_QUERY_FINGERPRINT:
        raise ValueError("reference query fingerprint is invalid")
    if proof.get("inventory_sha256") != inventory_sha256:
        raise ValueError("reference inventory fingerprint is invalid")
    if not isinstance(expected, int) or expected < 0:
        raise ValueError("trusted reference expected count is invalid")
    if proof.get("expected") != expected or proof.get("checked") != expected:
        raise ValueError("reference checked count must equal trusted expected count")
    if proof.get("mismatches") != 0:
        raise ValueError("reference validation found mismatches")


def plan_prune(catalog: Sequence[Mapping[str, Any]], retention_days: int, now: datetime) -> list[str]:
    if retention_days <= 0 or now.tzinfo is None:
        raise ValueError("retention policy and timezone-aware current time are required")
    if not catalog:
        raise ValueError("cannot prune an empty backup catalog")
    complete_orders = [item.get("complete_order") for item in catalog if item.get("complete") is True]
    if any(not isinstance(value, int) or value < 1 for value in complete_orders) or len(set(complete_orders)) != len(complete_orders):
        raise ValueError("remote COMPLETE order is missing, invalid, or duplicated")
    ordered = sorted(catalog, key=lambda item: int(item.get("complete_order", 0)))
    latest = max((item for item in ordered if item.get("complete") is True), key=lambda item: int(item["complete_order"]))
    if latest.get("complete") is not True or latest.get("valid") is not True:
        raise ValueError("latest backup is incomplete or invalid; prune fails closed")
    complete = [item for item in ordered if item.get("complete") is True]
    if any(item.get("backup_window_days") != retention_days for item in complete):
        raise ValueError("retention policy mismatch; prune fails closed")
    valid = [item for item in complete if item.get("valid") is True]
    if len(valid) < 2:
        raise ValueError("at least two valid restore points must remain")
    protected = {str(item["backup_run_id"]) for item in valid[-2:]}
    cutoff = now - timedelta(days=retention_days)
    deletions = []
    for item in valid:
        run_id = str(item.get("backup_run_id", ""))
        validate_run_id(run_id)
        if run_id not in protected and _parse_utc(str(item["backup_cutoff_utc"])) < cutoff:
            deletions.append(run_id)
    return deletions


def validate_ledger_boundary(
    *,
    business_buckets: Sequence[str],
    ledger_bucket: str,
    business_capabilities: set[str],
    business_destination: str,
    ledger_destination: str,
    key_history: Mapping[str, Any],
) -> None:
    if ledger_bucket in business_buckets:
        raise ValueError("live ledger bucket must be excluded from business snapshots")
    if {"ledger:restore", "ledger:delete"}.intersection(business_capabilities):
        raise ValueError("business identities cannot restore or delete ledger archives")
    if business_destination.rstrip("/") == ledger_destination.rstrip("/"):
        raise ValueError("ledger archive requires an independent destination/failure domain")
    if key_history.get("schema_version") != 1:
        raise ValueError("ledger signing-key history schema is invalid")
    versions = key_history.get("versions")
    active = key_history.get("active_key_version")
    if not isinstance(versions, list) or len(versions) < 2:
        raise ValueError("versioned ledger signing-key history is required")
    version_names = [item.get("version") for item in versions if isinstance(item, Mapping)]
    active_items = [item for item in versions if isinstance(item, Mapping) and item.get("status") == "active"]
    if active not in version_names or len(active_items) != 1 or active_items[0].get("version") != active:
        raise ValueError("ledger active signing-key version/history mismatch")
    _scan_manifest(key_history)


def validate_ledger_archive_group(
    group: Path,
    signing_key: Path,
    key_history: Mapping[str, Any],
    *,
    minimum_cutoff_utc: str,
) -> Mapping[str, Any]:
    validate_run_id(group.name)
    group_configured = Path(os.path.abspath(group))
    group_resolved = group.resolve(strict=False)
    if group_configured != group_resolved or _is_reparse_point(group) or not group.is_dir():
        raise ValueError("ledger archive group must be a non-reparse directory")
    validate_ledger_boundary(
        business_buckets=[],
        ledger_bucket="ledger",
        business_capabilities=set(),
        business_destination="s3://business.invalid/archive",
        ledger_destination="s3://ledger.invalid/archive",
        key_history=key_history,
    )
    expected_fields = {
        "schema_version", "archive_run_id", "cutoff_utc", "archive_sha256",
        "size_bytes", "entry_count", "signing_key_version", "lifecycle_policy_version",
    }
    manifest_path = group / "ledger-manifest.json"
    signature_path = group / "ledger-manifest.sig"
    complete_path = group / "COMPLETE"
    archive_path = group / "ledger.snapshot"
    for path in (manifest_path, signature_path, complete_path, archive_path):
        if _is_reparse_point(path):
            raise ValueError("ledger archive group files cannot be symlinks or reparse points")
    manifest = _require_mapping(_load_json(manifest_path), "ledger archive manifest")
    if set(manifest) != expected_fields or manifest.get("schema_version") != 1:
        raise ValueError("ledger archive manifest schema is invalid")
    if manifest.get("archive_run_id") != group.name:
        raise ValueError("ledger archive run binding is invalid")
    validate_run_id(str(manifest["archive_run_id"]))
    if manifest.get("signing_key_version") != key_history.get("active_key_version"):
        raise ValueError("ledger signing key version is not active")
    if manifest_path.read_bytes() != _canonical_json(manifest):
        raise ValueError("ledger archive manifest is not canonical")
    verify_hmac_signature(manifest_path, signing_key, signature_path)
    complete = complete_path.read_text(encoding="ascii").strip()
    if complete != hashlib.sha256(manifest_path.read_bytes()).hexdigest():
        raise ValueError("ledger COMPLETE marker does not match manifest")
    _require_sha256(manifest.get("archive_sha256"), "ledger archive hash")
    if not archive_path.is_file() or archive_path.stat().st_size != manifest.get("size_bytes"):
        raise ValueError("ledger archive size does not match manifest")
    if _sha256_file(archive_path) != manifest.get("archive_sha256"):
        raise ValueError("ledger archive hash does not match manifest")
    if not isinstance(manifest.get("entry_count"), int) or manifest["entry_count"] < 0:
        raise ValueError("ledger archive entry count is invalid")
    cutoff = _parse_utc(str(manifest.get("cutoff_utc", "")))
    if cutoff < _parse_utc(minimum_cutoff_utc):
        raise ValueError("ledger archive is not fresh enough for recovery")
    return manifest


def validate_ledger_pairing(
    group: Path,
    verify_key: Path,
    key_history: Mapping[str, Any],
    *,
    business_run_id: str,
    business_cutoff_utc: str,
) -> Mapping[str, Any]:
    validate_run_id(business_run_id)
    _parse_utc(business_cutoff_utc)
    ledger = validate_ledger_archive_group(
        group,
        verify_key,
        key_history,
        minimum_cutoff_utc=business_cutoff_utc,
    )
    versions = [
        item.get("version")
        for item in key_history.get("versions", [])
        if isinstance(item, Mapping)
    ]
    evidence = {
        "archive_run_id": ledger["archive_run_id"],
        "cutoff_utc": ledger["cutoff_utc"],
        "manifest_sha256": _sha256_file(group / "ledger-manifest.json"),
        "signing_key_versions": versions,
    }
    _scan_manifest(evidence)
    return evidence


def validate_ledger_restore_proof(
    proof_path: Path,
    signature_path: Path,
    signing_key: Path,
    archive_manifest: Mapping[str, Any],
    business_run_id: str,
    generation_id: str,
) -> None:
    validate_run_id(business_run_id)
    validate_run_id(generation_id)
    verify_hmac_signature(proof_path, signing_key, signature_path)
    proof = _require_mapping(_load_json(proof_path), "ledger restore proof")
    fields = {
        "schema_version", "status", "ledger_archive_run_id", "business_backup_run_id",
        "recovery_generation_id", "archive_sha256", "cutoff_utc", "restored_entry_count",
    }
    if set(proof) != fields or proof.get("schema_version") != 1 or proof.get("status") != "verified":
        raise ValueError("ledger restore proof schema is invalid")
    expected = {
        "ledger_archive_run_id": archive_manifest.get("archive_run_id"),
        "business_backup_run_id": business_run_id,
        "recovery_generation_id": generation_id,
        "archive_sha256": archive_manifest.get("archive_sha256"),
        "cutoff_utc": archive_manifest.get("cutoff_utc"),
        "restored_entry_count": archive_manifest.get("entry_count"),
    }
    if any(proof.get(key) != value for key, value in expected.items()):
        raise ValueError("ledger restore proof run/generation/archive binding is invalid")


def validate_disposable_recovery(project: str, volumes: Sequence[str], confirmed: str) -> None:
    if confirmed != "1":
        raise ValueError("disposable recovery requires DISPOSABLE_RECOVERY_CONFIRMED=1")
    if not re.fullmatch(r"ux09-backup-drill-[a-z0-9][a-z0-9-]{2,48}", project):
        raise ValueError("recovery project is not a disposable backup-drill project")
    if not volumes or any(not volume.startswith(project + "-") for volume in volumes):
        raise ValueError("recovery volume is not isolated to the disposable project")
    if any(re.search(r"(^|[-_])(prod|production)([-_]|$)", volume, re.IGNORECASE) for volume in volumes):
        raise ValueError("production volumes are forbidden")


def validate_drill_preflight(
    *,
    project: str,
    volumes: Sequence[str],
    confirmed: str,
    image: str,
    digest: str,
    catalog: Sequence[Mapping[str, Any]],
    retention_days: int,
    now: datetime,
) -> Mapping[str, Any]:
    validate_disposable_recovery(project, volumes, confirmed)
    immutable_image = validate_backup_image_reference(image, digest)
    plan_prune(catalog, retention_days, now)
    latest = max(
        (item for item in catalog if item.get("complete") is True),
        key=lambda item: int(item["complete_order"]),
    )
    if latest.get("valid") is not True:
        raise ValueError("latest backup is invalid; drill preflight fails closed")
    return {
        "schema_version": 1,
        "immutable_image": immutable_image,
        "latest_backup_run_id": validate_run_id(str(latest.get("backup_run_id", ""))),
        "traffic_open": False,
    }


def validate_released_b2b3_commands(cli_path: Path, worker_path: Path) -> None:
    for label, path in (("B2B3 CLI", cli_path), ("B2B3 Worker", worker_path)):
        if not path.is_absolute():
            raise ValueError(f"released {label} path must be absolute")
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise ValueError(f"released {label} must be a regular non-symlink file")
        if metadata.st_nlink != 1:
            raise ValueError(f"released {label} hardlinks or shared inodes are forbidden")
        if not metadata.st_mode & 0o111:
            raise ValueError(f"released {label} must be executable")


def _restore_binding(restore: Mapping[str, Any]) -> dict[str, str]:
    expected = {
        "backup_run_id",
        "recovery_generation_id",
        "restore_id",
        "backup_manifest_sha256",
        "ledger_manifest_sha256",
    }
    if (
        restore.get("schema_version") != 1
        or restore.get("state") != "restored_traffic_closed"
        or restore.get("traffic_open") is not False
        or not expected.issubset(restore)
    ):
        raise ValueError("restore evidence is not a completed traffic-closed restore")
    binding = {key: str(restore[key]) for key in expected}
    validate_run_id(binding["backup_run_id"])
    validate_run_id(binding["recovery_generation_id"])
    try:
        UUID(binding["restore_id"])
    except ValueError:
        raise ValueError("restore evidence restore id is invalid") from None
    _require_sha256(binding["backup_manifest_sha256"], "backup manifest hash")
    _require_sha256(binding["ledger_manifest_sha256"], "ledger manifest hash")
    return binding


def validate_b2b3_evidence(
    restore_path: Path,
    evidence_path: Path,
    signature_path: Path,
    verify_key_path: Path,
) -> Mapping[str, Any]:
    validate_secret_files([verify_key_path])
    verify_public_key_signature(evidence_path, verify_key_path, signature_path)
    restore = _require_mapping(_load_json(restore_path), "restore evidence")
    evidence = _require_mapping(_load_json(evidence_path), "B2B3 evidence")
    binding = _restore_binding(restore)
    expected_fields = {
        "schema_version",
        "status",
        *binding,
        "traffic_open",
        "checks",
        "counts",
    }
    if set(evidence) != expected_fields or evidence.get("schema_version") != 1:
        raise ValueError("B2B3 evidence schema is invalid")
    if evidence.get("status") != "complete" or evidence.get("traffic_open") is not False:
        raise ValueError("B2B3 evidence is not complete and traffic-closed")
    if any(evidence.get(key) != value for key, value in binding.items()):
        raise ValueError("B2B3 evidence run/generation/manifest binding is invalid")
    checks = _require_mapping(evidence.get("checks"), "B2B3 checks")
    expected_checks = {
        "objects_absent",
        "database_redacted",
        "ledger_consistent",
        "recovery_completed",
    }
    if set(checks) != expected_checks or any(checks[key] is not True for key in expected_checks):
        raise ValueError("B2B3 re-delete checks are incomplete")
    counts = _require_mapping(evidence.get("counts"), "B2B3 counts")
    if set(counts) != {"prepared_redeletions", "completed_redeletions", "deleted_objects"}:
        raise ValueError("B2B3 evidence counts schema is invalid")
    values = tuple(counts[key] for key in ("prepared_redeletions", "completed_redeletions", "deleted_objects"))
    if any(isinstance(value, bool) or not isinstance(value, int) or value <= 0 for value in values):
        raise ValueError("B2B3 evidence counts must be positive integers")
    if counts["prepared_redeletions"] != counts["completed_redeletions"]:
        raise ValueError("B2B3 recovery completion count is inconsistent")
    return evidence


def require_traffic_open_evidence(
    restore: Mapping[str, Any], b2b3: Mapping[str, Any] | None
) -> bool:
    if b2b3 is None:
        raise SecurityContractError("verified B2B3 evidence is unavailable")
    binding = _restore_binding(restore)
    if any(b2b3.get(key) != value for key, value in binding.items()):
        raise SecurityContractError("verified B2B3 evidence binding is unavailable")
    if b2b3.get("status") != "complete" or b2b3.get("traffic_open") is not False:
        raise SecurityContractError("verified B2B3 evidence is unavailable")
    return False


def write_drill_failure_evidence(
    output_path: Path, restore: Mapping[str, Any], safe_error_code: str
) -> Mapping[str, Any]:
    if not SAFE_ERROR_CODE_RE.fullmatch(safe_error_code):
        raise ValueError("drill failure code is unsafe")
    binding = _restore_binding(restore)
    evidence = {
        "schema_version": 1,
        "state": "b2b3_failed_traffic_closed",
        **binding,
        "safe_error_code": safe_error_code,
        "traffic_open": False,
    }
    _atomic_write_json(output_path, evidence)
    return evidence


def _traffic_closed_evidence(restore: Mapping[str, Any]) -> Mapping[str, Any]:
    return {
        "schema_version": 1,
        "state": "b2b3_verified_traffic_closed",
        **_restore_binding(restore),
        "traffic_open": False,
        "production_traffic_decision": "external_environment_required",
    }


def begin_closed_recovery(
    evidence_path: Path,
    closed_marker_path: Path,
    open_marker_path: Path,
    backup_run_id: str,
    generation_id: str,
) -> Mapping[str, Any]:
    validate_run_id(backup_run_id)
    validate_run_id(generation_id)
    if open_marker_path.is_symlink():
        raise ValueError("traffic-open marker path cannot be a symlink")
    open_marker_path.unlink(missing_ok=True)
    state = {
        "schema_version": 1,
        "state": "restore_started_traffic_closed",
        "backup_run_id": backup_run_id,
        "recovery_generation_id": generation_id,
        "traffic_open": False,
    }
    _atomic_write_json(evidence_path, state)
    _atomic_write_json(closed_marker_path, state)
    return state


def safe_extract_business_snapshot(archive_path: Path, destination: Path, approved_buckets: set[str]) -> None:
    if not approved_buckets or any(not BUCKET_RE.fullmatch(bucket) for bucket in approved_buckets):
        raise ValueError("approved business bucket set is invalid")
    if destination.exists():
        raise ValueError("tar extraction destination must not already exist")
    with tarfile.open(archive_path, "r:*") as archive:
        members = archive.getmembers()
        for member in members:
            path = PurePosixPath(member.name)
            if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
                raise ValueError("tar member path is absolute or traverses")
            if not path.parts or path.parts[0] != "objects":
                raise ValueError("tar member has unexpected top-level path")
            if len(path.parts) == 1:
                if not member.isdir():
                    raise ValueError("tar objects root must be a directory")
                continue
            if path.parts[1] not in approved_buckets:
                raise ValueError("tar member bucket is not approved")
            if len(path.parts) == 2 and not member.isdir():
                raise ValueError("tar bucket root must be a directory")
            if not (member.isdir() or member.isreg()):
                raise ValueError("tar member type is forbidden")
        destination.mkdir(mode=0o700, parents=True)
        for member in members:
            path = PurePosixPath(member.name)
            target = destination.joinpath(*path.parts)
            target_resolved = target.resolve(strict=False)
            if Path(os.path.commonpath([str(destination.resolve()), str(target_resolved)])) != destination.resolve():
                raise ValueError("tar member path escapes destination")
            if member.isdir():
                target.mkdir(mode=0o700, parents=True, exist_ok=True)
                if target.is_symlink():
                    raise ValueError("tar directory resolved through symlink")
                continue
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            source = archive.extractfile(member)
            if source is None:
                raise ValueError("tar regular member has no payload")
            descriptor = os.open(
                target,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            try:
                while True:
                    chunk = source.read(1024 * 1024)
                    if not chunk:
                        break
                    os.write(descriptor, chunk)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)


def build_drill_evidence(
    *,
    backup_cutoff_utc: str,
    failure_at_utc: str,
    recovery_started_utc: str,
    recovery_finished_utc: str,
    b2b3_complete: bool,
    smoke_complete: bool,
) -> dict[str, Any]:
    backup_cutoff = _parse_utc(backup_cutoff_utc)
    failure_at = _parse_utc(failure_at_utc)
    started = _parse_utc(recovery_started_utc)
    finished = _parse_utc(recovery_finished_utc)
    rpo_hours = (failure_at - backup_cutoff).total_seconds() / 3600
    rto_hours = (finished - started).total_seconds() / 3600
    gates = {
        "rpo_24h": 0 <= rpo_hours <= 24,
        "rto_4h": 0 <= rto_hours <= 4,
        "b2b3": b2b3_complete,
        "read_only_smoke": smoke_complete,
    }
    if not all(gates.values()):
        raise ValueError("RPO/RTO, real B2B3, and read-only smoke gates must all pass")
    return {
        "schema_version": 1,
        "rpo_hours": round(rpo_hours, 6),
        "rto_hours": round(rto_hours, 6),
        "gates": gates,
        "traffic_open": False,
    }


def _load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    _atomic_write_json(path, value)


def _require_env(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise ValueError(f"{name} is required")
    return value


def _secret_paths(*names: str) -> list[Path]:
    return [Path(_require_env(name)) for name in names]


def _private_secret_root(root: Path, purpose: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    root_configured = Path(os.path.abspath(root))
    root_resolved = root.resolve()
    if root_configured != root_resolved or _is_reparse_point(root):
        raise ValueError("private secret root cannot be a symlink, junction, or reparse point")
    candidate = (root_resolved / f".{purpose}-secrets-{os.getpid()}-{secrets.token_hex(8)}").resolve(strict=False)
    if Path(os.path.commonpath([str(root_resolved), str(candidate)])) != root_resolved:
        raise ValueError("private secret path escapes its root")
    return candidate


def _sanitized_child_environment(
    *,
    runtime_values: Mapping[str, str] | None = None,
    private_secret_paths: Mapping[str, str] | None = None,
) -> dict[str, str]:
    environment = {
        name: os.environ[name]
        for name in CHILD_BASE_ENV_ALLOWLIST
        if os.environ.get(name)
    }
    for name, value in (runtime_values or {}).items():
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", name) or SENSITIVE_ENV_NAME_RE.search(name):
            raise ValueError("child runtime environment name is not explicitly non-sensitive")
        if not isinstance(value, str) or not value or "\x00" in value:
            raise ValueError("child runtime environment value is invalid")
        environment[name] = value
    for name, value in (private_secret_paths or {}).items():
        if name not in PRIVATE_SECRET_ENV_ALLOWLIST:
            raise ValueError("child secret environment name is not allowlisted")
        if not isinstance(value, str) or not value or "\x00" in value:
            raise ValueError("child private secret path is invalid")
        environment[name] = value
    return environment


def _run(
    command: Sequence[str],
    *,
    capture: bool = False,
    environment: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(command),
        check=True,
        text=True,
        stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        env=dict(environment) if environment is not None else None,
    )


def _client(
    env_name: str,
    *arguments: str,
    capture: bool = False,
    environment: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    executable = _require_env(env_name)
    if any(character.isspace() for character in executable):
        raise ValueError(f"{env_name} must be one executable path, not a shell command")
    return _run([executable, *arguments], capture=capture, environment=environment)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_hmac_signature(payload_path: Path, key_path: Path, output_path: Path) -> None:
    key = key_path.read_bytes()
    if len(key) < 32:
        raise ValueError("manifest signing key must contain at least 32 bytes")
    signature = hmac.new(key, payload_path.read_bytes(), hashlib.sha256).hexdigest()
    _atomic_write_bytes(output_path, (signature + "\n").encode("ascii"))


def verify_hmac_signature(payload_path: Path, key_path: Path, signature_path: Path) -> None:
    key = key_path.read_bytes()
    if len(key) < 32:
        raise ValueError("manifest signing key must contain at least 32 bytes")
    supplied = signature_path.read_text(encoding="ascii").strip()
    if not SHA256_RE.fullmatch(supplied):
        raise ValueError("manifest signature encoding is invalid")
    expected = hmac.new(key, payload_path.read_bytes(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(supplied, expected):
        raise ValueError("manifest signature verification failed")


def verify_public_key_signature(
    payload_path: Path, public_key_path: Path, signature_path: Path
) -> None:
    try:
        completed = subprocess.run(
            [
                "openssl",
                "pkeyutl",
                "-verify",
                "-rawin",
                "-pubin",
                "-inkey",
                str(public_key_path),
                "-sigfile",
                str(signature_path),
                "-in",
                str(payload_path),
            ],
            check=False,
            text=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=_sanitized_child_environment(),
        )
    except OSError:
        raise ValueError("B2B3 public-key signature verifier is unavailable") from None
    if completed.returncode != 0:
        raise ValueError("B2B3 public-key signature verification failed")


def _validate_business_inventory(path: Path) -> tuple[int, str]:
    inventory = _load_json(path)
    if not isinstance(inventory, Mapping) or inventory.get("schema_version") != 1:
        raise ValueError("business inventory schema is invalid")
    objects = inventory.get("objects")
    if not isinstance(objects, list):
        raise ValueError("business inventory must enumerate objects")
    for item in objects:
        if not isinstance(item, Mapping) or set(item) != {"key_hash", "sha256", "size_bytes"}:
            raise ValueError("business inventory entries must contain only key hash, content hash, and size")
        _require_sha256(item["key_hash"], "inventory.key_hash")
        _require_sha256(item["sha256"], "inventory.sha256")
        if int(item["size_bytes"]) < 0:
            raise ValueError("inventory size is invalid")
    return len(objects), _sha256_file(path)


def build_reference_proof(database_keys: Sequence[str], inventory: Mapping[str, Any]) -> Mapping[str, Any]:
    objects = inventory.get("objects") if isinstance(inventory, Mapping) else None
    if not isinstance(objects, list):
        raise ValueError("business inventory must enumerate objects")
    inventory_hashes = {str(item.get("key_hash", "")) for item in objects if isinstance(item, Mapping)}
    if len(inventory_hashes) != len(objects) or any(not SHA256_RE.fullmatch(value) for value in inventory_hashes):
        raise ValueError("business inventory key hashes are invalid or duplicated")
    normalized_keys = sorted(set(database_keys))
    if len(normalized_keys) != len(database_keys) or any(not key for key in normalized_keys):
        raise ValueError("pinned reference query returned empty or duplicate keys")
    database_hashes = {hashlib.sha256(key.encode("utf-8")).hexdigest() for key in normalized_keys}
    expected = len(normalized_keys)
    return {
        "schema_version": 1,
        "validator_id": REFERENCE_VALIDATOR_ID,
        "query_fingerprint": REFERENCE_QUERY_FINGERPRINT,
        "inventory_sha256": hashlib.sha256(_canonical_json(inventory)).hexdigest(),
        "expected": expected,
        "checked": expected,
        "mismatches": len(database_hashes.symmetric_difference(inventory_hashes)),
    }


def validate_complete_backup_group(
    group: Path,
    signing_key: Path,
    restore_lister: Callable[[Path], int],
) -> Mapping[str, Any]:
    validate_run_id(group.name)
    if group.is_symlink() or not group.is_dir():
        raise ValueError("backup group must be a non-symlink directory")
    manifest_path = group / "manifest.json"
    signature_path = group / "manifest.sig"
    complete_path = group / "COMPLETE"
    manifest = _require_mapping(_load_json(manifest_path), "backup manifest")
    validate_backup_manifest(manifest)
    if manifest.get("backup_run_id") != group.name:
        raise ValueError("backup manifest run binding is invalid")
    if manifest_path.read_bytes() != _canonical_json(manifest):
        raise ValueError("backup manifest is not canonical")
    verify_hmac_signature(manifest_path, signing_key, signature_path)
    complete = complete_path.read_text(encoding="ascii").strip()
    if complete != _sha256_file(manifest_path):
        raise ValueError("backup COMPLETE marker does not match manifest")

    dump = group / "database.dump"
    snapshot = group / "business.snapshot"
    inventory = group / "business.inventory.json"
    if not all(path.is_file() and not path.is_symlink() for path in (dump, snapshot, inventory)):
        raise ValueError("backup payload is missing or is a symlink")
    if dump.stat().st_size != manifest["database"]["size_bytes"] or _sha256_file(dump) != manifest["database"]["sha256"]:
        raise ValueError("database dump size or hash is invalid")
    if snapshot.stat().st_size != manifest["business_snapshot"]["size_bytes"] or _sha256_file(snapshot) != manifest["business_snapshot"]["sha256"]:
        raise ValueError("business snapshot size or hash is invalid")
    object_count, inventory_hash = _validate_business_inventory(inventory)
    if inventory_hash != manifest["business_snapshot"]["inventory_sha256"] or object_count != manifest["business_snapshot"]["object_count"]:
        raise ValueError("business inventory is invalid")
    listed_entries = restore_lister(dump)
    if listed_entries != manifest["database"]["restore_list_entries"]:
        raise ValueError("pg_restore list evidence does not match the custom dump")
    return manifest


def catalog_from_groups(
    root: Path,
    signing_key: Path,
    restore_lister: Callable[[Path], int],
) -> list[Mapping[str, Any]]:
    if not root.is_dir() or root.is_symlink():
        raise ValueError("catalog root must be a non-symlink directory")
    catalog: list[Mapping[str, Any]] = []
    for group in sorted(path for path in root.iterdir() if path.name not in {".incomplete", ".leases"}):
        if not group.is_dir() or group.is_symlink():
            raise ValueError(f"invalid catalog entry: {group.name}")
        try:
            manifest = validate_complete_backup_group(group, signing_key, restore_lister)
            complete_order = (group / "COMPLETE").stat().st_mtime_ns
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as error:
            raise ValueError(f"invalid catalog backup group: {group.name}") from error
        catalog.append(
            {
                "backup_run_id": manifest["backup_run_id"],
                "backup_cutoff_utc": manifest["backup_cutoff_utc"],
                "complete": True,
                "valid": True,
                "backup_window_days": manifest["retention"]["backup_window_days"],
                "complete_order": complete_order,
                "ledger_archive": manifest["ledger_archive"],
            }
        )
    if not catalog:
        raise ValueError("catalog has no complete backup groups")
    return catalog


def validate_publish_receipt(path: Path, run_id: str, complete_sha256: str) -> Mapping[str, Any]:
    receipt = _require_mapping(_load_json(path), "atomic publisher receipt")
    expected_fields = {"schema_version", "status", "backup_run_id", "complete_sha256", "lease_id_hash"}
    if set(receipt) != expected_fields or receipt.get("schema_version") != 1 or receipt.get("status") != "committed":
        raise ValueError("atomic publisher receipt schema is invalid")
    validate_run_id(str(receipt.get("backup_run_id", "")))
    if receipt.get("backup_run_id") != run_id or receipt.get("complete_sha256") != complete_sha256:
        raise ValueError("atomic publisher receipt is not bound to this complete group")
    _require_sha256(receipt.get("lease_id_hash"), "atomic publisher lease id hash")
    return receipt


def command_guard_disposable(_: argparse.Namespace) -> None:
    volumes = [item for item in _require_env("RECOVERY_VOLUME_NAMES").split(",") if item]
    validate_disposable_recovery(
        _require_env("COMPOSE_PROJECT_NAME"), volumes, os.environ.get("DISPOSABLE_RECOVERY_CONFIRMED", "")
    )


def command_drill_preflight(_: argparse.Namespace) -> None:
    validate_released_b2b3_commands(
        Path(_require_env("B2B3_CLI_COMMAND")),
        Path(_require_env("B2B3_WORKER_COMMAND")),
    )
    catalog = _load_json(Path(_require_env("BACKUP_CATALOG_FILE")))
    if not isinstance(catalog, list):
        raise ValueError("backup catalog must be an array")
    evidence = validate_drill_preflight(
        project=_require_env("COMPOSE_PROJECT_NAME"),
        volumes=[item for item in _require_env("RECOVERY_VOLUME_NAMES").split(",") if item],
        confirmed=os.environ.get("DISPOSABLE_RECOVERY_CONFIRMED", ""),
        image=_require_env("BACKUP_IMAGE"),
        digest=_require_env("BACKUP_IMAGE_DIGEST"),
        catalog=catalog,
        retention_days=int(_require_env("BACKUP_WINDOW_DAYS")),
        now=datetime.now(timezone.utc),
    )
    print(
        "drill preflight passed with traffic closed: "
        f"run_id={evidence['latest_backup_run_id']} image={evidence['immutable_image']}"
    )


def command_validate_manifest(args: argparse.Namespace) -> None:
    validate_backup_manifest(_load_json(Path(args.path)))


def command_validate_run_id(args: argparse.Namespace) -> None:
    validate_run_id(args.value)


def command_validate_buckets(args: argparse.Namespace) -> None:
    validate_business_buckets(args.value)


def command_validate_minio_alias(args: argparse.Namespace) -> None:
    validate_minio_alias(args.value)


def command_safe_extract(args: argparse.Namespace) -> None:
    buckets = {bucket for bucket in args.buckets.split(",") if bucket}
    safe_extract_business_snapshot(Path(args.archive), Path(args.destination), buckets)


def command_prune_plan(args: argparse.Namespace) -> None:
    catalog = _load_json(Path(args.catalog))
    if not isinstance(catalog, list):
        raise ValueError("catalog must be an array")
    for run_id in plan_prune(catalog, args.retention_days, datetime.now(timezone.utc)):
        print(run_id)


def command_inventory(args: argparse.Namespace) -> None:
    root = Path(args.root)
    if not root.is_dir():
        raise ValueError("inventory root must be a directory")
    objects = []
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        objects.append(
            {
                "key_hash": hashlib.sha256(relative).hexdigest(),
                "sha256": _sha256_file(path),
                "size_bytes": path.stat().st_size,
            }
        )
    _write_json(Path(args.output), {"schema_version": 1, "objects": objects})


def command_catalog_local(args: argparse.Namespace) -> None:
    root = Path(args.root)
    signing_key = Path(args.signing_key)

    def restore_lister(dump: Path) -> int:
        listed = _run([os.environ.get("PG_RESTORE_BIN", "pg_restore"), "--list", str(dump)], capture=True)
        return sum(1 for line in listed.stdout.splitlines() if line and not line.startswith(";"))

    catalog = catalog_from_groups(root, signing_key, restore_lister)
    _atomic_write_bytes(Path(args.output), (json.dumps(catalog, sort_keys=True) + "\n").encode("utf-8"))


def command_traffic_gate(args: argparse.Namespace) -> None:
    marker = Path(args.marker)
    if marker.is_symlink():
        raise SecurityContractError("traffic-open marker symlinks are forbidden")
    marker.unlink(missing_ok=True)
    try:
        evidence = validate_b2b3_evidence(
            Path(args.restore_evidence),
            Path(args.b2b3_evidence),
            Path(args.b2b3_signature),
            Path(args.b2b3_verify_key),
        )
        restore = _require_mapping(_load_json(Path(args.restore_evidence)), "restore evidence")
        require_traffic_open_evidence(restore, evidence)
        _atomic_write_json(Path(args.closed_evidence), _traffic_closed_evidence(restore))
    except (OSError, ValueError, KeyError, TypeError):
        raise SecurityContractError("verified B2B3 evidence is unavailable; traffic remains closed") from None
    print("B2B3 evidence verified; traffic remains closed pending external environment acceptance")


def command_backup(_: argparse.Namespace) -> None:
    run_id = validate_run_id(_require_env("BACKUP_RUN_ID"))
    business_cutoff = _require_env("BACKUP_CUTOFF_UTC")
    _parse_utc(business_cutoff)
    buckets = validate_business_buckets(_require_env("BUSINESS_BUCKETS"))
    destination = validate_off_host_destination(
        _require_env("BACKUP_DESTINATION"),
        _require_env("APPLICATION_HOST"),
        os.environ.get("FORBIDDEN_DATA_PATHS", "").split(os.pathsep),
    )
    secret_paths = _secret_paths(
        "PGPASSFILE", "BUSINESS_SOURCE_CONFIG_FILE", "BACKUP_DESTINATION_CONFIG_FILE",
        "BACKUP_MANIFEST_SIGNING_KEY_FILE", "LEDGER_MANIFEST_VERIFY_KEY_FILE",
    )
    ledger_group = Path(_require_env("LEDGER_PAIRING_GROUP_PATH"))
    ledger_history = _load_json(Path(_require_env("LEDGER_SIGNING_KEY_HISTORY_FILE")))
    staging_root = Path(_require_env("BACKUP_STAGING_ROOT"))
    staging_root.mkdir(parents=True, exist_ok=True)
    staging = safe_run_path(staging_root, run_id, prefix=".pending-")
    receipt = safe_run_path(staging_root, run_id, prefix=".publisher-receipt-")
    private_root = safe_run_path(staging_root, run_id, prefix=".secrets-")
    if staging.exists():
        raise ValueError("backup staging run already exists")
    staging.mkdir(parents=True, mode=0o700)
    try:
        with secure_secret_copies(secret_paths, private_root) as private:
            pgpass, source_config, publisher_config, signing_key, ledger_verify_key = private
            child_environment = _sanitized_child_environment(
                runtime_values={"MINIO_ALIAS": validate_minio_alias(_require_env("MINIO_ALIAS"))},
                private_secret_paths={"PGPASSFILE": str(pgpass)},
            )
            ledger_evidence = validate_ledger_pairing(
                ledger_group,
                ledger_verify_key,
                ledger_history,
                business_run_id=run_id,
                business_cutoff_utc=business_cutoff,
            )
            dump = staging / "database.dump"
            snapshot = staging / "business.snapshot"
            inventory = staging / "business.inventory.json"
            _run([
                os.environ.get("PG_DUMP_BIN", "pg_dump"), "--format=custom", "--file", str(dump),
                "--host", _require_env("PGHOST"), "--port", os.environ.get("PGPORT", "5432"),
                "--username", _require_env("PGUSER"), "--dbname", _require_env("PGDATABASE"), "--no-password",
            ], environment=child_environment)
            if not dump.is_file() or dump.stat().st_size <= 0:
                raise ValueError("pg_dump produced an empty custom-format dump")
            listed = _run(
                [os.environ.get("PG_RESTORE_BIN", "pg_restore"), "--list", str(dump)],
                capture=True,
                environment=child_environment,
            )
            list_entries = sum(1 for line in listed.stdout.splitlines() if line and not line.startswith(";"))
            if list_entries <= 0:
                raise ValueError("pg_restore --list produced no archive entries")
            ledger_bucket = _require_env("LEDGER_BUCKET")
            ledger_destination = validate_off_host_destination(
                _require_env("LEDGER_ARCHIVE_DESTINATION"),
                _require_env("APPLICATION_HOST"),
                os.environ.get("FORBIDDEN_DATA_PATHS", "").split(os.pathsep),
            )
            validate_ledger_boundary(
                business_buckets=buckets,
                ledger_bucket=ledger_bucket,
                business_capabilities={"business:read", "destination:append"},
                business_destination=destination,
                ledger_destination=ledger_destination,
                key_history=ledger_history,
            )
            _client(
                "BUSINESS_SNAPSHOT_CLIENT", "snapshot", "--config-file", str(source_config),
                "--buckets", ",".join(buckets), "--output", str(snapshot), "--inventory", str(inventory),
                environment=child_environment,
            )
            if not snapshot.is_file() or snapshot.stat().st_size <= 0:
                raise ValueError("business snapshot is empty")
            object_count, inventory_hash = _validate_business_inventory(inventory)
            inventory_value = _require_mapping(_load_json(inventory), "business inventory")
            queried = _run([
                os.environ.get("PSQL_BIN", "psql"), "--no-align", "--tuples-only", "--quiet",
                "--set", "ON_ERROR_STOP=1", "--host", _require_env("PGHOST"),
                "--port", os.environ.get("PGPORT", "5432"), "--username", _require_env("PGUSER"),
                "--dbname", _require_env("PGDATABASE"), "--command", REFERENCE_QUERY,
            ], capture=True, environment=child_environment)
            database_keys = [line for line in queried.stdout.splitlines() if line]
            references = build_reference_proof(database_keys, inventory_value)
            validate_reference_proof(references, expected=len(database_keys), inventory_sha256=inventory_hash)

            image = _require_env("BACKUP_IMAGE")
            image_digest = _require_env("BACKUP_IMAGE_DIGEST")
            validate_backup_image_reference(image, image_digest)
            manifest = {
                "schema_version": 1,
                "backup_run_id": run_id,
                "state": "complete",
                "backup_cutoff_utc": business_cutoff,
                "schedule_interval_hours": 12,
                "toolchain": {
                    "image": image,
                    "image_digest": image_digest,
                    "postgres": "16.9",
                    "minio_client": "RELEASE.2025-07-21T05-28-08Z",
                    "destination_client": "external-atomic-publisher-v1",
                },
                "database": {
                    "format": "custom", "sha256": _sha256_file(dump), "size_bytes": dump.stat().st_size,
                    "restore_list_entries": list_entries,
                },
                "business_snapshot": {
                    "sha256": _sha256_file(snapshot), "size_bytes": snapshot.stat().st_size,
                    "object_count": object_count, "inventory_sha256": inventory_hash,
                },
                "reference_validation": references,
                "ledger_archive": ledger_evidence,
                "retention": {
                    "backup_window_days": int(_require_env("BACKUP_WINDOW_DAYS")),
                    "policy_version": _require_env("RETENTION_POLICY_VERSION"),
                },
                "gates": {"pg_restore_list": True, "hashes": True, "object_inventory": True, "references": True},
            }
            validate_backup_manifest(manifest)
            payload = _canonical_json(manifest)
            _atomic_write_bytes(staging / "manifest.json", payload)
            write_hmac_signature(staging / "manifest.json", signing_key, staging / "manifest.sig")
            complete_sha256 = hashlib.sha256(payload).hexdigest()
            _atomic_write_bytes(staging / "COMPLETE", (complete_sha256 + "\n").encode("ascii"))
            _client(
                "BACKUP_ATOMIC_PUBLISHER", "publish-complete-group", "--lease-config-file", str(publisher_config),
                "--destination", destination, "--run-id", run_id, "--source", str(staging), "--receipt", str(receipt),
                environment=child_environment,
            )
            validate_publish_receipt(receipt, run_id, complete_sha256)
        print(f"backup complete: run_id={run_id}")
    finally:
        shutil.rmtree(staging, ignore_errors=True)
        receipt.unlink(missing_ok=True)


def command_prune(_: argparse.Namespace) -> None:
    destination = validate_off_host_destination(
        _require_env("BACKUP_DESTINATION"), _require_env("APPLICATION_HOST"),
        os.environ.get("FORBIDDEN_DATA_PATHS", "").split(os.pathsep),
    )
    secrets_to_copy = _secret_paths("BACKUP_PRUNE_CONFIG_FILE", "BACKUP_MANIFEST_SIGNING_KEY_FILE")
    catalog_path = Path(_require_env("BACKUP_CATALOG_FILE"))
    private_root = _private_secret_root(catalog_path.parent, "prune")
    with secure_secret_copies(secrets_to_copy, private_root) as private:
        prune_config, signing_key = private
        child_environment = _sanitized_child_environment()
        _client(
            "BACKUP_DESTINATION_CLIENT", "catalog", "--config-file", str(prune_config),
            "--signing-key-file", str(signing_key), "--destination", destination, "--output", str(catalog_path),
            environment=child_environment,
        )
        catalog = _load_json(catalog_path)
        if not isinstance(catalog, list):
            raise ValueError("backup catalog must be an array")
        deletions = plan_prune(catalog, int(_require_env("BACKUP_WINDOW_DAYS")), datetime.now(timezone.utc))
        for run_id in deletions:
            validate_run_id(run_id)
            _client(
                "BACKUP_DESTINATION_CLIENT", "delete-complete-group", "--config-file", str(prune_config),
                "--destination", destination, "--run-id", run_id,
                environment=child_environment,
            )
    print(f"prune complete: deleted_groups={len(deletions)}")


def command_restore(_: argparse.Namespace) -> None:
    command_guard_disposable(argparse.Namespace())
    run_id = validate_run_id(_require_env("RESTORE_BACKUP_RUN_ID"))
    generation_id = validate_run_id(_require_env("RESTORE_GENERATION_ID"))
    try:
        restore_id = str(UUID(generation_id))
    except ValueError:
        raise ValueError("RESTORE_GENERATION_ID must be the B2B3 restore UUID") from None
    evidence_path = Path(_require_env("RESTORE_EVIDENCE_FILE"))
    closed_marker_path = Path(_require_env("TRAFFIC_CLOSED_MARKER_FILE"))
    open_marker_path = Path(_require_env("TRAFFIC_OPEN_MARKER_FILE"))
    begin_closed_recovery(evidence_path, closed_marker_path, open_marker_path, run_id, generation_id)
    buckets = validate_business_buckets(_require_env("BUSINESS_BUCKETS"))

    business_destination = validate_off_host_destination(
        _require_env("BACKUP_DESTINATION"), _require_env("APPLICATION_HOST"),
        os.environ.get("FORBIDDEN_DATA_PATHS", "").split(os.pathsep),
    )
    ledger_destination = validate_off_host_destination(
        _require_env("LEDGER_ARCHIVE_DESTINATION"), _require_env("APPLICATION_HOST"),
        os.environ.get("FORBIDDEN_DATA_PATHS", "").split(os.pathsep),
    )
    if business_destination.rstrip("/") == ledger_destination.rstrip("/"):
        raise ValueError("ledger and business restore destinations must be independent")
    secret_paths = _secret_paths(
        "PGPASSFILE", "BUSINESS_RESTORE_CONFIG_FILE", "BACKUP_RESTORE_CONFIG_FILE", "LEDGER_RESTORE_CONFIG_FILE",
        "BACKUP_MANIFEST_SIGNING_KEY_FILE", "LEDGER_MANIFEST_SIGNING_KEY_FILE",
    )
    staging_root = Path(_require_env("RESTORE_STAGING_ROOT"))
    staging_root.mkdir(parents=True, exist_ok=True)
    workspace = safe_run_path(staging_root, run_id)
    private_root = safe_run_path(staging_root, generation_id, prefix=".restore-secrets-")
    if workspace.exists():
        raise ValueError("restore staging path already exists")
    workspace.mkdir(parents=True, mode=0o700)
    try:
        with secure_secret_copies(secret_paths, private_root) as private:
            pgpass, business_restore, destination_restore, ledger_restore, manifest_signing_key, ledger_signing_key = private
            child_environment = _sanitized_child_environment(
                runtime_values={"MINIO_ALIAS": validate_minio_alias(_require_env("MINIO_ALIAS"))},
                private_secret_paths={"PGPASSFILE": str(pgpass)},
            )
            _client(
                "BACKUP_DESTINATION_CLIENT", "fetch-complete-group", "--config-file", str(destination_restore),
                "--destination", business_destination, "--run-id", run_id, "--output", str(workspace),
                environment=child_environment,
            )

            def restore_lister(dump_path: Path) -> int:
                listed = _run(
                    [os.environ.get("PG_RESTORE_BIN", "pg_restore"), "--list", str(dump_path)],
                    capture=True,
                    environment=child_environment,
                )
                return sum(1 for line in listed.stdout.splitlines() if line and not line.startswith(";"))

            manifest = validate_complete_backup_group(workspace, manifest_signing_key, restore_lister)
            selection_path = workspace / "ledger-selection.json"
            _client(
                "LEDGER_ARCHIVE_CLIENT", "select-latest-complete", "--config-file", str(ledger_restore),
                "--destination", ledger_destination, "--output", str(selection_path),
                environment=child_environment,
            )
            selection = _require_mapping(_load_json(selection_path), "ledger archive selection")
            if set(selection) != {"schema_version", "archive_run_id"} or selection.get("schema_version") != 1:
                raise ValueError("ledger archive selection schema is invalid")
            ledger_run_id = validate_run_id(str(selection.get("archive_run_id", "")))
            ledger_group = safe_run_path(workspace, ledger_run_id)
            _client(
                "LEDGER_ARCHIVE_CLIENT", "fetch-complete-group", "--config-file", str(ledger_restore),
                "--destination", ledger_destination, "--run-id", ledger_run_id, "--output", str(ledger_group),
                environment=child_environment,
            )
            key_history = _load_json(Path(_require_env("LEDGER_SIGNING_KEY_HISTORY_FILE")))
            ledger_manifest = validate_ledger_archive_group(
                ledger_group,
                ledger_signing_key,
                key_history,
                minimum_cutoff_utc=str(manifest["backup_cutoff_utc"]),
            )
            _client(
                "LEDGER_ARCHIVE_CLIENT", "restore-verified", "--config-file", str(ledger_restore),
                "--archive", str(ledger_group / "ledger.snapshot"), "--run-id", ledger_run_id,
                "--generation-id", generation_id,
                environment=child_environment,
            )
            proof_path = workspace / "ledger-restore-proof.json"
            proof_signature_path = workspace / "ledger-restore-proof.sig"
            _client(
                "LEDGER_RESTORE_PROOF_CLIENT", "attest-restored-archive", "--config-file", str(ledger_restore),
                "--archive-run-id", ledger_run_id, "--business-run-id", run_id,
                "--generation-id", generation_id, "--proof", str(proof_path), "--signature", str(proof_signature_path),
                environment=child_environment,
            )
            validate_ledger_restore_proof(
                proof_path, proof_signature_path, ledger_signing_key, ledger_manifest, run_id, generation_id
            )
            ledger_verified = True

            dump = workspace / "database.dump"
            snapshot = workspace / "business.snapshot"
            _run([
                os.environ.get("PG_RESTORE_BIN", "pg_restore"), "--exit-on-error", "--clean", "--if-exists",
                "--no-owner", "--host", _require_env("PGHOST"), "--port", os.environ.get("PGPORT", "5432"),
                "--username", _require_env("PGUSER"), "--dbname", _require_env("PGDATABASE"), str(dump),
            ], environment=child_environment)
            _client(
                "BUSINESS_RESTORE_CLIENT", "restore", "--config-file", str(business_restore),
                "--snapshot", str(snapshot), "--buckets", ",".join(buckets),
                environment=child_environment,
            )
            evidence = {
                "schema_version": 1,
                "state": "restored_traffic_closed",
                "backup_run_id": run_id,
                "recovery_generation_id": generation_id,
                "restore_id": restore_id,
                "backup_cutoff_utc": str(manifest["backup_cutoff_utc"]),
                "backup_manifest_sha256": _sha256_file(workspace / "manifest.json"),
                "ledger_manifest_sha256": _sha256_file(ledger_group / "ledger-manifest.json"),
                "traffic_open": False,
                "gates": {"database": True, "business_objects": True, "ledger_restored_first": ledger_verified},
            }
            _write_json(evidence_path, evidence)
            _write_json(closed_marker_path, evidence)
        print(f"restore complete with traffic closed: run_id={run_id}")
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


def command_ledger_archive(_: argparse.Namespace) -> None:
    business_buckets = validate_business_buckets(_require_env("BUSINESS_BUCKETS"))
    destination = validate_off_host_destination(
        _require_env("LEDGER_ARCHIVE_DESTINATION"), _require_env("APPLICATION_HOST"),
        os.environ.get("FORBIDDEN_DATA_PATHS", "").split(os.pathsep),
    )
    secret_paths = _secret_paths(
        "LEDGER_SOURCE_CONFIG_FILE", "LEDGER_ARCHIVE_CONFIG_FILE", "LEDGER_MANIFEST_SIGNING_KEY_FILE"
    )
    history = _load_json(Path(_require_env("LEDGER_SIGNING_KEY_HISTORY_FILE")))
    validate_ledger_boundary(
        business_buckets=business_buckets,
        ledger_bucket=_require_env("LEDGER_BUCKET"),
        business_capabilities={"business:read", "destination:append"},
        business_destination=_require_env("BACKUP_DESTINATION"),
        ledger_destination=destination,
        key_history=history,
    )
    archive_run_id = validate_run_id(_require_env("LEDGER_ARCHIVE_RUN_ID"))
    staging_root = Path(_require_env("LEDGER_STAGING_ROOT"))
    staging_root.mkdir(parents=True, exist_ok=True)
    work = safe_run_path(staging_root, archive_run_id, prefix=".pending-")
    receipt = safe_run_path(staging_root, archive_run_id, prefix=".publisher-receipt-")
    private_root = safe_run_path(staging_root, archive_run_id, prefix=".secrets-")
    if work.exists():
        raise ValueError("ledger staging run already exists")
    work.mkdir(parents=True, mode=0o700)
    try:
        with secure_secret_copies(secret_paths, private_root) as private:
            source, publisher_config, signing_key = private
            child_environment = _sanitized_child_environment(
                runtime_values={"MINIO_ALIAS": validate_minio_alias(_require_env("MINIO_ALIAS"))}
            )
            archive = work / "ledger.snapshot"
            summary = work / "ledger-summary.json"
            _client(
                "LEDGER_ARCHIVE_CLIENT", "snapshot", "--config-file", str(source),
                "--bucket", _require_env("LEDGER_BUCKET"), "--output", str(archive), "--summary", str(summary),
                environment=child_environment,
            )
            if not archive.is_file() or archive.stat().st_size <= 0:
                raise ValueError("ledger archive is empty")
            summary_value = _require_mapping(_load_json(summary), "ledger summary")
            manifest = {
                "schema_version": 1,
                "archive_run_id": archive_run_id,
                "cutoff_utc": _require_env("LEDGER_ARCHIVE_CUTOFF_UTC"),
                "archive_sha256": _sha256_file(archive),
                "size_bytes": archive.stat().st_size,
                "entry_count": int(summary_value.get("entry_count", -1)),
                "signing_key_version": history["active_key_version"],
                "lifecycle_policy_version": _require_env("LEDGER_LIFECYCLE_POLICY_VERSION"),
            }
            if manifest["entry_count"] < 0:
                raise ValueError("ledger archive entry count is invalid")
            _parse_utc(str(manifest["cutoff_utc"]))
            _scan_manifest(manifest)
            _write_json(work / "ledger-manifest.json", manifest)
            write_hmac_signature(work / "ledger-manifest.json", signing_key, work / "ledger-manifest.sig")
            complete_sha256 = _sha256_file(work / "ledger-manifest.json")
            _atomic_write_bytes(work / "COMPLETE", (complete_sha256 + "\n").encode("ascii"))
            _client(
                "LEDGER_ATOMIC_PUBLISHER", "publish-complete-group", "--lease-config-file", str(publisher_config),
                "--destination", destination, "--run-id", archive_run_id, "--source", str(work),
                "--receipt", str(receipt),
                environment=child_environment,
            )
            validate_publish_receipt(receipt, archive_run_id, complete_sha256)
            output = Path(_require_env("LEDGER_ARCHIVE_MANIFEST_FILE"))
            _write_json(output, manifest)
        print(f"ledger archive complete: run_id={archive_run_id}")
    finally:
        shutil.rmtree(work, ignore_errors=True)
        receipt.unlink(missing_ok=True)


def run_b2b3_protocol(
    restore_path: Path,
    evidence_path: Path,
    signature_path: Path,
    verify_key_path: Path,
    closed_evidence_path: Path,
) -> Mapping[str, Any]:
    restore = _require_mapping(_load_json(restore_path), "restore evidence")
    binding = _restore_binding(restore)
    try:
        validate_released_b2b3_commands(
            Path(_require_env("B2B3_CLI_COMMAND")),
            Path(_require_env("B2B3_WORKER_COMMAND")),
        )
    except (OSError, ValueError):
        write_drill_failure_evidence(
            closed_evidence_path, restore, "b2b3_release_preflight_failed"
        )
        raise ValueError("released B2B3 preflight failed; traffic remains closed") from None
    try:
        validate_secret_files([verify_key_path])
        for path in (evidence_path, signature_path):
            if path.is_symlink():
                raise SecurityContractError("B2B3 evidence paths cannot be symlinks")
            path.unlink(missing_ok=True)
    except (OSError, ValueError):
        write_drill_failure_evidence(
            closed_evidence_path, restore, "b2b3_evidence_preflight_failed"
        )
        raise ValueError("B2B3 evidence preflight failed; traffic remains closed") from None
    child_environment = _sanitized_child_environment(
        runtime_values={
            "B2B3_BACKUP_RUN_ID": binding["backup_run_id"],
            "B2B3_RECOVERY_GENERATION_ID": binding["recovery_generation_id"],
            "B2B3_RESTORE_ID": binding["restore_id"],
        }
    )
    try:
        cli = _client(
            "B2B3_CLI_COMMAND",
            "--restore-id",
            binding["restore_id"],
            "--restored-at",
            str(restore["backup_cutoff_utc"]),
            capture=True,
            environment=child_environment,
        )
        prepared_matches = re.findall(r"^recovery_prepared=([0-9]+)$", cli.stdout, re.MULTILINE)
        if len(prepared_matches) != 1 or int(prepared_matches[0]) <= 0:
            raise ValueError("B2B3 CLI did not prepare a real recovery")
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError):
        write_drill_failure_evidence(closed_evidence_path, restore, "b2b3_cli_failed")
        raise ValueError("released B2B3 CLI failed; traffic remains closed") from None
    prepared = int(prepared_matches[0])
    try:
        _client(
            "B2B3_WORKER_COMMAND",
            "--restore-id",
            binding["restore_id"],
            "--backup-run-id",
            binding["backup_run_id"],
            "--generation-id",
            binding["recovery_generation_id"],
            "--backup-manifest-sha256",
            binding["backup_manifest_sha256"],
            "--ledger-manifest-sha256",
            binding["ledger_manifest_sha256"],
            "--evidence",
            str(evidence_path),
            "--signature",
            str(signature_path),
            environment=child_environment,
        )
        evidence = validate_b2b3_evidence(
            restore_path, evidence_path, signature_path, verify_key_path
        )
        if evidence["counts"]["prepared_redeletions"] != prepared:
            raise ValueError("B2B3 CLI and Worker recovery counts are inconsistent")
        require_traffic_open_evidence(restore, evidence)
        _atomic_write_json(closed_evidence_path, _traffic_closed_evidence(restore))
        return evidence
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError):
        write_drill_failure_evidence(closed_evidence_path, restore, "b2b3_worker_or_evidence_failed")
        raise ValueError("released B2B3 Worker or evidence verification failed; traffic remains closed") from None


def command_drill(_: argparse.Namespace) -> None:
    command_drill_preflight(argparse.Namespace())
    catalog = _load_json(Path(_require_env("BACKUP_CATALOG_FILE")))
    if not isinstance(catalog, list):
        raise ValueError("backup catalog must be an array")
    latest = max(
        (item for item in catalog if item.get("complete") is True and item.get("valid") is True),
        key=lambda item: int(item["complete_order"]),
    )
    os.environ.setdefault("RESTORE_BACKUP_RUN_ID", validate_run_id(str(latest["backup_run_id"])))
    os.environ.setdefault("RESTORE_GENERATION_ID", str(uuid4()))
    command_restore(argparse.Namespace())
    evidence = run_b2b3_protocol(
        Path(_require_env("RESTORE_EVIDENCE_FILE")),
        Path(_require_env("B2B3_EVIDENCE_FILE")),
        Path(_require_env("B2B3_EVIDENCE_SIGNATURE_FILE")),
        Path(_require_env("B2B3_EVIDENCE_VERIFY_KEY_FILE")),
        Path(_require_env("DRILL_EVIDENCE_FILE")),
    )
    print(
        "restore re-delete verified with traffic closed: "
        f"completed_redeletions={evidence['counts']['completed_redeletions']}"
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    validate = subparsers.add_parser("validate-manifest")
    validate.add_argument("path")
    validate.set_defaults(handler=command_validate_manifest)
    run_id = subparsers.add_parser("validate-run-id")
    run_id.add_argument("value")
    run_id.set_defaults(handler=command_validate_run_id)
    buckets = subparsers.add_parser("validate-buckets")
    buckets.add_argument("value")
    buckets.set_defaults(handler=command_validate_buckets)
    minio_alias = subparsers.add_parser("validate-minio-alias")
    minio_alias.add_argument("value")
    minio_alias.set_defaults(handler=command_validate_minio_alias)
    safe_extract = subparsers.add_parser("safe-extract")
    safe_extract.add_argument("archive")
    safe_extract.add_argument("destination")
    safe_extract.add_argument("buckets")
    safe_extract.set_defaults(handler=command_safe_extract)
    prune_plan = subparsers.add_parser("prune-plan")
    prune_plan.add_argument("catalog")
    prune_plan.add_argument("retention_days", type=int)
    prune_plan.set_defaults(handler=command_prune_plan)
    inventory = subparsers.add_parser("inventory")
    inventory.add_argument("root")
    inventory.add_argument("output")
    inventory.set_defaults(handler=command_inventory)
    catalog = subparsers.add_parser("catalog-local")
    catalog.add_argument("root")
    catalog.add_argument("output")
    catalog.add_argument("signing_key")
    catalog.set_defaults(handler=command_catalog_local)
    disposable = subparsers.add_parser("guard-disposable")
    disposable.set_defaults(handler=command_guard_disposable)
    preflight = subparsers.add_parser("preflight-drill")
    preflight.set_defaults(handler=command_drill_preflight)
    traffic = subparsers.add_parser("traffic-gate")
    traffic.add_argument("restore_evidence")
    traffic.add_argument("b2b3_evidence")
    traffic.add_argument("b2b3_signature")
    traffic.add_argument("b2b3_verify_key")
    traffic.add_argument("marker")
    traffic.add_argument("closed_evidence")
    traffic.set_defaults(handler=command_traffic_gate)
    for name, handler in (
        ("backup", command_backup),
        ("prune", command_prune),
        ("restore", command_restore),
        ("ledger-archive", command_ledger_archive),
        ("drill", command_drill),
    ):
        subparser = subparsers.add_parser(name)
        subparser.set_defaults(handler=handler)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        args.handler(args)
    except SecurityContractError as error:
        print(f"backup security contract failed closed: {error}", file=sys.stderr)
        return 78
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError) as error:
        print(f"backup contract failed closed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
