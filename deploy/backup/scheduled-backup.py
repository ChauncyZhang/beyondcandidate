#!/usr/bin/env python3
"""Linux oneshot coordinator for restore-aware Phase 6C backups."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import tempfile
from typing import Callable, Iterator, Mapping, Sequence, TextIO

import backupctl


UTC_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
DEFAULT_THRESHOLD_SECONDS = 18 * 60 * 60
DEFAULT_RPO_PAGE_SECONDS = 24 * 60 * 60
DEFAULT_MAX_FUTURE_SKEW_SECONDS = 5 * 60


class StageFailure(RuntimeError):
    def __init__(self, stage: str, exit_code: int):
        super().__init__(f"backup stage failed: {stage}")
        self.stage = stage
        self.exit_code = exit_code if 0 < exit_code < 256 else 1
        self.run_id: str | None = None


class OverlapFailure(RuntimeError):
    pass


def _required(environment: Mapping[str, str], name: str) -> str:
    value = environment.get(name, "")
    if not value or "\x00" in value or "\n" in value or "\r" in value:
        raise ValueError(f"{name} is required and must be a single value")
    return value


def _positive_int(environment: Mapping[str, str], name: str, default: int) -> int:
    raw = environment.get(name, str(default))
    if not raw.isascii() or not raw.isdecimal():
        raise ValueError(f"{name} must be a positive integer")
    value = int(raw)
    if value <= 0 or value > 31_536_000:
        raise ValueError(f"{name} must be between 1 and 31536000 seconds")
    return value


def _required_file(environment: Mapping[str, str], name: str) -> Path:
    path = Path(_required(environment, name))
    if not path.is_absolute():
        raise ValueError(f"{name} must be an absolute file path")
    metadata = path.lstat()
    if not path.is_file() or path.is_symlink():
        raise ValueError(f"{name} must be a regular non-symlink file")
    if metadata.st_nlink != 1:
        raise ValueError(f"{name} hardlinks or shared inodes are forbidden")
    if os.name != "nt" and metadata.st_mode & 0o077:
        raise ValueError(f"{name} permissions must be 0600 or stricter")
    return path


def _parse_utc(value: str) -> datetime:
    try:
        parsed = datetime.strptime(value, UTC_FORMAT).replace(tzinfo=timezone.utc)
    except (TypeError, ValueError) as error:
        raise ValueError("backup freshness timestamp is invalid") from error
    return parsed


def _json_log(stream: TextIO, event: str, **fields: object) -> None:
    record = {
        "timestamp": datetime.now(timezone.utc).strftime(UTC_FORMAT),
        "event": event,
        **fields,
    }
    print(json.dumps(record, sort_keys=True, separators=(",", ":")), file=stream, flush=True)


def run_stage(
    stage: str,
    command: Sequence[str],
    environment: Mapping[str, str],
    *,
    subprocess_run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    log_stream: TextIO = sys.stdout,
) -> None:
    completed = subprocess_run(
        list(command),
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=dict(environment),
    )
    if completed.returncode != 0:
        _json_log(
            log_stream,
            "backup_stage",
            stage=stage,
            result="failed",
            exit_code=completed.returncode,
        )
        raise StageFailure(stage, completed.returncode)
    _json_log(log_stream, "backup_stage", stage=stage, result="succeeded", exit_code=0)


def _atomic_write(path: Path, payload: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        if os.name != "nt":
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def _state_root(environment: Mapping[str, str]) -> Path:
    root = Path(_required(environment, "BACKUP_STATE_DIR"))
    if not root.is_absolute():
        raise ValueError("BACKUP_STATE_DIR must be absolute")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if root.is_symlink():
        raise ValueError("BACKUP_STATE_DIR cannot be a symlink")
    return root


def _write_freshness(
    state_root: Path,
    *,
    run_id: str,
    cutoff_utc: str,
    valid_restore_points: int,
    warning_threshold_seconds: int,
    page_threshold_seconds: int,
) -> None:
    backupctl.validate_run_id(run_id)
    cutoff = _parse_utc(cutoff_utc)
    if valid_restore_points < 2:
        raise ValueError("at least two valid restore points are required")
    state = {
        "schema_version": 2,
        "backup_run_id": run_id,
        "backup_cutoff_utc": cutoff_utc,
        "valid_restore_points": valid_restore_points,
    }
    _atomic_write(
        state_root / "last-success.json",
        (json.dumps(state, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8"),
    )
    metrics = (
        "# HELP ux09_backup_last_valid_restore_point_unixtime_seconds "
        "Cutoff time of the newest remotely validated complete restore point.\n"
        "# TYPE ux09_backup_last_valid_restore_point_unixtime_seconds gauge\n"
        f"ux09_backup_last_valid_restore_point_unixtime_seconds {int(cutoff.timestamp())}\n"
        "# HELP ux09_backup_valid_restore_points Remotely validated complete restore points.\n"
        "# TYPE ux09_backup_valid_restore_points gauge\n"
        f"ux09_backup_valid_restore_points {valid_restore_points}\n"
        "# HELP ux09_backup_freshness_warning_threshold_seconds Backup freshness warning threshold.\n"
        "# TYPE ux09_backup_freshness_warning_threshold_seconds gauge\n"
        f"ux09_backup_freshness_warning_threshold_seconds {warning_threshold_seconds}\n"
        "# HELP ux09_backup_rpo_page_threshold_seconds Backup RPO page threshold.\n"
        "# TYPE ux09_backup_rpo_page_threshold_seconds gauge\n"
        f"ux09_backup_rpo_page_threshold_seconds {page_threshold_seconds}\n"
    )
    metrics_directory = state_root / "metrics"
    metrics_directory.mkdir(mode=0o755, exist_ok=True)
    if metrics_directory.is_symlink() or not metrics_directory.is_dir():
        raise ValueError("backup metrics directory must be a regular directory")
    metrics_directory.chmod(0o755)
    _atomic_write(metrics_directory / "backup.prom", metrics.encode("ascii"), mode=0o644)


def _write_pending_run(
    path: Path,
    *,
    state: str,
    stage: str,
    ledger_run_id: str,
    business_run_id: str,
    cutoff_utc: str,
) -> None:
    _atomic_write(
        path,
        (
            json.dumps(
                {
                    "schema_version": 1,
                    "state": state,
                    "stage": stage,
                    "ledger_run_id": ledger_run_id,
                    "business_run_id": business_run_id,
                    "cutoff_utc": cutoff_utc,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8"),
    )


def check_freshness(
    environment: Mapping[str, str],
    *,
    now: datetime | None = None,
) -> int:
    root = _state_root(environment)
    warning_threshold = _positive_int(
        environment,
        "BACKUP_FRESHNESS_THRESHOLD_SECONDS",
        DEFAULT_THRESHOLD_SECONDS,
    )
    page_threshold = _positive_int(
        environment,
        "BACKUP_RPO_PAGE_SECONDS",
        DEFAULT_RPO_PAGE_SECONDS,
    )
    if page_threshold <= warning_threshold:
        raise ValueError("BACKUP_RPO_PAGE_SECONDS must exceed the freshness warning threshold")
    maximum_future_skew = _positive_int(
        environment,
        "BACKUP_MAX_FUTURE_SKEW_SECONDS",
        DEFAULT_MAX_FUTURE_SKEW_SECONDS,
    )
    state_path = root / "last-success.json"
    if not state_path.is_file() or state_path.is_symlink():
        raise ValueError("backup freshness state is missing")
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("backup freshness state is malformed") from error
    if not isinstance(state, dict) or set(state) != {
        "schema_version", "backup_run_id", "backup_cutoff_utc", "valid_restore_points"
    } or state.get("schema_version") != 2:
        raise ValueError("backup freshness state is malformed")
    valid_restore_points = state.get("valid_restore_points")
    if not isinstance(valid_restore_points, int) or valid_restore_points < 2:
        raise ValueError("backup freshness state must prove at least two valid restore points")
    backupctl.validate_run_id(state.get("backup_run_id"))
    cutoff = _parse_utc(state.get("backup_cutoff_utc"))
    current = now or datetime.now(timezone.utc)
    age = int((current - cutoff).total_seconds())
    if age < -maximum_future_skew:
        raise ValueError("backup freshness timestamp is in the future")
    if age > page_threshold:
        raise ValueError("backup freshness state is stale")
    return max(age, 0)


@contextmanager
def exclusive_lock(path: Path) -> Iterator[None]:
    try:
        import fcntl
    except ImportError as error:  # pragma: no cover - target is production Linux
        raise RuntimeError("backup scheduler requires Linux fcntl locking") from error
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise OverlapFailure("another scheduled backup is active") from error
        yield
    finally:
        os.close(descriptor)


def _restore_list_entries(path: Path) -> int:
    completed = subprocess.run(
        [os.environ.get("PG_RESTORE_BIN", "pg_restore"), "--list", str(path)],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return sum(1 for line in completed.stdout.splitlines() if line and not line.startswith(";"))


def _run_id(prefix: str, now: datetime, token_hex: Callable[[int], str]) -> str:
    return backupctl.validate_run_id(
        f"{prefix}-{now.strftime('%Y%m%dT%H%M%SZ')}-{token_hex(8)}"
    )


def run_once(
    environment: Mapping[str, str],
    *,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    token_hex: Callable[[int], str] = secrets.token_hex,
    execute: Callable[[str, Sequence[str], Mapping[str, str]], None] = run_stage,
    validate_group: Callable[..., Mapping[str, object]] = backupctl.validate_complete_backup_group,
    validate_ledger_group: Callable[..., Mapping[str, object]] = backupctl.validate_ledger_archive_group,
    lock: Callable[[Path], object] = exclusive_lock,
) -> None:
    root = _state_root(environment)
    warning_threshold = _positive_int(
        environment,
        "BACKUP_FRESHNESS_THRESHOLD_SECONDS",
        DEFAULT_THRESHOLD_SECONDS,
    )
    page_threshold = _positive_int(
        environment,
        "BACKUP_RPO_PAGE_SECONDS",
        DEFAULT_RPO_PAGE_SECONDS,
    )
    if page_threshold <= warning_threshold:
        raise ValueError("BACKUP_RPO_PAGE_SECONDS must exceed the freshness warning threshold")
    retention_days = _positive_int(environment, "BACKUP_WINDOW_DAYS", 30)
    for name in (
        "LEDGER_ARCHIVE_CLIENT",
        "LEDGER_ARCHIVE_DESTINATION",
        "BACKUP_DESTINATION_CLIENT",
        "BACKUP_DESTINATION",
    ):
        _required(environment, name)
    ledger_archive_config = _required_file(environment, "LEDGER_ARCHIVE_CONFIG_FILE")
    ledger_verify_config = _required_file(environment, "LEDGER_VERIFY_CONFIG_FILE")
    ledger_manifest_verify_key = _required_file(
        environment, "LEDGER_MANIFEST_VERIFY_KEY_FILE"
    )
    ledger_signing_key_history = _required_file(
        environment, "LEDGER_SIGNING_KEY_HISTORY_FILE"
    )
    backup_verify_config = _required_file(environment, "BACKUP_VERIFY_CONFIG_FILE")
    manifest_verify_key = _required_file(environment, "BACKUP_MANIFEST_VERIFY_KEY_FILE")
    backupctl.validate_secret_files(
        [
            ledger_archive_config,
            ledger_verify_config,
            ledger_manifest_verify_key,
            ledger_signing_key_history,
            backup_verify_config,
            manifest_verify_key,
        ]
    )

    with lock(root / "scheduler.lock"):
        pending_path = root / "pending-run.json"
        if pending_path.exists() or pending_path.is_symlink():
            raise ValueError("a scheduled backup run requires reconciliation")
        started = now().astimezone(timezone.utc).replace(microsecond=0)
        cutoff = started.strftime(UTC_FORMAT)
        token = token_hex(8)
        ledger_run_id = _run_id("ledger", started, lambda _size: token)
        business_run_id = _run_id("business", started, lambda _size: token)
        workspace = root / "runs" / business_run_id
        ledger_group = workspace / ledger_run_id
        business_group = workspace / business_run_id
        workspace.mkdir(parents=True, mode=0o700)
        current_stage = "allocated"
        completed = False
        _write_pending_run(
            pending_path,
            state="running",
            stage=current_stage,
            ledger_run_id=ledger_run_id,
            business_run_id=business_run_id,
            cutoff_utc=cutoff,
        )
        child_environment = dict(environment)
        child_environment.update(
            {
                "LEDGER_ARCHIVE_CONFIG_FILE": str(ledger_archive_config),
                "LEDGER_VERIFY_CONFIG_FILE": str(ledger_verify_config),
                "BACKUP_VERIFY_CONFIG_FILE": str(backup_verify_config),
                "BACKUP_MANIFEST_VERIFY_KEY_FILE": str(manifest_verify_key),
            }
        )
        backupctl_path = Path(__file__).with_name("backupctl.py")

        def execute_stage(stage: str, command: Sequence[str]) -> None:
            nonlocal current_stage
            current_stage = stage
            _write_pending_run(
                pending_path,
                state="running",
                stage=stage,
                ledger_run_id=ledger_run_id,
                business_run_id=business_run_id,
                cutoff_utc=cutoff,
            )
            execute(stage, command, child_environment)

        try:
            child_environment.update(
                {
                    "LEDGER_ARCHIVE_RUN_ID": ledger_run_id,
                    "LEDGER_ARCHIVE_CUTOFF_UTC": cutoff,
                    "LEDGER_ARCHIVE_MANIFEST_FILE": str(workspace / "ledger-manifest.json"),
                }
            )
            execute_stage(
                "ledger_publish",
                [sys.executable, str(backupctl_path), "ledger-archive"],
            )
            execute_stage(
                "ledger_fetch",
                [
                    _required(environment, "LEDGER_ARCHIVE_CLIENT"),
                    "fetch-complete-group",
                    "--config-file",
                    str(ledger_verify_config),
                    "--destination",
                    _required(environment, "LEDGER_ARCHIVE_DESTINATION"),
                    "--run-id",
                    ledger_run_id,
                    "--output",
                    str(ledger_group),
                ],
            )
            child_environment.update(
                {
                    "BACKUP_RUN_ID": business_run_id,
                    "BACKUP_CUTOFF_UTC": cutoff,
                    "LEDGER_PAIRING_GROUP_PATH": str(ledger_group),
                }
            )
            execute_stage(
                "business_publish",
                [sys.executable, str(backupctl_path), "backup"],
            )
            execute_stage(
                "business_fetch",
                [
                    _required(environment, "BACKUP_DESTINATION_CLIENT"),
                    "fetch-complete-group",
                    "--config-file",
                    str(backup_verify_config),
                    "--destination",
                    _required(environment, "BACKUP_DESTINATION"),
                    "--run-id",
                    business_run_id,
                    "--output",
                    str(business_group),
                ],
            )
            manifest = validate_group(
                business_group,
                manifest_verify_key,
                _restore_list_entries,
            )
            if manifest.get("backup_run_id") != business_run_id:
                raise ValueError("validated backup group is not bound to the scheduled run")
            catalog_path = workspace / "verified-catalog.json"
            execute_stage(
                "business_catalog",
                [
                    _required(environment, "BACKUP_DESTINATION_CLIENT"),
                    "catalog",
                    "--config-file",
                    str(backup_verify_config),
                    "--signing-key-file",
                    str(manifest_verify_key),
                    "--destination",
                    _required(environment, "BACKUP_DESTINATION"),
                    "--output",
                    str(catalog_path),
                ],
            )
            try:
                catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as error:
                raise ValueError("remote backup catalog is malformed") from error
            if not isinstance(catalog, list):
                raise ValueError("remote backup catalog must be an array")
            if any(not isinstance(item, dict) for item in catalog):
                raise ValueError("remote backup catalog entries must be objects")
            backupctl.plan_prune(catalog, retention_days, started)
            valid_points = [
                item
                for item in catalog
                if isinstance(item, dict)
                and item.get("complete") is True
                and item.get("valid") is True
            ]
            if len(valid_points) < 2:
                raise ValueError("at least two valid restore points are required")
            try:
                ledger_history = json.loads(
                    ledger_signing_key_history.read_text(encoding="utf-8")
                )
            except (OSError, UnicodeError, json.JSONDecodeError) as error:
                raise ValueError("ledger signing-key history is malformed") from error
            ledger_validation_root = workspace / "catalog-ledgers"
            ledger_validation_root.mkdir(mode=0o700)
            for point in valid_points:
                business_point_run_id = backupctl.validate_run_id(
                    str(point.get("backup_run_id", ""))
                )
                ledger_evidence = point.get("ledger_archive")
                if not isinstance(ledger_evidence, dict) or set(ledger_evidence) != {
                    "archive_run_id",
                    "cutoff_utc",
                    "manifest_sha256",
                    "signing_key_versions",
                }:
                    raise ValueError("backup catalog ledger evidence is invalid")
                archive_run_id = backupctl.validate_run_id(
                    str(ledger_evidence.get("archive_run_id", ""))
                )
                expected_cutoff = str(point.get("backup_cutoff_utc", ""))
                if ledger_evidence.get("cutoff_utc") != expected_cutoff:
                    raise ValueError("backup catalog ledger cutoff binding is invalid")
                backupctl._require_sha256(
                    ledger_evidence.get("manifest_sha256"),
                    "ledger_archive.manifest_sha256",
                )
                point_root = backupctl.safe_run_path(
                    ledger_validation_root, business_point_run_id
                )
                point_root.mkdir(mode=0o700)
                historical_ledger_group = backupctl.safe_run_path(
                    point_root, archive_run_id
                )
                execute_stage(
                    "catalog_ledger_fetch",
                    [
                        _required(environment, "LEDGER_ARCHIVE_CLIENT"),
                        "fetch-complete-group",
                        "--config-file",
                        str(ledger_verify_config),
                        "--destination",
                        _required(environment, "LEDGER_ARCHIVE_DESTINATION"),
                        "--run-id",
                        archive_run_id,
                        "--output",
                        str(historical_ledger_group),
                    ],
                )
                validated_ledger = validate_ledger_group(
                    historical_ledger_group,
                    ledger_manifest_verify_key,
                    ledger_history,
                    minimum_cutoff_utc=expected_cutoff,
                )
                recorded_versions = ledger_evidence.get("signing_key_versions")
                current_versions = {
                    item.get("version")
                    for item in ledger_history.get("versions", [])
                    if isinstance(item, dict) and isinstance(item.get("version"), str)
                }
                if (
                    validated_ledger.get("archive_run_id") != archive_run_id
                    or validated_ledger.get("cutoff_utc") != expected_cutoff
                    or not isinstance(recorded_versions, list)
                    or not recorded_versions
                    or len(set(recorded_versions)) != len(recorded_versions)
                    or not set(recorded_versions).issubset(current_versions)
                    or validated_ledger.get("signing_key_version")
                    not in recorded_versions
                    or backupctl._sha256_file(
                        historical_ledger_group / "ledger-manifest.json"
                    )
                    != ledger_evidence.get("manifest_sha256")
                ):
                    raise ValueError("backup catalog ledger binding is invalid")
            latest = max(valid_points, key=lambda item: int(item.get("complete_order", 0)))
            if latest.get("backup_run_id") != business_run_id:
                raise ValueError("scheduled backup is not the latest valid restore point")
            if latest.get("backup_cutoff_utc") != manifest.get("backup_cutoff_utc"):
                raise ValueError("latest restore point cutoff does not match the validated group")
            _write_freshness(
                root,
                run_id=business_run_id,
                cutoff_utc=str(manifest.get("backup_cutoff_utc", "")),
                valid_restore_points=len(valid_points),
                warning_threshold_seconds=warning_threshold,
                page_threshold_seconds=page_threshold,
            )
            _json_log(
                sys.stdout,
                "scheduled_backup",
                run_id=business_run_id,
                result="succeeded",
            )
            completed = True
        except StageFailure as error:
            error.run_id = (
                ledger_run_id if current_stage == "ledger_publish" else business_run_id
            )
            state = (
                "reconciliation_required"
                if current_stage in {"ledger_publish", "business_publish"}
                and error.exit_code in {75, 76}
                else "failed"
            )
            _write_pending_run(
                pending_path,
                state=state,
                stage=current_stage,
                ledger_run_id=ledger_run_id,
                business_run_id=business_run_id,
                cutoff_utc=cutoff,
            )
            raise
        except (OSError, RuntimeError, TypeError, ValueError):
            _write_pending_run(
                pending_path,
                state="failed",
                stage=current_stage,
                ledger_run_id=ledger_run_id,
                business_run_id=business_run_id,
                cutoff_utc=cutoff,
            )
            raise
        finally:
            if completed:
                shutil.rmtree(workspace, ignore_errors=True)
                pending_path.unlink(missing_ok=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("run", "freshness-check"))
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        if arguments.command == "run":
            run_once(os.environ)
        else:
            age = check_freshness(os.environ)
            _json_log(sys.stdout, "backup_freshness", result="fresh", age_seconds=age)
        return 0
    except StageFailure as error:
        fields: dict[str, object] = {
            "result": "failed",
            "stage": error.stage,
            "exit_code": error.exit_code,
        }
        if error.run_id is not None:
            fields["run_id"] = error.run_id
        _json_log(sys.stderr, "scheduled_backup", **fields)
        return error.exit_code
    except OverlapFailure:
        _json_log(sys.stderr, "scheduled_backup", result="overlap_rejected", exit_code=75)
        return 75
    except (OSError, RuntimeError, TypeError, ValueError) as error:
        _json_log(
            sys.stderr,
            "scheduled_backup",
            result="failed_closed",
            error_class=type(error).__name__,
            exit_code=78,
        )
        return 78


if __name__ == "__main__":
    raise SystemExit(main())
