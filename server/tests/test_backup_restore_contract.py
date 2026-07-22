from __future__ import annotations

from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
import hashlib
import hmac
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import tarfile
import time
import uuid

import pytest


ROOT = Path(__file__).resolve().parents[2]
BACKUP_DIR = ROOT / "deploy" / "backup"
BACKUPCTL = BACKUP_DIR / "backupctl.py"
DRILL_COMPOSE = ROOT / "deploy" / "compose.backup-drill.yaml"
BACKUP_RUNBOOK = ROOT / "deploy" / "backup-recovery-runbook.md"
OPERATIONS_RUNBOOK = ROOT / "deploy" / "production-operations-runbook.md"
FOUNDATION_REPORT = ROOT / ".superpowers" / "sdd" / "task-phase6c-report.md"
PUBLISHER = BACKUP_DIR / "s3-atomic-publisher.py"
PUBLISHER_REPORT = ROOT / ".superpowers" / "sdd" / "task-phase6c-publisher-report.md"
REFERENCE_QUERY = """SELECT storage_key FROM file_objects WHERE storage_state <> 'deleted'
UNION
SELECT object_key FROM report_exports WHERE object_key IS NOT NULL AND status = 'succeeded'
ORDER BY 1"""
REFERENCE_QUERY_FINGERPRINT = hashlib.sha256(REFERENCE_QUERY.encode("utf-8")).hexdigest()


def _load_backupctl():
    assert BACKUPCTL.is_file(), "Phase 6C backup contract core is missing"
    spec = importlib.util.spec_from_file_location("phase6c_backupctl", BACKUPCTL)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous
    return module


def _load_publisher():
    assert PUBLISHER.is_file(), "Phase 6C S3-compatible publisher is missing"
    spec = importlib.util.spec_from_file_location("phase6c_s3_publisher", PUBLISHER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous
    return module


def _write_publisher_source(root: Path, run_id: str, marker: bytes = b"writer-one") -> Path:
    source = root / "source"
    source.mkdir(parents=True)
    (source / "database.dump").write_bytes(marker)
    database = marker
    snapshot = b"business-snapshot"
    inventory = b'{"key_hash":"' + b"a" * 64 + b'"}\n'
    (source / "business.snapshot").write_bytes(snapshot)
    (source / "inventory.jsonl").write_bytes(inventory)
    (source / "reference-proof.json").write_text('{"schema_version":1}\n', encoding="utf-8")
    manifest = (json.dumps({
        "backup_run_id": run_id,
        "database": {"sha256": hashlib.sha256(database).hexdigest(), "size_bytes": len(database)},
        "business_snapshot": {
            "sha256": hashlib.sha256(snapshot).hexdigest(),
            "size_bytes": len(snapshot),
            "inventory_sha256": hashlib.sha256(inventory).hexdigest(),
        },
    }, sort_keys=True, separators=(",", ":")) + "\n").encode()
    (source / "manifest.json").write_bytes(manifest)
    (source / "manifest.sig").write_bytes(("b" * 64 + "\n").encode("ascii"))
    (source / "COMPLETE").write_bytes((hashlib.sha256(manifest).hexdigest() + "\n").encode("ascii"))
    return source


def _publisher_args(config: Path, source: Path, receipt: Path, run_id: str) -> list[str]:
    return [
        "publish-complete-group",
        "--lease-config-file", str(config),
        "--destination", "minio://backup/vault/ux09",
        "--run-id", run_id,
        "--source", str(source),
        "--receipt", str(receipt),
    ]


def _reconcile_args(config: Path, receipt: Path, run_id: str) -> list[str]:
    return [
        "reconcile-complete-group",
        "--lease-config-file", str(config),
        "--destination", "minio://backup/vault/ux09",
        "--run-id", run_id,
        "--receipt", str(receipt),
    ]


def _write_test_mc_config(path: Path) -> None:
    path.write_text(
        json.dumps({
            "version": "10",
            "aliases": {
                "backup": {
                    "url": "http://provider.invalid",
                    "accessKey": "synthetic-user",
                    "secretKey": "synthetic-password",
                    "api": "S3v4",
                    "path": "auto",
                }
            },
        }),
        encoding="utf-8",
    )
    path.chmod(0o600)


class _FakeConditionalStore:
    def __init__(self, publisher, *, fail_upload_number: int | None = None):
        self.publisher = publisher
        self.fail_upload_number = fail_upload_number
        self.objects: dict[str, bytes] = {}
        self.commands: list[tuple[str, ...]] = []
        self.uploads = 0

    def __call__(self, command, *, stdin=None, capture_stdout=False):
        args = tuple(command)
        self.commands.append(args)
        operation = next(item for item in ("pipe", "put", "stat", "get") if item in args)
        target = args[-1]
        if operation == "pipe":
            assert "If-None-Match:*" in args
            if target in self.objects:
                raise self.publisher.LeaseConflict()
            assert stdin is not None
            self.objects[target] = stdin.read()
            return b""
        if operation == "put":
            self.uploads += 1
            if self.uploads == self.fail_upload_number:
                raise self.publisher.ProviderFailure()
            self.objects[target] = Path(args[-2]).read_bytes()
            return b""
        if operation == "stat":
            if target not in self.objects:
                raise self.publisher.ObjectMissing()
            return json.dumps({"size": len(self.objects[target])}).encode()
        if operation == "get":
            target = args[-2]
            if target not in self.objects:
                raise self.publisher.ObjectMissing()
            Path(args[-1]).write_bytes(self.objects[target])
            return b""
        raise AssertionError("unexpected fake provider operation")


def _manifest(**overrides):
    manifest = {
        "schema_version": 1,
        "backup_run_id": "20260715T000000Z-a1b2c3d4",
        "state": "complete",
        "backup_cutoff_utc": "2026-07-15T00:00:00Z",
        "schedule_interval_hours": 12,
        "toolchain": {
            "image": "ux09-backup:phase6c-foundation",
            "image_digest": "sha256:" + "a" * 64,
            "postgres": "16.9",
            "minio_client": "RELEASE.2025-07-21T05-28-08Z",
            "destination_client": "rclone-1.70.3",
        },
        "database": {
            "format": "custom",
            "sha256": "b" * 64,
            "size_bytes": 4096,
            "restore_list_entries": 23,
        },
        "business_snapshot": {
            "sha256": "c" * 64,
            "size_bytes": 8192,
            "object_count": 7,
            "inventory_sha256": "d" * 64,
        },
        "reference_validation": {
            "schema_version": 1,
            "validator_id": "ux09-reference-validator-v1",
            "query_fingerprint": REFERENCE_QUERY_FINGERPRINT,
            "inventory_sha256": "d" * 64,
            "expected": 7,
            "checked": 7,
            "mismatches": 0,
        },
        "ledger_archive": {
            "archive_run_id": "ledger-20260715T000000Z-e5f6a7b8",
            "cutoff_utc": "2026-07-15T00:00:00Z",
            "manifest_sha256": "e" * 64,
            "signing_key_versions": ["ledger-v1", "ledger-v2"],
        },
        "retention": {"backup_window_days": 30, "policy_version": "rp-v4"},
        "gates": {
            "pg_restore_list": True,
            "hashes": True,
            "object_inventory": True,
            "references": True,
        },
    }
    manifest.update(overrides)
    return manifest


def _catalog_entry(run_id: str, cutoff: str, **overrides):
    item = {
        "backup_run_id": run_id,
        "backup_cutoff_utc": cutoff,
        "complete": True,
        "valid": True,
        "backup_window_days": 30,
        "complete_order": 1,
    }
    item.update(overrides)
    return item


def test_phase6c_foundation_files_are_new_isolated_contract_surface() -> None:
    required = {
        BACKUPCTL,
        PUBLISHER,
        PUBLISHER_REPORT,
        BACKUP_DIR / "Dockerfile",
        BACKUP_DIR / "backup.sh",
        BACKUP_DIR / "ledger-archive.sh",
        BACKUP_DIR / "prune.sh",
        BACKUP_DIR / "restore.sh",
        BACKUP_DIR / "drill.sh",
        BACKUP_DIR / "traffic-gate.sh",
        DRILL_COMPOSE,
        BACKUP_RUNBOOK,
        OPERATIONS_RUNBOOK,
        FOUNDATION_REPORT,
    }
    missing = sorted(path.relative_to(ROOT).as_posix() for path in required if not path.is_file())
    assert not missing, f"missing Phase 6C foundation files: {missing}"


def test_phase6c_scope_rejects_generated_artifacts_and_staged_files_outside_allowlist() -> None:
    generated = sorted(
        path.relative_to(ROOT).as_posix()
        for path in BACKUP_DIR.rglob("*")
        if path.name == "__pycache__" or path.suffix == ".pyc"
    )
    assert not generated, f"generated backup artifacts must not exist or be committed: {generated}"

    staged = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    exact = {
        ".superpowers/sdd/task-phase6c-publisher-report.md",
        "deploy/backup-recovery-runbook.md",
        "deploy/production-operations-runbook.md",
        "server/tests/test_backup_restore_contract.py",
    }
    unexpected = [
        path for path in staged if path not in exact and not path.startswith("deploy/backup/")
    ]
    assert not unexpected, f"staged files outside the Phase 6C allowlist: {unexpected}"
    assert not any("__pycache__" in path or path.endswith(".pyc") for path in staged)


def test_manifest_schema_accepts_only_complete_non_pii_evidence() -> None:
    backupctl = _load_backupctl()
    backupctl.validate_backup_manifest(_manifest())

    for forbidden_key, value in (
        ("object_key", "clean/candidate-123/resume.pdf"),
        ("candidate_id", "c-123"),
        ("request_id", "r-123"),
        ("filename", "alice-resume.pdf"),
        ("password", "synthetic-secret"),
        ("secret", "synthetic-secret"),
    ):
        tainted = _manifest(**{forbidden_key: value})
        with pytest.raises(ValueError, match="forbidden|sensitive|PII"):
            backupctl.validate_backup_manifest(tainted)

    tainted_value = _manifest(operator_note="alice@example.test")
    with pytest.raises(ValueError, match="forbidden|sensitive|PII"):
        backupctl.validate_backup_manifest(tainted_value)


def test_manifest_rejects_unverified_or_unpaired_restore_points() -> None:
    backupctl = _load_backupctl()
    for broken in (
        _manifest(state="pending"),
        _manifest(database={**_manifest()["database"], "restore_list_entries": 0}),
        _manifest(reference_validation={**_manifest()["reference_validation"], "mismatches": 1}),
        _manifest(gates={**_manifest()["gates"], "hashes": False}),
        _manifest(schedule_interval_hours=24),
        _manifest(operator_note="no arbitrary extension fields"),
    ):
        with pytest.raises(ValueError):
            backupctl.validate_backup_manifest(broken)


def test_manifest_signature_verification_rejects_payload_tamper(tmp_path: Path) -> None:
    backupctl = _load_backupctl()
    payload = tmp_path / "manifest.json"
    signature = tmp_path / "manifest.sig"
    key = tmp_path / "manifest-signing-key"
    payload.write_text('{"schema_version":1}\n', encoding="utf-8")
    key.write_bytes(b"k" * 32)

    backupctl.write_hmac_signature(payload, key, signature)
    backupctl.verify_hmac_signature(payload, key, signature)
    payload.write_text('{"schema_version":2}\n', encoding="utf-8")
    with pytest.raises(ValueError, match="signature"):
        backupctl.verify_hmac_signature(payload, key, signature)


def test_atomic_publish_writes_manifest_and_complete_only_after_validation(tmp_path: Path) -> None:
    backupctl = _load_backupctl()
    staging = tmp_path / "pending"
    staging.mkdir()
    (staging / "database.dump").write_bytes(b"not-empty")
    (staging / "business.snapshot").write_bytes(b"not-empty")
    published = tmp_path / "published"

    with pytest.raises(RuntimeError, match="reference validation"):
        backupctl.atomic_publish_local(
            staging,
            published,
            _manifest(),
            lambda: (_ for _ in ()).throw(RuntimeError("reference validation failed")),
        )
    assert not published.exists()
    assert not (staging / "manifest.json").exists()
    assert not (staging / "COMPLETE").exists()

    backupctl.atomic_publish_local(staging, published, _manifest(), lambda: None)
    assert json.loads((published / "manifest.json").read_text(encoding="utf-8")) == _manifest()
    assert (published / "COMPLETE").read_text(encoding="utf-8").strip()
    assert not staging.exists()


def test_atomic_publish_lease_allows_only_one_concurrent_writer(tmp_path: Path) -> None:
    backupctl = _load_backupctl()
    published = tmp_path / "published" / _manifest()["backup_run_id"]
    staging = []
    for label in ("first", "second"):
        path = tmp_path / label
        path.mkdir()
        (path / "database.dump").write_bytes(label.encode())
        (path / "business.snapshot").write_bytes(label.encode())
        (path / "writer").write_text(label, encoding="ascii")
        staging.append(path)

    def publish(path: Path) -> str:
        backupctl.atomic_publish_local(path, published, _manifest(), lambda: None)
        return path.name

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(publish, path) for path in staging]
    successes = [future.result() for future in futures if future.exception() is None]
    failures = [future.exception() for future in futures if future.exception() is not None]
    assert len(successes) == 1
    assert len(failures) == 1
    assert isinstance(failures[0], FileExistsError)
    winner = (published / "writer").read_text(encoding="ascii")
    assert winner == successes[0]
    assert (published / "manifest.json").is_file()
    assert (published / "COMPLETE").is_file()


def test_atomic_publisher_receipt_is_strict_and_run_hash_bound(tmp_path: Path) -> None:
    backupctl = _load_backupctl()
    receipt = tmp_path / "receipt.json"
    value = {
        "schema_version": 1,
        "status": "committed",
        "backup_run_id": "business-run-safe1",
        "complete_sha256": "a" * 64,
        "lease_id_hash": "b" * 64,
    }
    backupctl._write_json(receipt, value)
    backupctl.validate_publish_receipt(receipt, "business-run-safe1", "a" * 64)
    for broken in (
        {**value, "status": "copied"},
        {**value, "backup_run_id": "business-run-other1"},
        {**value, "complete_sha256": "c" * 64},
        {**value, "lease_id_hash": "not-a-hash"},
    ):
        backupctl._write_json(receipt, broken)
        with pytest.raises(ValueError, match="publisher|receipt|bound|hash"):
            backupctl.validate_publish_receipt(receipt, "business-run-safe1", "a" * 64)


def test_s3_publisher_uses_one_conditional_lease_and_complete_last(tmp_path: Path, monkeypatch) -> None:
    publisher = _load_publisher()
    run_id = "business-run-publisher1"
    source = _write_publisher_source(tmp_path, run_id)
    config = tmp_path / "config.json"
    _write_test_mc_config(config)
    receipt = tmp_path / "receipt.json"
    store = _FakeConditionalStore(publisher)
    monkeypatch.setattr(publisher, "_execute_mc", store)

    assert publisher.main(_publisher_args(config, source, receipt, run_id)) == 0
    backupctl = _load_backupctl()
    complete_hash = hashlib.sha256((source / "manifest.json").read_bytes()).hexdigest()
    backupctl.validate_publish_receipt(receipt, run_id, complete_hash)

    lease_commands = [command for command in store.commands if "pipe" in command]
    assert len(lease_commands) == 1
    assert "If-None-Match:*" in lease_commands[0]
    assert not any("ls" in command or "find" in command for command in store.commands)
    put_targets = [command[-1] for command in store.commands if "put" in command]
    assert put_targets[-1].endswith("/COMPLETE")
    assert all(not target.endswith("/COMPLETE") for target in put_targets[:-1])
    assert hashlib.sha256(store.objects[lease_commands[0][-1]]).hexdigest() == json.loads(
        receipt.read_text(encoding="utf-8")
    )["lease_id_hash"]


def test_s3_publisher_passes_in_memory_lease_as_subprocess_input(tmp_path: Path, monkeypatch) -> None:
    publisher = _load_publisher()
    config_dir = tmp_path / "mc"
    config_dir.mkdir()
    observed = {}

    def fake_run(*args, **kwargs):
        observed.update(kwargs)
        return subprocess.CompletedProcess(args[0], 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(publisher.subprocess, "run", fake_run)
    publisher._execute_mc(
        [publisher.MC_BIN, "-C", str(config_dir), "pipe", "provider/bucket/private-lease"],
        stdin=io.BytesIO(b"lease-bytes"),
    )
    assert observed["input"] == b"lease-bytes"
    assert "stdin" not in observed


@pytest.mark.parametrize(
    ("operation", "provider_code", "expected"),
    [
        ("stat", "NoSuchKey", "ObjectMissing"),
        ("pipe", "PreconditionFailed", "LeaseConflict"),
        ("pipe", "ConditionalRequestConflict", "LeaseConflict"),
    ],
)
def test_s3_publisher_classifies_mc_json_errors_from_stdout(
    tmp_path: Path, monkeypatch, operation: str, provider_code: str, expected: str
) -> None:
    publisher = _load_publisher()
    config_dir = tmp_path / "mc"
    config_dir.mkdir()
    payload = json.dumps({"status": "error", "error": {"cause": {"error": {"Code": provider_code}}}}).encode()
    monkeypatch.setattr(
        publisher.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 1, stdout=payload, stderr=b""),
    )
    error_type = getattr(publisher, expected)
    with pytest.raises(error_type):
        publisher._execute_mc(
            [publisher.MC_BIN, "-C", str(config_dir), operation, "provider/bucket/private-object"],
            capture_stdout=True,
        )


def test_s3_publisher_partial_upload_stays_invisible_and_lease_is_permanent(
    tmp_path: Path, monkeypatch
) -> None:
    publisher = _load_publisher()
    run_id = "business-run-partial01"
    source = _write_publisher_source(tmp_path, run_id)
    config = tmp_path / "config.json"
    _write_test_mc_config(config)
    receipt = tmp_path / "receipt.json"
    store = _FakeConditionalStore(publisher, fail_upload_number=2)
    monkeypatch.setattr(publisher, "_execute_mc", store)

    assert publisher.main(_publisher_args(config, source, receipt, run_id)) == publisher.EXIT_PROVIDER_FAILURE
    assert not receipt.exists()
    assert not any(target.endswith("/COMPLETE") for target in store.objects)
    lease_target = next(command[-1] for command in store.commands if "pipe" in command)
    assert lease_target in store.objects

    store.fail_upload_number = None
    immutable = dict(store.objects)
    assert publisher.main(_publisher_args(config, source, receipt, run_id)) == publisher.EXIT_CONFLICT
    assert store.objects == immutable
    assert not receipt.exists()


def test_s3_publisher_rejects_payload_tamper_against_complete_manifest(tmp_path: Path, monkeypatch) -> None:
    publisher = _load_publisher()
    run_id = "business-run-tamper001"
    source = _write_publisher_source(tmp_path, run_id)
    (source / "database.dump").write_bytes(b"tampered-after-complete")
    config = tmp_path / "config.json"
    _write_test_mc_config(config)
    calls = []
    monkeypatch.setattr(publisher, "_execute_mc", lambda *args, **kwargs: calls.append((args, kwargs)))

    assert publisher.main(_publisher_args(config, source, tmp_path / "receipt.json", run_id)) == publisher.EXIT_SAFETY
    assert not calls


@pytest.mark.parametrize("kind", ["bad-run", "bad-destination", "unexpected", "invalid-complete", "source-symlink"])
def test_s3_publisher_rejects_malformed_sources_before_provider_call(
    tmp_path: Path, monkeypatch, kind: str
) -> None:
    publisher = _load_publisher()
    run_id = "business-run-input001"
    source = _write_publisher_source(tmp_path, run_id)
    config = tmp_path / "config.json"
    _write_test_mc_config(config)
    receipt = tmp_path / "receipt.json"
    args = _publisher_args(config, source, receipt, run_id)
    if kind == "bad-run":
        args[args.index("--run-id") + 1] = "../escape"
    elif kind == "bad-destination":
        args[args.index("--destination") + 1] = "s3://user:secret@example.test/vault"
    elif kind == "unexpected":
        (source / "operator-notes.txt").write_text("not allowed", encoding="utf-8")
    elif kind == "invalid-complete":
        (source / "COMPLETE").write_bytes(("0" * 64 + "\n").encode("ascii"))
    else:
        linked_source = tmp_path / "linked-source"
        try:
            linked_source.symlink_to(source, target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("directory symlink creation is unavailable")
        args[args.index("--source") + 1] = str(linked_source)
    calls = []
    monkeypatch.setattr(publisher, "_execute_mc", lambda *args, **kwargs: calls.append((args, kwargs)))

    assert publisher.main(args) == publisher.EXIT_SAFETY
    assert not calls
    assert not receipt.exists()


@pytest.mark.parametrize("kind", ["permissive", "hardlink", "symlink"])
def test_s3_publisher_rejects_unprotected_or_aliased_config(
    tmp_path: Path, monkeypatch, kind: str
) -> None:
    publisher = _load_publisher()
    run_id = "business-run-config001"
    source = _write_publisher_source(tmp_path, run_id)
    original = tmp_path / "original-config.json"
    _write_test_mc_config(original)
    config = original
    if kind == "permissive":
        if os.name == "nt":
            pytest.skip("Windows does not expose POSIX group/world mode bits")
        original.chmod(0o640)
    elif kind == "hardlink":
        config = tmp_path / "linked-config.json"
        os.link(original, config)
    else:
        config = tmp_path / "symlink-config.json"
        try:
            config.symlink_to(original)
        except (OSError, NotImplementedError):
            pytest.skip("file symlink creation is unavailable")
    calls = []
    monkeypatch.setattr(publisher, "_execute_mc", lambda *args, **kwargs: calls.append((args, kwargs)))

    assert publisher.main(_publisher_args(config, source, tmp_path / "receipt.json", run_id)) == publisher.EXIT_SAFETY
    assert not calls


@pytest.mark.skipif(os.name == "nt", reason="POSIX dir_fd/openat race contract")
def test_s3_publisher_snapshots_member_before_in_place_source_mutation(tmp_path: Path, monkeypatch) -> None:
    publisher = _load_publisher()
    assert hasattr(publisher, "_copy_source_member"), "source members must be copied through pinned dir_fd"
    run_id = "business-run-inplace01"
    original_payload = b"before-in-place-mutation"
    source = _write_publisher_source(tmp_path, run_id, original_payload)
    config = tmp_path / "config.json"
    _write_test_mc_config(config)
    store = _FakeConditionalStore(publisher)
    monkeypatch.setattr(publisher, "_execute_mc", store)
    original_copy = publisher._copy_source_member
    mutated = False

    def copy_then_mutate(source_fd: int, name: str, destination: Path):
        nonlocal mutated
        result = original_copy(source_fd, name, destination)
        if name == "database.dump":
            (source / name).write_bytes(b"after-in-place-mutation")
            mutated = True
        return result

    monkeypatch.setattr(publisher, "_copy_source_member", copy_then_mutate)
    assert publisher.main(_publisher_args(config, source, tmp_path / "receipt.json", run_id)) == 0
    assert mutated
    remote = next(value for key, value in store.objects.items() if key.endswith("/database.dump"))
    assert remote == original_payload


@pytest.mark.skipif(os.name == "nt", reason="POSIX dir_fd/openat race contract")
def test_s3_publisher_pins_source_directory_before_path_replacement(tmp_path: Path, monkeypatch) -> None:
    publisher = _load_publisher()
    assert hasattr(publisher, "_open_source_directory"), "source directory must have a pinned descriptor"
    run_id = "business-run-dirreplace1"
    original_payload = b"pinned-directory-payload"
    source = _write_publisher_source(tmp_path / "original", run_id, original_payload)
    attacker = _write_publisher_source(tmp_path / "attacker", run_id, b"replacement-directory-payload")
    config = tmp_path / "config.json"
    _write_test_mc_config(config)
    store = _FakeConditionalStore(publisher)
    monkeypatch.setattr(publisher, "_execute_mc", store)
    original_open = publisher._open_source_directory
    detached = source.with_name("detached-source")
    replaced = False

    def open_then_replace(path: Path) -> int:
        nonlocal replaced
        descriptor = original_open(path)
        source.rename(detached)
        shutil.copytree(attacker, source)
        replaced = True
        return descriptor

    monkeypatch.setattr(publisher, "_open_source_directory", open_then_replace)
    assert publisher.main(_publisher_args(config, source, tmp_path / "receipt.json", run_id)) == 0
    assert replaced
    remote = next(value for key, value in store.objects.items() if key.endswith("/database.dump"))
    assert remote == original_payload


@pytest.mark.skipif(os.name == "nt", reason="POSIX dir_fd/openat race contract")
def test_s3_publisher_pins_each_ancestor_before_symlink_swap(tmp_path: Path, monkeypatch) -> None:
    publisher = _load_publisher()
    run_id = "business-run-ancestor01"
    tree = tmp_path / "tree"
    ancestor = tree / "ancestor"
    source = _write_publisher_source(ancestor, run_id, b"pinned-ancestor-payload")
    attacker_parent = tmp_path / "attacker-parent"
    _write_publisher_source(attacker_parent, run_id, b"symlink-target-payload")
    detached = tree / "detached-ancestor"
    config = tmp_path / "config.json"
    _write_test_mc_config(config)
    store = _FakeConditionalStore(publisher)
    monkeypatch.setattr(publisher, "_execute_mc", store)
    original_os_open = publisher.os.open
    swapped = False

    def open_and_swap(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal swapped
        descriptor = original_os_open(path, flags, mode, dir_fd=dir_fd)
        if path == "ancestor" and dir_fd is not None and not swapped:
            ancestor.rename(detached)
            ancestor.symlink_to(attacker_parent, target_is_directory=True)
            swapped = True
        return descriptor

    monkeypatch.setattr(publisher.os, "open", open_and_swap)
    assert publisher.main(_publisher_args(config, source, tmp_path / "receipt.json", run_id)) == 0
    assert swapped, "the ancestor path was not opened component-by-component"
    remote = next(value for key, value in store.objects.items() if key.endswith("/database.dump"))
    assert remote == b"pinned-ancestor-payload"


@pytest.mark.skipif(os.name == "nt", reason="POSIX dir_fd/openat race contract")
def test_s3_publisher_receipt_target_creation_after_parent_open_is_commit_unknown(
    tmp_path: Path, monkeypatch
) -> None:
    publisher = _load_publisher()
    assert hasattr(publisher, "_open_receipt_parent"), "receipt parent must have a pinned descriptor"
    run_id = "business-run-receiptrace1"
    source = _write_publisher_source(tmp_path, run_id)
    config = tmp_path / "config.json"
    _write_test_mc_config(config)
    receipt_dir = tmp_path / "receipts"
    receipt_dir.mkdir()
    receipt = receipt_dir / "receipt.json"
    store = _FakeConditionalStore(publisher)
    monkeypatch.setattr(publisher, "_execute_mc", store)
    original_open = publisher._open_receipt_parent

    def open_then_create(path: Path) -> int:
        descriptor = original_open(path)
        attacker = os.open(receipt.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=descriptor)
        os.write(attacker, b"attacker-owned")
        os.close(attacker)
        return descriptor

    monkeypatch.setattr(publisher, "_open_receipt_parent", open_then_create)
    assert publisher.main(_publisher_args(config, source, receipt, run_id)) == publisher.EXIT_COMMIT_UNKNOWN
    assert receipt.read_bytes() == b"attacker-owned"
    assert any(key.endswith("/COMPLETE") for key in store.objects)


@pytest.mark.skipif(os.name == "nt", reason="POSIX dir_fd/openat race contract")
def test_s3_publisher_receipt_parent_replacement_writes_only_to_pinned_directory(
    tmp_path: Path, monkeypatch
) -> None:
    publisher = _load_publisher()
    assert hasattr(publisher, "_open_receipt_parent"), "receipt parent must have a pinned descriptor"
    run_id = "business-run-parentrace1"
    source = _write_publisher_source(tmp_path, run_id)
    config = tmp_path / "config.json"
    _write_test_mc_config(config)
    receipt_dir = tmp_path / "receipts"
    receipt_dir.mkdir()
    detached = tmp_path / "detached-receipts"
    receipt = receipt_dir / "receipt.json"
    store = _FakeConditionalStore(publisher)
    monkeypatch.setattr(publisher, "_execute_mc", store)
    original_open = publisher._open_receipt_parent
    replaced = False

    def open_then_replace(path: Path) -> int:
        nonlocal replaced
        descriptor = original_open(path)
        receipt_dir.rename(detached)
        receipt_dir.mkdir()
        replaced = True
        return descriptor

    monkeypatch.setattr(publisher, "_open_receipt_parent", open_then_replace)
    assert publisher.main(_publisher_args(config, source, receipt, run_id)) == 0
    assert replaced
    assert (detached / receipt.name).is_file()
    assert not receipt.exists()


@pytest.mark.parametrize("failure", ["complete-put", "complete-stat", "complete-get", "receipt"])
def test_s3_publisher_failures_at_or_after_complete_are_commit_unknown(
    tmp_path: Path, monkeypatch, failure: str
) -> None:
    publisher = _load_publisher()
    run_id = "business-run-unknown01"
    source = _write_publisher_source(tmp_path, run_id)
    config = tmp_path / "config.json"
    _write_test_mc_config(config)
    receipt = tmp_path / "receipt.json"
    if failure == "receipt":
        receipt.write_bytes(b"preexisting")
    store = _FakeConditionalStore(publisher)

    def fail_after_complete(command, *, stdin=None, capture_stdout=False):
        args = tuple(command)
        operation = next(item for item in ("pipe", "put", "stat", "get") if item in args)
        target = args[-2] if operation == "get" else args[-1]
        if failure == "complete-put" and operation == "put" and target.endswith("/COMPLETE"):
            store(command, stdin=stdin, capture_stdout=capture_stdout)
            raise publisher.ProviderFailure()
        complete_exists = any(key.endswith("/COMPLETE") for key in store.objects)
        if complete_exists and failure == f"complete-{operation}" and target.endswith("/COMPLETE"):
            raise publisher.ProviderFailure()
        return store(command, stdin=stdin, capture_stdout=capture_stdout)

    monkeypatch.setattr(publisher, "_execute_mc", fail_after_complete)
    assert publisher.main(_publisher_args(config, source, receipt, run_id)) == publisher.EXIT_COMMIT_UNKNOWN
    assert any(key.endswith("/COMPLETE") for key in store.objects)


def test_s3_publisher_read_only_reconciliation_recovers_bound_receipt(tmp_path: Path, monkeypatch) -> None:
    publisher = _load_publisher()
    run_id = "business-run-reconcile1"
    source = _write_publisher_source(tmp_path, run_id)
    config = tmp_path / "config.json"
    _write_test_mc_config(config)
    initial_receipt = tmp_path / "initial-receipt.json"
    store = _FakeConditionalStore(publisher)
    monkeypatch.setattr(publisher, "_execute_mc", store)
    assert publisher.main(_publisher_args(config, source, initial_receipt, run_id)) == 0
    initial_receipt.unlink()
    store.commands.clear()

    reconciled = tmp_path / "reconciled-receipt.json"
    assert publisher.main(_reconcile_args(config, reconciled, run_id)) == 0
    complete_hash = hashlib.sha256((source / "manifest.json").read_bytes()).hexdigest()
    _load_backupctl().validate_publish_receipt(reconciled, run_id, complete_hash)
    assert store.commands
    assert not any("pipe" in command or "put" in command for command in store.commands)


@pytest.mark.parametrize(
    ("destination", "app_host", "forbidden_path"),
    [
        ("/srv/backups", "app.example.test", "/srv/backups"),
        ("file:///offhost-looking", "app.example.test", "/var/lib/postgresql/data"),
        ("ssh://app.example.test/vault", "app.example.test", "/var/lib/postgresql/data"),
        ("ssh://backup.example.test/var/lib/postgresql/data", "app.example.test", "/var/lib/postgresql/data"),
        ("s3://postgres/var/lib/minio/data", "app.example.test", "/var/lib/minio/data"),
    ],
)
def test_off_host_destination_fails_closed_for_app_host_and_data_paths(
    destination: str, app_host: str, forbidden_path: str
) -> None:
    backupctl = _load_backupctl()
    with pytest.raises(ValueError, match="off-host|forbidden|application host"):
        backupctl.validate_off_host_destination(destination, app_host, [forbidden_path])
    assert backupctl.validate_off_host_destination(
        "s3://independent-backup-vault/ux09", app_host, [forbidden_path]
    ) == "s3://independent-backup-vault/ux09"


@pytest.mark.parametrize(
    "destination",
    [
        "s3://user:password@backup.example.test/ux09",
        "s3://backup.example.test/ux09?token=value",
        "s3://backup.example.test/ux09#fragment",
        "http://backup.example.test/ux09",
        "ftp://backup.example.test/ux09",
        "ssh://backup.example.test/ux09",
        "javascript://backup.example.test/ux09",
        "s3://backup.example.test/ux09/%2e%2e/data",
        "s3://backup.example.test/ux09\\..\\data",
    ],
)
def test_destination_uri_rejects_userinfo_query_fragment_and_dangerous_schemes(destination: str) -> None:
    backupctl = _load_backupctl()
    with pytest.raises(ValueError, match="scheme|userinfo|query|fragment|destination|path"):
        backupctl.validate_off_host_destination(destination, "app.example.test", [])


def test_secret_files_are_required_distinct_and_never_serialized(tmp_path: Path) -> None:
    backupctl = _load_backupctl()
    secret_files = []
    for name in (
        "pgpass",
        "business-source.conf",
        "business-destination.conf",
        "business-prune.conf",
        "business-restore.conf",
        "ledger-source.conf",
        "ledger-destination.conf",
        "ledger-restore.conf",
        "manifest-signing-key",
    ):
        path = tmp_path / name
        path.write_text(f"synthetic-{name}-credential", encoding="utf-8")
        path.chmod(0o600)
        secret_files.append(path)

    backupctl.validate_secret_files(secret_files)
    with pytest.raises(ValueError, match="distinct"):
        backupctl.validate_secret_files([secret_files[0], secret_files[0]])

    serialized = json.dumps(_manifest(), sort_keys=True)
    for path in secret_files:
        assert path.read_text(encoding="utf-8") not in serialized


def test_secret_files_reject_symlink_hardlink_nonregular_and_wide_permissions(tmp_path: Path) -> None:
    backupctl = _load_backupctl()
    original = tmp_path / "secret"
    original.write_bytes(b"x" * 32)
    original.chmod(0o600)
    hardlink = tmp_path / "hardlink"
    os.link(original, hardlink)
    with pytest.raises(ValueError, match="hardlink|link|inode"):
        backupctl.validate_secret_files([original])

    standalone = tmp_path / "standalone"
    standalone.write_bytes(b"y" * 32)
    standalone.chmod(0o644)
    if os.name != "nt":
        with pytest.raises(ValueError, match="permission"):
            backupctl.validate_secret_files([standalone])

    with pytest.raises(ValueError, match="regular"):
        backupctl.validate_secret_files([tmp_path])

    symlink = tmp_path / "symlink"
    try:
        symlink.symlink_to(standalone)
    except OSError:
        pytest.skip("symlink creation is unavailable")
    with pytest.raises(ValueError, match="symlink|link"):
        backupctl.validate_secret_files([symlink])


def test_secret_snapshot_uses_private_regular_copies_and_closes_tocotu(tmp_path: Path) -> None:
    backupctl = _load_backupctl()
    source = tmp_path / "source"
    source.write_bytes(b"z" * 32)
    source.chmod(0o600)
    private_root = tmp_path / "private"
    with backupctl.secure_secret_copies([source], private_root) as copies:
        assert len(copies) == 1
        copied = copies[0]
        assert copied.read_bytes() == b"z" * 32
        assert copied != source
        if os.name != "nt":
            assert copied.stat().st_mode & 0o077 == 0
        assert not copied.is_symlink()
    assert not private_root.exists()


def test_child_environment_is_strict_allowlist_and_fake_child_sees_only_private_secret_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backupctl = _load_backupctl()
    malicious = {
        "PGPASSWORD": "pg-password-value",
        "AWS_ACCESS_KEY_ID": "aws-access-value",
        "AWS_SECRET_ACCESS_KEY": "aws-secret-value",
        "AWS_SESSION_TOKEN": "aws-token-value",
        "RCLONE_CONFIG_PASS": "rclone-password-value",
        "DATABASE_PASSWORD": "database-password-value",
        "API_TOKEN": "api-token-value",
        "SIGNING_KEY": "signing-key-value",
        "CREDENTIAL_BLOB": "credential-value",
        "UNRELATED_INHERITED_VALUE": "must-not-pass",
    }
    for name, value in malicious.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("LANG", "C.UTF-8")
    environment = backupctl._sanitized_child_environment(
        runtime_values={"MINIO_ALIAS": "business-source"},
        private_secret_paths={"PGPASSFILE": "/private/secret-0"},
    )
    assert not set(malicious).intersection(environment)
    assert environment["PGPASSFILE"] == "/private/secret-0"
    assert environment["MINIO_ALIAS"] == "business-source"
    assert environment["LANG"] == "C.UTF-8"
    child = subprocess.run(
        [sys.executable, "-c", "import json,os; print(json.dumps(dict(os.environ), sort_keys=True))"],
        capture_output=True,
        text=True,
        check=True,
        env=environment,
    )
    observed = json.loads(child.stdout)
    assert not set(malicious).intersection(observed)
    assert observed["PGPASSFILE"] == "/private/secret-0"
    assert observed["MINIO_ALIAS"] == "business-source"
    assert not any(value in child.stdout for value in malicious.values())
    with pytest.raises(ValueError, match="non-sensitive"):
        backupctl._sanitized_child_environment(runtime_values={"API_TOKEN": "must-not-pass"})
    with pytest.raises(ValueError, match="alias"):
        backupctl.validate_minio_alias("https://user:secret@example.test")


def test_prune_deletes_complete_expired_groups_and_preserves_newest_two() -> None:
    backupctl = _load_backupctl()
    catalog = [
        _catalog_entry("run-1", "2026-05-01T00:00:00Z", complete_order=1),
        _catalog_entry("run-2", "2026-05-15T00:00:00Z", complete_order=2),
        _catalog_entry("run-3", "2026-07-01T00:00:00Z", complete_order=3),
        _catalog_entry("run-4", "2026-07-14T00:00:00Z", complete_order=4),
        _catalog_entry("pending-old", "2026-04-01T00:00:00Z", complete=False, valid=False, complete_order=0),
    ]
    assert backupctl.plan_prune(
        catalog,
        retention_days=30,
        now=datetime(2026, 7, 15, tzinfo=timezone.utc),
    ) == ["run-1", "run-2"]


def test_prune_fails_closed_on_policy_mismatch_or_invalid_latest() -> None:
    backupctl = _load_backupctl()
    base = [
        _catalog_entry("run-1", "2026-05-01T00:00:00Z", complete_order=1),
        _catalog_entry("run-2", "2026-07-01T00:00:00Z", complete_order=2),
        _catalog_entry("run-3", "2026-07-14T00:00:00Z", complete_order=3),
    ]
    mismatch = [*base[:-1], {**base[-1], "backup_window_days": 31}]
    invalid_latest = [*base[:-1], {**base[-1], "valid": False}]
    for catalog in (mismatch, invalid_latest):
        with pytest.raises(ValueError, match="policy|latest|valid"):
            backupctl.plan_prune(
                catalog,
                retention_days=30,
                now=datetime(2026, 7, 15, tzinfo=timezone.utc),
            )


def test_prune_uses_complete_order_and_never_downgrades_invalid_latest() -> None:
    backupctl = _load_backupctl()
    catalog = [
        _catalog_entry("run-new-by-cutoff", "2026-07-15T00:00:00Z", complete_order=2),
        _catalog_entry("run-latest-complete", "2026-07-14T00:00:00Z", complete_order=3, valid=False),
        _catalog_entry("run-old", "2026-05-01T00:00:00Z", complete_order=1),
    ]
    with pytest.raises(ValueError, match="latest"):
        backupctl.plan_prune(catalog, 30, datetime(2026, 7, 16, tzinfo=timezone.utc))


def test_ledger_archive_is_separate_and_business_identity_has_no_ledger_mutation() -> None:
    backupctl = _load_backupctl()
    history = {
        "schema_version": 1,
        "active_key_version": "ledger-v2",
        "versions": [
            {"version": "ledger-v1", "status": "retired"},
            {"version": "ledger-v2", "status": "active"},
        ],
    }
    backupctl.validate_ledger_boundary(
        business_buckets=["resumes", "exports"],
        ledger_bucket="governance-ledger",
        business_capabilities={"business:read", "destination:append"},
        business_destination="s3://business-vault/ux09",
        ledger_destination="s3://ledger-vault/ux09",
        key_history=history,
    )
    with pytest.raises(ValueError):
        backupctl.validate_ledger_boundary(
            business_buckets=["resumes", "governance-ledger"],
            ledger_bucket="governance-ledger",
            business_capabilities={"ledger:restore", "ledger:delete"},
            business_destination="s3://same-vault/ux09",
            ledger_destination="s3://same-vault/ux09",
            key_history={"schema_version": 1, "active_key_version": "unversioned", "versions": []},
        )


def test_restore_uses_latest_independent_ledger_not_business_cutoff() -> None:
    source = BACKUPCTL.read_text(encoding="utf-8")
    assert '"select-latest-complete"' in source
    assert '"restore-latest-before"' not in source


def _write_signed_ledger_group(tmp_path: Path, backupctl, *, cutoff: str = "2026-07-15T01:00:00Z") -> tuple[Path, Path, dict]:
    group = tmp_path / "ledger-run-safe1"
    group.mkdir(parents=True)
    key = tmp_path / "ledger-key"
    key.write_bytes(b"l" * 32)
    key.chmod(0o600)
    archive = group / "ledger.snapshot"
    archive.write_bytes(b"signed-ledger-archive")
    manifest = {
        "schema_version": 1,
        "archive_run_id": group.name,
        "cutoff_utc": cutoff,
        "archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
        "size_bytes": archive.stat().st_size,
        "entry_count": 2,
        "signing_key_version": "ledger-v2",
        "lifecycle_policy_version": "ledger-lifecycle-v1",
    }
    manifest_path = group / "ledger-manifest.json"
    manifest_path.write_bytes(backupctl._canonical_json(manifest))
    backupctl.write_hmac_signature(manifest_path, key, group / "ledger-manifest.sig")
    (group / "COMPLETE").write_text(hashlib.sha256(manifest_path.read_bytes()).hexdigest() + "\n", encoding="ascii")
    return group, key, manifest


def test_ledger_consumer_validates_schema_signature_complete_hash_freshness_and_binding(tmp_path: Path) -> None:
    backupctl = _load_backupctl()
    group, key, manifest = _write_signed_ledger_group(tmp_path, backupctl)
    history = {
        "schema_version": 1,
        "active_key_version": "ledger-v2",
        "versions": [{"version": "ledger-v1", "status": "retired"}, {"version": "ledger-v2", "status": "active"}],
    }
    validated = backupctl.validate_ledger_archive_group(
        group, key, history, minimum_cutoff_utc="2026-07-15T00:00:00Z"
    )
    assert validated == manifest

    (group / "COMPLETE").write_text("0" * 64 + "\n", encoding="ascii")
    with pytest.raises(ValueError, match="COMPLETE"):
        backupctl.validate_ledger_archive_group(group, key, history, minimum_cutoff_utc="2026-07-15T00:00:00Z")

    group, key, _ = _write_signed_ledger_group(tmp_path / "stale", backupctl, cutoff="2026-07-14T00:00:00Z")
    with pytest.raises(ValueError, match="fresh"):
        backupctl.validate_ledger_archive_group(group, key, history, minimum_cutoff_utc="2026-07-15T00:00:00Z")


def test_backup_pairing_accepts_only_strictly_verified_ledger_group(tmp_path: Path) -> None:
    backupctl = _load_backupctl()
    history = {
        "schema_version": 1,
        "active_key_version": "ledger-v2",
        "versions": [{"version": "ledger-v1", "status": "retired"}, {"version": "ledger-v2", "status": "active"}],
    }
    group, key, manifest = _write_signed_ledger_group(tmp_path / "valid", backupctl)
    evidence = backupctl.validate_ledger_pairing(
        group,
        key,
        history,
        business_run_id="business-run-safe1",
        business_cutoff_utc="2026-07-15T00:00:00Z",
    )
    assert evidence["archive_run_id"] == manifest["archive_run_id"]
    assert evidence["cutoff_utc"] == manifest["cutoff_utc"]
    assert evidence["manifest_sha256"] == hashlib.sha256((group / "ledger-manifest.json").read_bytes()).hexdigest()

    bare = tmp_path / "bare" / "ledger-run-bare1"
    bare.mkdir(parents=True)
    (bare / "ledger-manifest.json").write_bytes(backupctl._canonical_json(manifest))
    with pytest.raises((OSError, ValueError), match="ledger|archive|signature|COMPLETE|manifest"):
        backupctl.validate_ledger_pairing(
            bare, key, history, business_run_id="business-run-safe1", business_cutoff_utc="2026-07-15T00:00:00Z"
        )

    group, key, _ = _write_signed_ledger_group(tmp_path / "missing-archive", backupctl)
    (group / "ledger.snapshot").unlink()
    with pytest.raises((OSError, ValueError), match="archive|size|hash"):
        backupctl.validate_ledger_pairing(
            group, key, history, business_run_id="business-run-safe1", business_cutoff_utc="2026-07-15T00:00:00Z"
        )


def test_backup_command_requires_dedicated_ledger_verify_key_and_verified_group() -> None:
    source = BACKUPCTL.read_text(encoding="utf-8")
    backup = source.split("def command_backup", 1)[1].split("def command_prune", 1)[0]
    assert "LEDGER_MANIFEST_VERIFY_KEY_FILE" in backup
    assert "LEDGER_PAIRING_GROUP_PATH" in backup
    assert "validate_ledger_pairing" in backup
    assert "_load_json(ledger_manifest_path)" not in backup


def test_ledger_restore_proof_is_signed_and_bound_to_run_and_generation(tmp_path: Path) -> None:
    backupctl = _load_backupctl()
    group, key, manifest = _write_signed_ledger_group(tmp_path, backupctl)
    proof = {
        "schema_version": 1,
        "status": "verified",
        "ledger_archive_run_id": manifest["archive_run_id"],
        "business_backup_run_id": "business-run-safe1",
        "recovery_generation_id": "generation-safe1",
        "archive_sha256": manifest["archive_sha256"],
        "cutoff_utc": manifest["cutoff_utc"],
        "restored_entry_count": manifest["entry_count"],
    }
    proof_path = tmp_path / "proof.json"
    proof_path.write_bytes(backupctl._canonical_json(proof))
    signature = tmp_path / "proof.sig"
    backupctl.write_hmac_signature(proof_path, key, signature)
    backupctl.validate_ledger_restore_proof(
        proof_path, signature, key, manifest, "business-run-safe1", "generation-safe1"
    )
    replay = {**proof, "business_backup_run_id": "business-run-old11"}
    proof_path.write_bytes(backupctl._canonical_json(replay))
    backupctl.write_hmac_signature(proof_path, key, signature)
    with pytest.raises(ValueError, match="binding"):
        backupctl.validate_ledger_restore_proof(
            proof_path, signature, key, manifest, "business-run-safe1", "generation-safe1"
        )


@pytest.mark.parametrize(
    ("project", "volumes", "confirmed"),
    [
        ("ux09", ["ux09_postgres-data"], "1"),
        ("ux09-production", ["ux09-production-postgres-data"], "1"),
        ("ux09-backup-drill-a1", ["ux09_postgres-data"], "1"),
        ("ux09-backup-drill-a1", ["ux09-backup-drill-a1-postgres-data"], "0"),
    ],
)
def test_restore_and_drill_require_disposable_project_and_volumes(
    project: str, volumes: list[str], confirmed: str
) -> None:
    backupctl = _load_backupctl()
    with pytest.raises(ValueError, match="disposable|production|volume"):
        backupctl.validate_disposable_recovery(project, volumes, confirmed)
    backupctl.validate_disposable_recovery(
        "ux09-backup-drill-safe123",
        ["ux09-backup-drill-safe123-postgres-data", "ux09-backup-drill-safe123-minio-data"],
        "1",
    )


def _signed_b2b3_evidence(tmp_path: Path, backupctl) -> tuple[Path, Path, Path, Path, dict]:
    restore = {
        "schema_version": 1,
        "state": "restored_traffic_closed",
        "backup_run_id": "business-run-safe1",
        "recovery_generation_id": "00000000-0000-4000-8000-000000000001",
        "restore_id": "00000000-0000-4000-8000-000000000001",
        "backup_cutoff_utc": "2026-07-15T00:00:00Z",
        "backup_manifest_sha256": "a" * 64,
        "ledger_manifest_sha256": "b" * 64,
        "traffic_open": False,
    }
    evidence = {
        "schema_version": 1,
        "status": "complete",
        "backup_run_id": restore["backup_run_id"],
        "recovery_generation_id": restore["recovery_generation_id"],
        "restore_id": restore["restore_id"],
        "backup_manifest_sha256": restore["backup_manifest_sha256"],
        "ledger_manifest_sha256": restore["ledger_manifest_sha256"],
        "traffic_open": False,
        "checks": {
            "objects_absent": True,
            "database_redacted": True,
            "ledger_consistent": True,
            "recovery_completed": True,
        },
        "counts": {
            "prepared_redeletions": 1,
            "completed_redeletions": 1,
            "deleted_objects": 2,
        },
    }
    restore_path = tmp_path / "restore.json"
    evidence_path = tmp_path / "b2b3.json"
    signature_path = tmp_path / "b2b3.sig"
    private_key_path = tmp_path / "receiver-private.pem"
    key_path = tmp_path / "b2b3-verify-public.pem"
    restore_path.write_bytes(backupctl._canonical_json(restore))
    evidence_path.write_bytes(backupctl._canonical_json(evidence))
    subprocess.run(
        ["openssl", "genpkey", "-algorithm", "ED25519", "-out", str(private_key_path)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["openssl", "pkey", "-in", str(private_key_path), "-pubout", "-out", str(key_path)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            "openssl",
            "pkeyutl",
            "-sign",
            "-rawin",
            "-inkey",
            str(private_key_path),
            "-in",
            str(evidence_path),
            "-out",
            str(signature_path),
        ],
        check=True,
        capture_output=True,
    )
    key_path.chmod(0o400)
    private_key_path.unlink()
    return restore_path, evidence_path, signature_path, key_path, evidence


def test_signed_b2b3_evidence_proves_redelete_and_is_bound_to_restore(tmp_path: Path) -> None:
    backupctl = _load_backupctl()
    restore_path, evidence_path, signature_path, key_path, evidence = _signed_b2b3_evidence(
        tmp_path, backupctl
    )

    assert backupctl.validate_b2b3_evidence(
        restore_path, evidence_path, signature_path, key_path
    ) == evidence
    assert backupctl.require_traffic_open_evidence(
        json.loads(restore_path.read_text(encoding="utf-8")), evidence
    ) is False

    tampered = {**evidence, "backup_run_id": "business-run-other1"}
    evidence_path.write_bytes(backupctl._canonical_json(tampered))
    with pytest.raises(ValueError, match="signature"):
        backupctl.validate_b2b3_evidence(
            restore_path, evidence_path, signature_path, key_path
        )


def test_traffic_gate_verifies_b2b3_but_only_writes_closed_evidence(tmp_path: Path) -> None:
    backupctl = _load_backupctl()
    restore_path, evidence_path, signature_path, key_path, _ = _signed_b2b3_evidence(
        tmp_path, backupctl
    )
    open_marker = tmp_path / "TRAFFIC_OPEN"
    closed_evidence = tmp_path / "traffic-closed.json"
    open_marker.write_text("stale", encoding="ascii")

    result = subprocess.run(
        [
            sys.executable,
            str(BACKUPCTL),
            "traffic-gate",
            str(restore_path),
            str(evidence_path),
            str(signature_path),
            str(key_path),
            str(open_marker),
            str(closed_evidence),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert not open_marker.exists()
    assert json.loads(closed_evidence.read_text(encoding="utf-8")) == {
        "schema_version": 1,
        "state": "b2b3_verified_traffic_closed",
        "backup_run_id": "business-run-safe1",
        "recovery_generation_id": "00000000-0000-4000-8000-000000000001",
        "restore_id": "00000000-0000-4000-8000-000000000001",
        "backup_manifest_sha256": "a" * 64,
        "ledger_manifest_sha256": "b" * 64,
        "traffic_open": False,
        "production_traffic_decision": "external_environment_required",
    }


def test_drill_failure_evidence_is_safe_bound_and_traffic_closed(tmp_path: Path) -> None:
    backupctl = _load_backupctl()
    restore_path, _, _, _, _ = _signed_b2b3_evidence(tmp_path, backupctl)
    output = tmp_path / "drill-failed.json"
    backupctl.write_drill_failure_evidence(
        output,
        json.loads(restore_path.read_text(encoding="utf-8")),
        "b2b3_worker_failed",
    )
    document = json.loads(output.read_text(encoding="utf-8"))
    assert document["safe_error_code"] == "b2b3_worker_failed"
    assert document["traffic_open"] is False
    assert document["backup_run_id"] == "business-run-safe1"
    assert document["recovery_generation_id"] == "00000000-0000-4000-8000-000000000001"
    assert set(document) == {
        "schema_version",
        "state",
        "backup_run_id",
        "recovery_generation_id",
        "restore_id",
        "backup_manifest_sha256",
        "ledger_manifest_sha256",
        "safe_error_code",
        "traffic_open",
    }


def test_missing_released_b2b3_cli_fails_in_preflight_with_safe_closed_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backupctl = _load_backupctl()
    restore_path, evidence_path, signature_path, key_path, _ = _signed_b2b3_evidence(
        tmp_path, backupctl
    )
    evidence_path.unlink()
    signature_path.unlink()
    closed = tmp_path / "drill-evidence.json"
    monkeypatch.setenv("B2B3_CLI_COMMAND", str(tmp_path / "missing-real-cli"))
    monkeypatch.setenv("B2B3_WORKER_COMMAND", str(tmp_path / "missing-real-worker"))

    with pytest.raises(ValueError, match="preflight|traffic remains closed"):
        backupctl.run_b2b3_protocol(
            restore_path, evidence_path, signature_path, key_path, closed
        )

    failure = json.loads(closed.read_text(encoding="utf-8"))
    assert failure["state"] == "b2b3_failed_traffic_closed"
    assert failure["safe_error_code"] == "b2b3_release_preflight_failed"
    assert failure["traffic_open"] is False
    assert "missing-real-cli" not in closed.read_text(encoding="utf-8")


def test_missing_b2b3_verify_key_still_records_safe_closed_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backupctl = _load_backupctl()
    restore_path, evidence_path, signature_path, key_path, _ = _signed_b2b3_evidence(
        tmp_path, backupctl
    )
    if os.name == "nt":
        key_path.chmod(0o600)
    key_path.unlink()
    closed = tmp_path / "drill-evidence.json"
    cli = tmp_path / "released-cli"
    worker = tmp_path / "released-worker"
    for path in (cli, worker):
        path.write_text("#!/bin/sh\nexit 0\n", encoding="ascii")
        path.chmod(0o555)
    monkeypatch.setenv("B2B3_CLI_COMMAND", str(cli))
    monkeypatch.setenv("B2B3_WORKER_COMMAND", str(worker))
    if os.name == "nt":
        monkeypatch.setattr(
            backupctl, "validate_released_b2b3_commands", lambda *_: None
        )

    with pytest.raises(ValueError, match="preflight|traffic remains closed"):
        backupctl.run_b2b3_protocol(
            restore_path, evidence_path, signature_path, key_path, closed
        )

    failure = json.loads(closed.read_text(encoding="utf-8"))
    assert failure["safe_error_code"] == "b2b3_evidence_preflight_failed"
    assert failure["traffic_open"] is False


def test_restore_evidence_binds_real_b2b3_inputs_without_pending_placeholder() -> None:
    source = BACKUPCTL.read_text(encoding="utf-8")
    restore = source.split("def command_restore", 1)[1].split("def command_ledger_archive", 1)[0]
    assert 'UUID(generation_id)' in restore
    assert '"restore_id": restore_id' in restore
    assert '"backup_manifest_sha256": _sha256_file(workspace / "manifest.json")' in restore
    assert '"ledger_manifest_sha256": _sha256_file(ledger_group / "ledger-manifest.json")' in restore
    assert "pending_real_cli_and_worker_protocol" not in restore


def test_drill_runs_restore_then_released_cli_worker_and_verifies_signed_evidence() -> None:
    source = BACKUPCTL.read_text(encoding="utf-8")
    drill = source.split("def command_drill(", 1)[1].split("def _parser", 1)[0]
    assert drill.index("command_restore") < drill.index("run_b2b3_protocol")
    assert "B2B3_CLI_COMMAND" in source
    assert "B2B3_WORKER_COMMAND" in source
    assert "validate_b2b3_evidence" in source
    assert "does not implement B2B3" not in drill


@pytest.mark.skipif(os.name == "nt", reason="released executable mode contract requires Linux")
def test_released_b2b3_commands_fail_closed_unless_exact_regular_executables(
    tmp_path: Path,
) -> None:
    backupctl = _load_backupctl()
    cli = tmp_path / "b2b3-cli"
    worker = tmp_path / "b2b3-worker"
    cli.write_text("#!/bin/sh\nexit 0\n", encoding="ascii")
    worker.write_text("#!/bin/sh\nexit 0\n", encoding="ascii")
    cli.chmod(0o555)
    worker.chmod(0o555)

    backupctl.validate_released_b2b3_commands(cli, worker)
    worker.chmod(0o444)
    with pytest.raises(ValueError, match="executable|released"):
        backupctl.validate_released_b2b3_commands(cli, worker)


def test_traffic_gate_rejects_handwritten_replayed_and_unbound_evidence_without_marker(tmp_path: Path) -> None:
    marker = tmp_path / "TRAFFIC_OPEN"
    cases = [
        ({"status": "complete", "run_id": "business-run-safe1"}, {"status": "complete"}),
        ({"status": "restored", "run_id": "business-run-old11"}, {"status": "complete", "run_id": "business-run-old11"}),
        ({"status": "restored"}, {"status": "complete"}),
    ]
    for index, (restore, b2b3) in enumerate(cases):
        restore_path = tmp_path / f"restore-{index}.json"
        b2b3_path = tmp_path / f"b2b3-{index}.json"
        restore_path.write_text(json.dumps(restore), encoding="utf-8")
        b2b3_path.write_text(json.dumps(b2b3), encoding="utf-8")
        marker.write_text("stale-open", encoding="ascii")
        result = subprocess.run(
            [
                sys.executable,
                str(BACKUPCTL),
                "traffic-gate",
                str(restore_path),
                str(b2b3_path),
                str(tmp_path / f"missing-{index}.sig"),
                str(tmp_path / f"missing-{index}.key"),
                str(marker),
                str(tmp_path / f"closed-{index}.json"),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 78
        assert "signature" in result.stderr.lower() or "evidence" in result.stderr.lower()
        assert not marker.exists()


def test_restore_start_atomically_closes_traffic_and_binds_run_generation(tmp_path: Path) -> None:
    backupctl = _load_backupctl()
    evidence = tmp_path / "restore-evidence.json"
    closed = tmp_path / "TRAFFIC_CLOSED"
    opened = tmp_path / "TRAFFIC_OPEN"
    opened.write_text("stale", encoding="ascii")
    backupctl.begin_closed_recovery(
        evidence, closed, opened, "business-run-safe1", "generation-safe1"
    )
    assert not opened.exists()
    expected = {
        "schema_version": 1,
        "state": "restore_started_traffic_closed",
        "backup_run_id": "business-run-safe1",
        "recovery_generation_id": "generation-safe1",
        "traffic_open": False,
    }
    assert json.loads(evidence.read_text(encoding="utf-8")) == expected
    assert json.loads(closed.read_text(encoding="utf-8")) == expected
    assert not list(tmp_path.glob("*.tmp-*"))


@pytest.mark.parametrize("fragment", ["/absolute", "../escape", "safe/escape", "..", ".", "C:\\absolute"])
def test_run_ids_and_joined_paths_reject_absolute_and_traversal(fragment: str, tmp_path: Path) -> None:
    backupctl = _load_backupctl()
    with pytest.raises(ValueError, match="run|fragment|path"):
        backupctl.validate_run_id(fragment)
    with pytest.raises(ValueError, match="run|fragment|path"):
        backupctl.safe_run_path(tmp_path, fragment)


def test_joined_path_rejects_existing_symlink_escape(tmp_path: Path) -> None:
    backupctl = _load_backupctl()
    outside = tmp_path.parent / "outside-phase6c"
    outside.mkdir(exist_ok=True)
    link = tmp_path / "symlink-safe1"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation is unavailable")
    with pytest.raises(ValueError, match="escape|symlink|path"):
        backupctl.safe_run_path(tmp_path, "symlink-safe1")


def test_safe_run_path_rejects_root_symlink_on_posix(tmp_path: Path) -> None:
    if os.name == "nt":
        return
    backupctl = _load_backupctl()
    target = tmp_path / "target"
    target.mkdir()
    root_link = tmp_path / "root-link"
    root_link.symlink_to(target, target_is_directory=True)
    with pytest.raises(ValueError, match="root|symlink|reparse|junction"):
        backupctl.safe_run_path(root_link, "child-safe1")


def test_safe_run_path_rejects_windows_root_junction_without_symlink_privilege(tmp_path: Path) -> None:
    if os.name != "nt":
        return
    backupctl = _load_backupctl()
    target = tmp_path / "junction-target"
    target.mkdir()
    junction = tmp_path / "junction-root"
    created = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(target)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert created.returncode == 0, created.stderr or created.stdout
    try:
        with pytest.raises(ValueError, match="root|symlink|reparse|junction"):
            backupctl.safe_run_path(junction, "child-safe1")
    finally:
        os.rmdir(junction)


@pytest.mark.parametrize(
    "raw",
    ["", "resumes,", "resumes,,exports", "../ledger", "resumes/escape", "resumes\\escape", "s3://bucket"],
)
def test_business_bucket_validation_rejects_empty_traversal_separator_and_uri(raw: str) -> None:
    backupctl = _load_backupctl()
    with pytest.raises(ValueError, match="bucket"):
        backupctl.validate_business_buckets(raw)
    assert backupctl.validate_business_buckets("resumes,report-exports") == ["resumes", "report-exports"]


def test_business_buckets_are_validated_before_every_client_call() -> None:
    source = BACKUPCTL.read_text(encoding="utf-8")
    for command_name, next_name in (
        ("command_backup", "command_prune"),
        ("command_restore", "command_ledger_archive"),
        ("command_ledger_archive", "command_drill"),
    ):
        section = source.split(f"def {command_name}", 1)[1].split(f"def {next_name}", 1)[0]
        assert section.index("validate_business_buckets") < section.index("_client("), command_name
    minio = (BACKUP_DIR / "minio-business.sh").read_text(encoding="utf-8")
    assert minio.index("validate-buckets") < minio.index("mc --config-dir")


def test_reference_proof_requires_pinned_schema_fingerprints_and_exact_counts() -> None:
    backupctl = _load_backupctl()
    valid = {
        "schema_version": 1,
        "validator_id": backupctl.REFERENCE_VALIDATOR_ID,
        "query_fingerprint": backupctl.REFERENCE_QUERY_FINGERPRINT,
        "inventory_sha256": "d" * 64,
        "expected": 7,
        "checked": 7,
        "mismatches": 0,
    }
    backupctl.validate_reference_proof(valid, expected=7, inventory_sha256="d" * 64)
    backupctl.validate_reference_proof({**valid, "expected": 0, "checked": 0}, expected=0, inventory_sha256="d" * 64)
    for broken in (
        {key: value for key, value in valid.items() if key != "expected"},
        {**valid, "checked": 6},
        {**valid, "expected": 6, "checked": 6},
        {**valid, "query_fingerprint": "0" * 64},
        {**valid, "inventory_sha256": "0" * 64},
    ):
        with pytest.raises(ValueError, match="reference|expected|fingerprint|checked"):
            backupctl.validate_reference_proof(broken, expected=7, inventory_sha256="d" * 64)


def test_backup_orchestration_uses_bundled_reference_protocol_and_atomic_publisher() -> None:
    source = BACKUPCTL.read_text(encoding="utf-8")
    backup_section = source.split("def command_backup", 1)[1].split("def command_prune", 1)[0]
    assert "REFERENCE_VALIDATOR" not in backup_section
    assert "REFERENCE_QUERY" in backup_section
    assert "build_reference_proof" in backup_section
    assert "BACKUP_ATOMIC_PUBLISHER" in backup_section
    assert "stage-group" not in backup_section
    assert "publish-group" not in backup_section


def test_restore_orchestration_closes_first_and_requires_verified_ledger_proof() -> None:
    source = BACKUPCTL.read_text(encoding="utf-8")
    restore = source.split("def command_restore", 1)[1].split("def command_ledger_archive", 1)[0]
    assert restore.index("begin_closed_recovery") < restore.index("LEDGER_ARCHIVE_CLIENT")
    assert "validate_ledger_archive_group" in restore
    assert "validate_ledger_restore_proof" in restore
    assert "RESTORE_GENERATION_ID" in restore
    assert '"ledger_restored_first": ledger_verified' in restore
    assert '"ledger_restored_first": True' not in restore


def test_orchestration_uses_private_secret_snapshots_not_original_paths() -> None:
    source = BACKUPCTL.read_text(encoding="utf-8")
    for command_name, next_name in (
        ("command_backup", "command_prune"),
        ("command_prune", "command_restore"),
        ("command_restore", "command_ledger_archive"),
        ("command_ledger_archive", "command_drill"),
    ):
        section = source.split(f"def {command_name}", 1)[1].split(f"def {next_name}", 1)[0]
        assert "secure_secret_copies" in section, command_name


def test_invalid_catalog_group_fails_closed_without_epoch_downgrade(tmp_path: Path) -> None:
    backupctl = _load_backupctl()
    group = tmp_path / "broken-run-safe1"
    group.mkdir()
    (group / "manifest.json").write_text("not-json", encoding="utf-8")
    (group / "COMPLETE").write_text("0" * 64, encoding="ascii")
    key = tmp_path / "key"
    key.write_bytes(b"k" * 32)
    key.chmod(0o600)
    with pytest.raises(ValueError, match="catalog|manifest|invalid"):
        backupctl.catalog_from_groups(tmp_path, key, lambda _path: 1)
    assert "1970-01-01" not in BACKUPCTL.read_text(encoding="utf-8")


def _write_complete_business_group(tmp_path: Path, backupctl) -> tuple[Path, Path, dict]:
    root = tmp_path / "groups"
    group = root / "business-run-safe1"
    group.mkdir(parents=True)
    key = tmp_path / "business-signing-key"
    key.write_bytes(b"b" * 32)
    key.chmod(0o600)
    dump = group / "database.dump"
    snapshot = group / "business.snapshot"
    inventory_path = group / "business.inventory.json"
    dump.write_bytes(b"custom-format-dump")
    snapshot.write_bytes(b"business-tar-payload")
    backupctl._write_json(inventory_path, {"schema_version": 1, "objects": []})
    manifest = _manifest(
        backup_run_id=group.name,
        database={
            "format": "custom",
            "sha256": hashlib.sha256(dump.read_bytes()).hexdigest(),
            "size_bytes": dump.stat().st_size,
            "restore_list_entries": 3,
        },
        business_snapshot={
            "sha256": hashlib.sha256(snapshot.read_bytes()).hexdigest(),
            "size_bytes": snapshot.stat().st_size,
            "object_count": 0,
            "inventory_sha256": hashlib.sha256(inventory_path.read_bytes()).hexdigest(),
        },
        reference_validation={
            "schema_version": 1,
            "validator_id": "ux09-reference-validator-v1",
            "query_fingerprint": REFERENCE_QUERY_FINGERPRINT,
            "inventory_sha256": hashlib.sha256(inventory_path.read_bytes()).hexdigest(),
            "expected": 0,
            "checked": 0,
            "mismatches": 0,
        },
    )
    backupctl._write_json(group / "manifest.json", manifest)
    backupctl.write_hmac_signature(group / "manifest.json", key, group / "manifest.sig")
    (group / "COMPLETE").write_text(hashlib.sha256((group / "manifest.json").read_bytes()).hexdigest() + "\n", encoding="ascii")
    return root, key, manifest


@pytest.mark.parametrize("tamper", ["signature", "complete", "dump", "restore-list"])
def test_prune_catalog_verifies_signature_complete_payload_and_custom_dump(
    tmp_path: Path, tamper: str
) -> None:
    backupctl = _load_backupctl()
    root, key, manifest = _write_complete_business_group(tmp_path, backupctl)
    group = root / manifest["backup_run_id"]
    restore_entries = manifest["database"]["restore_list_entries"]
    catalog = backupctl.catalog_from_groups(root, key, lambda _path: restore_entries)
    assert catalog[0]["valid"] is True
    if tamper == "signature":
        (group / "manifest.sig").write_text("0" * 64 + "\n", encoding="ascii")
    elif tamper == "complete":
        (group / "COMPLETE").write_text("0" * 64 + "\n", encoding="ascii")
    elif tamper == "dump":
        (group / "database.dump").write_bytes(b"tampered")
    with pytest.raises(ValueError, match="catalog|signature|COMPLETE|dump|invalid"):
        backupctl.catalog_from_groups(
            root,
            key,
            lambda _path: restore_entries + (1 if tamper == "restore-list" else 0),
        )


def test_rpo_rto_evidence_enforces_24h_and_4h_budgets() -> None:
    backupctl = _load_backupctl()
    evidence = backupctl.build_drill_evidence(
        backup_cutoff_utc="2026-07-14T12:00:00Z",
        failure_at_utc="2026-07-15T00:00:00Z",
        recovery_started_utc="2026-07-15T00:05:00Z",
        recovery_finished_utc="2026-07-15T02:05:00Z",
        b2b3_complete=True,
        smoke_complete=True,
    )
    assert evidence["rpo_hours"] == 12
    assert evidence["rto_hours"] == 2
    assert evidence["gates"]["rpo_24h"] is True
    assert evidence["gates"]["rto_4h"] is True

    with pytest.raises(ValueError, match="RPO|RTO"):
        backupctl.build_drill_evidence(
            backup_cutoff_utc="2026-07-13T00:00:00Z",
            failure_at_utc="2026-07-15T00:00:00Z",
            recovery_started_utc="2026-07-15T00:00:00Z",
            recovery_finished_utc="2026-07-15T05:00:00Z",
            b2b3_complete=False,
            smoke_complete=False,
        )


def test_posix_scripts_use_strict_mode_and_expose_no_secret_values() -> None:
    scripts = sorted(BACKUP_DIR.glob("*.sh"))
    assert scripts
    for script in scripts:
        source = script.read_text(encoding="utf-8")
        assert source.startswith("#!/bin/sh\nset -eu\n"), script
        assert "PGPASSWORD=" not in source
        assert "set -x" not in source
        assert re.search(r"(_FILE|CONFIG_FILE)", source), script


def test_backup_image_is_digest_pinned_and_contains_versioned_toolchain() -> None:
    dockerfile = (BACKUP_DIR / "Dockerfile").read_text(encoding="utf-8")
    from_lines = re.findall(r"^FROM\s+(\S+)", dockerfile, flags=re.MULTILINE)
    assert len(from_lines) >= 3
    assert all(re.search(r":[^@\s]+@sha256:[0-9a-f]{64}$", image) for image in from_lines)
    for required in ("postgres:16.9", "minio/mc:RELEASE.2025-07-21T05-28-08Z", "rclone/rclone:1.70.3"):
        assert required in dockerfile
    assert "COPY *.py *.sh /opt/ux09-backup/" in dockerfile
    assert re.search(r"\bopenssl=[0-9][^\s]*", dockerfile)


def test_destination_adapter_refuses_to_overwrite_existing_run_id() -> None:
    source = (BACKUP_DIR / "destination-rclone.sh").read_text(encoding="utf-8")
    assert "atomic publisher" in source.lower()
    assert "stage-group)" not in source or "exit 78" in source
    assert "publish-group)" not in source or "exit 78" in source
    assert "BACKUP_ATOMIC_PUBLISHER" in BACKUPCTL.read_text(encoding="utf-8")


@pytest.mark.skipif(
    os.environ.get("UX09_RUN_MINIO_PUBLISHER_TEST") != "1",
    reason="set UX09_RUN_MINIO_PUBLISHER_TEST=1 for the disposable MinIO process race",
)
def test_s3_publisher_real_disposable_minio_two_process_race(tmp_path: Path) -> None:
    image = os.environ.get("UX09_BACKUP_PUBLISHER_IMAGE", "ux09-backup:phase6c-publisher-test")
    suffix = uuid.uuid4().hex[:12]
    network = f"ux09-publisher-{suffix}"
    server = f"ux09-publisher-minio-{suffix}"
    runner = f"ux09-publisher-runner-{suffix}"
    minio_image = "minio/minio:RELEASE.2025-07-23T15-54-02Z@sha256:d249d1fb6966de4d8ad26c04754b545205ff15a62e4fd19ebd0f26fa5baacbc0"
    access_key = "u" + secrets.token_hex(12)
    secret_key = "p" + secrets.token_hex(24)
    env_file = tmp_path / "minio.env"
    env_file.write_text(f"MINIO_ROOT_USER={access_key}\nMINIO_ROOT_PASSWORD={secret_key}\n", encoding="utf-8")
    env_file.chmod(0o600)
    config = tmp_path / "config.json"
    config.write_text(json.dumps({
        "version": "10",
        "aliases": {
            "backup": {
                "url": f"http://{server}:9000",
                "accessKey": access_key,
                "secretKey": secret_key,
                "api": "S3v4",
                "path": "auto",
            }
        },
    }), encoding="utf-8")
    config.chmod(0o600)

    def docker(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            ["docker", *args], cwd=ROOT, capture_output=True, text=True,
            encoding="utf-8", check=False,
        )
        if check and result.returncode != 0:
            pytest.fail("disposable MinIO Docker operation failed without exposing provider output")
        return result

    docker("network", "create", network)
    try:
        docker(
            "run", "-d", "--name", server, "--network", network,
            "--env-file", str(env_file), minio_image, "server", "/data",
        )
        docker(
            "run", "-d", "--name", runner, "--network", network,
            "--entrypoint", "sh", image, "-c", "sleep 300",
        )
        docker("cp", str(config), f"{runner}:/tmp/config.json")
        docker("exec", "-u", "0", runner, "chmod", "0600", "/tmp/config.json")
        docker("exec", "-u", "0", runner, "chown", "10001:10001", "/tmp/config.json")
        ready = False
        for _ in range(40):
            probe = docker(
                "exec", runner, "mc", "-C", "/tmp", "--quiet", "ready", "backup",
                check=False,
            )
            if probe.returncode == 0:
                ready = True
                break
            time.sleep(0.25)
        assert ready, "disposable MinIO did not become ready"
        docker("exec", runner, "mc", "-C", "/tmp", "--quiet", "mb", "backup/vault")

        run_id = "business-run-race0001"
        source_one = _write_publisher_source(tmp_path / "one", run_id, b"writer-one")
        source_two = _write_publisher_source(tmp_path / "two", run_id, b"writer-two")
        docker("cp", str(source_one), f"{runner}:/tmp/source-one")
        docker("cp", str(source_two), f"{runner}:/tmp/source-two")

        def guarded_publish(config_path: str, source_path: str, receipt_path: str) -> subprocess.CompletedProcess[str]:
            return docker(
                "exec", runner, "python3", "/opt/ux09-backup/s3-atomic-publisher.py",
                "publish-complete-group", "--lease-config-file", config_path,
                "--destination", "minio://backup/vault/ux09", "--run-id", run_id,
                "--source", source_path, "--receipt", receipt_path, check=False,
            )

        docker("exec", "-u", "0", runner, "chmod", "0640", "/tmp/config.json")
        assert guarded_publish("/tmp/config.json", "/tmp/source-one", "/tmp/rejected-mode.json").returncode == 78
        docker("exec", "-u", "0", runner, "chmod", "0600", "/tmp/config.json")
        docker("exec", runner, "ln", "-s", "/tmp/config.json", "/tmp/config-link.json")
        assert guarded_publish("/tmp/config-link.json", "/tmp/source-one", "/tmp/rejected-link.json").returncode == 78
        docker("exec", runner, "rm", "/tmp/config-link.json")
        docker("exec", runner, "ln", "/tmp/config.json", "/tmp/config-hard.json")
        assert guarded_publish("/tmp/config-hard.json", "/tmp/source-one", "/tmp/rejected-hard.json").returncode == 78
        docker("exec", runner, "rm", "/tmp/config-hard.json")
        docker("exec", runner, "ln", "-s", "/tmp/source-one", "/tmp/source-link")
        assert guarded_publish("/tmp/config.json", "/tmp/source-link", "/tmp/rejected-source.json").returncode == 78
        docker("exec", runner, "rm", "/tmp/source-link")

        def publish(label: str) -> subprocess.CompletedProcess[str]:
            return guarded_publish("/tmp/config.json", f"/tmp/source-{label}", f"/tmp/receipt-{label}.json")

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(publish, ("one", "two")))
        diagnostic_listing = docker(
            "exec", runner, "mc", "-C", "/tmp", "--json", "ls", "--recursive", "backup/vault",
            check=False,
        )
        diagnostic_count = len([line for line in diagnostic_listing.stdout.splitlines() if line.strip()])
        assert sorted(result.returncode for result in results) == [0, 75], (
            f"unexpected publisher exit codes; remote_object_count={diagnostic_count}"
        )
        winner_index = next(index for index, result in enumerate(results) if result.returncode == 0)
        winner_label = ("one", "two")[winner_index]
        winner_source = (source_one, source_two)[winner_index]
        assert all(result.stdout == "" for result in results)
        assert all("vault" not in result.stderr and "source-" not in result.stderr for result in results)

        listing = docker(
            "exec", runner, "mc", "-C", "/tmp", "--json", "ls", "--recursive", "backup/vault"
        )
        entries = [json.loads(line) for line in listing.stdout.splitlines() if line.strip()]
        keys = {entry["key"] for entry in entries}
        group_prefix = f"ux09/{run_id}/"
        expected_members = {
            "COMPLETE", "business.snapshot", "database.dump", "inventory.jsonl",
            "manifest.json", "manifest.sig", "reference-proof.json",
        }
        assert {key.removeprefix(group_prefix) for key in keys if key.startswith(group_prefix)} == expected_members
        lease_keys = [key for key in keys if "/.ux09-private-leases/" in f"/{key}"]
        assert len(lease_keys) == 1

        winner_payload = docker(
            "exec", runner, "mc", "-C", "/tmp", "--quiet", "cat",
            f"backup/vault/{group_prefix}database.dump",
        ).stdout.encode()
        assert winner_payload in {b"writer-one", b"writer-two"}
        complete_entry = next(entry for entry in entries if entry["key"] == group_prefix + "COMPLETE")
        payload_entries = [entry for entry in entries if entry["key"].startswith(group_prefix) and not entry["key"].endswith("COMPLETE")]
        assert all(entry["lastModified"] <= complete_entry["lastModified"] for entry in payload_entries)
        receipt_checks = [
            docker("exec", runner, "test", "-f", f"/tmp/receipt-{label}.json", check=False).returncode
            for label in ("one", "two")
        ]
        assert sorted(receipt_checks) == [0, 1]

        winner_receipt = tmp_path / "winner-receipt.json"
        docker("cp", f"{runner}:/tmp/receipt-{winner_label}.json", str(winner_receipt))
        receipt_value = json.loads(winner_receipt.read_text(encoding="ascii"))
        assert set(receipt_value) == {
            "schema_version", "status", "backup_run_id", "complete_sha256", "lease_id_hash",
        }
        assert receipt_value["schema_version"] == 1
        assert receipt_value["status"] == "committed"
        complete_hash = hashlib.sha256((winner_source / "manifest.json").read_bytes()).hexdigest()
        _load_backupctl().validate_publish_receipt(winner_receipt, run_id, complete_hash)

        remote_lease = f"backup/vault/{lease_keys[0]}"
        docker("exec", runner, "mc", "-C", "/tmp", "--quiet", "get", remote_lease, "/tmp/permanent-lease")
        downloaded_lease = tmp_path / "permanent-lease"
        docker("cp", f"{runner}:/tmp/permanent-lease", str(downloaded_lease))
        lease_value = downloaded_lease.read_bytes()
        assert len(lease_value) == 32
        assert hashlib.sha256(lease_value).hexdigest() == receipt_value["lease_id_hash"]

        reconciled_result = docker(
            "exec", runner, "python3", "/opt/ux09-backup/s3-atomic-publisher.py",
            "reconcile-complete-group", "--lease-config-file", "/tmp/config.json",
            "--destination", "minio://backup/vault/ux09", "--run-id", run_id,
            "--receipt", "/tmp/reconciled-receipt.json", check=False,
        )
        assert reconciled_result.returncode == 0
        assert reconciled_result.stdout == ""
        reconciled_receipt = tmp_path / "reconciled-receipt.json"
        docker("cp", f"{runner}:/tmp/reconciled-receipt.json", str(reconciled_receipt))
        assert json.loads(reconciled_receipt.read_text(encoding="ascii")) == receipt_value
        _load_backupctl().validate_publish_receipt(reconciled_receipt, run_id, complete_hash)
        post_reconcile_listing = docker(
            "exec", runner, "mc", "-C", "/tmp", "--json", "ls", "--recursive", "backup/vault"
        )
        post_reconcile_entries = [
            json.loads(line) for line in post_reconcile_listing.stdout.splitlines() if line.strip()
        ]
        assert {entry["key"] for entry in post_reconcile_entries} == keys
    finally:
        docker("rm", "-f", runner, server, check=False)
        docker("network", "rm", network, check=False)


def test_drill_compose_is_isolated_disposable_and_has_no_traffic_service(tmp_path: Path) -> None:
    released_cli = tmp_path / "b2b3-cli"
    released_worker = tmp_path / "b2b3-worker"
    released_cli.write_text("release fixture", encoding="ascii")
    released_worker.write_text("release fixture", encoding="ascii")
    environment = os.environ.copy()
    environment.update(
        {
            "BACKUP_IMAGE": "registry.example.test/ux09-backup",
            "BACKUP_IMAGE_DIGEST": "sha256:" + "a" * 64,
            "B2B3_CLI_RELEASE_FILE": str(released_cli),
            "B2B3_WORKER_RELEASE_FILE": str(released_worker),
        }
    )
    result = subprocess.run(
        ["docker", "compose", "-f", str(DRILL_COMPOSE), "config", "--format", "json"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        env=environment,
    )
    assert result.returncode == 0, result.stderr
    model = json.loads(result.stdout)
    assert model["name"].startswith("ux09-backup-drill-")
    assert set(model["services"]) == {"backup-tool", "minio", "postgres"}
    assert not any(service.get("ports") for service in model["services"].values())
    assert model["networks"]["recovery"]["internal"] is True
    assert model["networks"]["offhost-egress"].get("internal", False) is False
    assert set(model["services"]["postgres"]["networks"]) == {"recovery"}
    assert set(model["services"]["minio"]["networks"]) == {"recovery"}
    assert set(model["services"]["backup-tool"]["networks"]) == {
        "offhost-egress",
        "recovery",
    }
    rendered = result.stdout.lower()
    assert "ux09_postgres-data" not in rendered
    assert "ux09_minio-data" not in rendered
    assert model["services"]["backup-tool"]["image"].endswith("@sha256:" + "a" * 64)
    tool_environment = model["services"]["backup-tool"]["environment"]
    assert tool_environment["BACKUP_IMAGE"] == "registry.example.test/ux09-backup"
    assert tool_environment["BACKUP_IMAGE_DIGEST"] == "sha256:" + "a" * 64
    assert tool_environment["BACKUP_CATALOG_FILE"] == "/work/verified-backup-catalog.json"
    assert tool_environment["B2B3_CLI_COMMAND"] == "/released/b2b3-cli"
    assert tool_environment["B2B3_WORKER_COMMAND"] == "/released/b2b3-worker"
    tool_volumes = model["services"]["backup-tool"]["volumes"]
    release_mounts = {
        volume["target"]: volume
        for volume in tool_volumes
        if volume["target"].startswith("/released/")
    }
    assert set(release_mounts) == {"/released/b2b3-cli", "/released/b2b3-worker"}
    assert all(volume["type"] == "bind" for volume in release_mounts.values())
    assert all(volume["read_only"] is True for volume in release_mounts.values())
    assert "b2b3_evidence_signing_key" not in model.get("secrets", {})
    assert all(
        secret["source"] != "b2b3_evidence_signing_key"
        for secret in model["services"]["backup-tool"]["secrets"]
    )
    raw_compose = DRILL_COMPOSE.read_text(encoding="utf-8")
    assert "ux09-backup:phase6c-foundation" not in raw_compose
    assert "${BACKUP_IMAGE:?" in raw_compose
    assert "${BACKUP_IMAGE_DIGEST:?" in raw_compose


@pytest.mark.parametrize(
    ("image", "digest"),
    [
        ("https://registry.example.test/ux09-backup", "sha256:" + "a" * 64),
        ("Registry.example.test/ux09-backup", "sha256:" + "a" * 64),
        ("registry.example.test/ux09-backup@sha256:" + "a" * 64, "sha256:" + "a" * 64),
        ("registry.example.test/ux09 backup", "sha256:" + "a" * 64),
        ("registry.example.test/ux09-backup", "a" * 64),
        ("registry.example.test/ux09-backup", "sha256:" + "A" * 64),
        ("registry.example.test/ux09-backup", "sha256:short"),
    ],
)
def test_drill_preflight_rejects_mutable_or_malformed_image_reference(image: str, digest: str) -> None:
    backupctl = _load_backupctl()
    with pytest.raises(ValueError, match="image|digest"):
        backupctl.validate_backup_image_reference(image, digest)
    assert backupctl.validate_backup_image_reference(
        "registry.example.test/ux09-backup:phase6c-foundation", "sha256:" + "a" * 64
    ).endswith("@sha256:" + "a" * 64)


def test_drill_preflight_fails_closed_on_invalid_latest_and_is_fixed_entry() -> None:
    backupctl = _load_backupctl()
    catalog = [
        _catalog_entry("business-run-old11", "2026-07-14T00:00:00Z", complete_order=1),
        _catalog_entry("business-run-latest1", "2026-07-15T00:00:00Z", complete_order=2, valid=False),
    ]
    with pytest.raises(ValueError, match="latest|valid"):
        backupctl.validate_drill_preflight(
            project="ux09-backup-drill-safe123",
            volumes=["ux09-backup-drill-safe123-postgres-data", "ux09-backup-drill-safe123-minio-data"],
            confirmed="1",
            image="registry.example.test/ux09-backup",
            digest="sha256:" + "a" * 64,
            catalog=catalog,
            retention_days=30,
            now=datetime(2026, 7, 15, 1, tzinfo=timezone.utc),
        )
    source = BACKUPCTL.read_text(encoding="utf-8")
    drill_script = (BACKUP_DIR / "drill.sh").read_text(encoding="utf-8")
    assert '"preflight-drill"' in source
    assert "preflight-drill" in drill_script


def _tar_with_member(path: Path, member: tarfile.TarInfo, payload: bytes = b"") -> None:
    with tarfile.open(path, "w") as archive:
        archive.addfile(member, io.BytesIO(payload) if member.isreg() else None)


@pytest.mark.parametrize("kind", ["absolute", "traversal", "symlink", "hardlink", "fifo", "unexpected", "bucket-root-file"])
def test_business_snapshot_safe_extract_rejects_malicious_tar_members(tmp_path: Path, kind: str) -> None:
    backupctl = _load_backupctl()
    archive = tmp_path / f"{kind}.tar"
    if kind == "absolute":
        member = tarfile.TarInfo("/objects/resumes/escape")
    elif kind == "traversal":
        member = tarfile.TarInfo("objects/resumes/../../escape")
    elif kind == "unexpected":
        member = tarfile.TarInfo("unexpected/resumes/file")
    elif kind == "bucket-root-file":
        member = tarfile.TarInfo("objects/resumes")
    else:
        member = tarfile.TarInfo("objects/resumes/entry")
    if kind == "symlink":
        member.type = tarfile.SYMTYPE
        member.linkname = "../../escape"
    elif kind == "hardlink":
        member.type = tarfile.LNKTYPE
        member.linkname = "objects/resumes/other"
    elif kind == "fifo":
        member.type = tarfile.FIFOTYPE
    else:
        member.size = 1
    _tar_with_member(archive, member, b"x")
    destination = tmp_path / "extract"
    with pytest.raises(ValueError, match="tar|member|path|type|bucket"):
        backupctl.safe_extract_business_snapshot(archive, destination, {"resumes"})
    assert not (tmp_path / "escape").exists()


def test_business_snapshot_safe_extract_accepts_only_approved_regular_tree(tmp_path: Path) -> None:
    backupctl = _load_backupctl()
    archive = tmp_path / "valid.tar"
    member = tarfile.TarInfo("objects/resumes/clean/document.bin")
    member.size = 4
    _tar_with_member(archive, member, b"safe")
    destination = tmp_path / "extract"
    backupctl.safe_extract_business_snapshot(archive, destination, {"resumes"})
    assert (destination / "objects" / "resumes" / "clean" / "document.bin").read_bytes() == b"safe"


def test_runbooks_and_report_state_foundation_limits_and_real_b2b3_dependency() -> None:
    combined = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (BACKUP_RUNBOOK, OPERATIONS_RUNBOOK, FOUNDATION_REPORT)
    )
    for required in (
        "12 hours",
        "24-hour RPO",
        "4-hour RTO",
        "DISPOSABLE_RECOVERY_CONFIRMED=1",
        "B2B3 CLI",
        "B2B3 Worker",
        "traffic",
        "forward-only",
        "rollback",
        "credential",
        "signing-key",
        "Phase 6C foundation",
        "preflight-drill",
        "BACKUP_IMAGE_DIGEST=sha256:",
        "LEDGER_MANIFEST_VERIFY_KEY_FILE",
        "LEDGER_PAIRING_GROUP_PATH",
        "invalid latest",
    ):
        assert required.lower() in combined.lower()
    assert "docker compose -f deploy/compose.backup-drill.yaml config --quiet" in combined
    assert "/opt/ux09-backup/drill.sh" in combined
    assert "not production ready" in FOUNDATION_REPORT.read_text(encoding="utf-8").lower()
    assert "complete real restore drill" in FOUNDATION_REPORT.read_text(encoding="utf-8").lower()
