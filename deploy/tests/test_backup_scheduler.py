from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import os
import re
import shutil
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCHEDULER = ROOT / "deploy" / "backup" / "scheduled-backup.py"
SYSTEMD_RENDERER = ROOT / "deploy" / "backup" / "render-systemd-units.sh"
BACKUP_COMPOSE = ROOT / "deploy" / "compose.backup.yaml"
ALERT_RULES = ROOT / "deploy" / "observability" / "alerts" / "ux09.rules.yml"


def load_scheduler():
    assert SCHEDULER.is_file(), "Phase 6C scheduled backup coordinator is missing"
    spec = importlib.util.spec_from_file_location("ux09_scheduled_backup", SCHEDULER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    sys.path.insert(0, str(SCHEDULER.parent))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(SCHEDULER.parent))
    return module


def scheduler_env(state_dir: Path) -> dict[str, str]:
    secrets_dir = state_dir / "test-secrets"
    secrets_dir.mkdir(parents=True, exist_ok=True)
    files = {
        "LEDGER_ARCHIVE_CONFIG_FILE": "ledger-archive.conf",
        "LEDGER_VERIFY_CONFIG_FILE": "ledger-verify.conf",
        "LEDGER_MANIFEST_VERIFY_KEY_FILE": "ledger-manifest.key",
        "LEDGER_SIGNING_KEY_HISTORY_FILE": "ledger-history.json",
        "BACKUP_VERIFY_CONFIG_FILE": "backup-verify.conf",
        "BACKUP_MANIFEST_VERIFY_KEY_FILE": "manifest.key",
    }
    for name, filename in files.items():
        path = secrets_dir / filename
        value = (
            '{"schema_version":1,"active_key_version":"v1",'
            '"versions":[{"version":"v0","status":"retired"},'
            '{"version":"v1","status":"active"}]}\n'
            if name == "LEDGER_SIGNING_KEY_HISTORY_FILE"
            else "test-fixture\n"
        )
        path.write_text(value, encoding="ascii")
        path.chmod(0o600)
    return {
        "BACKUP_STATE_DIR": str(state_dir),
        "BACKUP_FRESHNESS_THRESHOLD_SECONDS": "64800",
        "BACKUP_RPO_PAGE_SECONDS": "86400",
        "BACKUP_MAX_FUTURE_SKEW_SECONDS": "300",
        "BACKUP_WINDOW_DAYS": "30",
        "LEDGER_ARCHIVE_CLIENT": "/opt/ux09-backup/ledger-client",
        "LEDGER_ARCHIVE_CONFIG_FILE": str(secrets_dir / files["LEDGER_ARCHIVE_CONFIG_FILE"]),
        "LEDGER_VERIFY_CONFIG_FILE": str(secrets_dir / files["LEDGER_VERIFY_CONFIG_FILE"]),
        "LEDGER_MANIFEST_VERIFY_KEY_FILE": str(
            secrets_dir / files["LEDGER_MANIFEST_VERIFY_KEY_FILE"]
        ),
        "LEDGER_SIGNING_KEY_HISTORY_FILE": str(
            secrets_dir / files["LEDGER_SIGNING_KEY_HISTORY_FILE"]
        ),
        "LEDGER_ARCHIVE_DESTINATION": "s3://ledger-backups/ux09",
        "BACKUP_DESTINATION_CLIENT": "/opt/ux09-backup/destination-rclone.sh",
        "BACKUP_VERIFY_CONFIG_FILE": str(secrets_dir / files["BACKUP_VERIFY_CONFIG_FILE"]),
        "BACKUP_DESTINATION": "s3://business-backups/ux09",
        "BACKUP_MANIFEST_VERIFY_KEY_FILE": str(secrets_dir / files["BACKUP_MANIFEST_VERIFY_KEY_FILE"]),
    }


def catalog_entry(
    business_run_id: str,
    cutoff_utc: str,
    complete_order: int,
    ledger_run_id: str,
) -> dict[str, object]:
    ledger_manifest = fake_ledger_manifest(ledger_run_id)
    return {
        "backup_run_id": business_run_id,
        "backup_cutoff_utc": cutoff_utc,
        "complete": True,
        "valid": True,
        "backup_window_days": 30,
        "complete_order": complete_order,
        "ledger_archive": {
            "archive_run_id": ledger_run_id,
            "cutoff_utc": cutoff_utc,
            "manifest_sha256": hashlib.sha256(ledger_manifest).hexdigest(),
            "signing_key_versions": ["v1"],
        },
    }


def fake_ledger_manifest(ledger_run_id: str) -> bytes:
    return (json.dumps({"archive_run_id": ledger_run_id}, sort_keys=True) + "\n").encode(
        "ascii"
    )


def write_fake_ledger_fetch(command: list[str]) -> None:
    output = Path(command[command.index("--output") + 1])
    output.mkdir(parents=True, exist_ok=True)
    (output / "ledger-manifest.json").write_bytes(fake_ledger_manifest(output.name))


@contextmanager
def unlocked(_path: Path):
    yield


def test_run_updates_freshness_only_after_exact_remote_group_validation(tmp_path: Path) -> None:
    scheduler = load_scheduler()
    calls: list[tuple[str, list[str]]] = []

    def execute(stage: str, command: list[str], environment: dict[str, str]) -> None:
        calls.append((stage, command))
        if stage.endswith("fetch"):
            output = Path(command[command.index("--output") + 1])
            output.mkdir(parents=True)
        if stage == "catalog_ledger_fetch":
            write_fake_ledger_fetch(command)
        if stage == "business_catalog":
            output = Path(command[command.index("--output") + 1])
            output.write_text(
                json.dumps(
                    [
                        catalog_entry(
                            "business-previous",
                            "2026-07-14T13:02:03Z",
                            1,
                            "ledger-previous",
                        ),
                        catalog_entry(
                            "business-20260715T010203Z-0011223344556677",
                            "2026-07-15T01:02:03Z",
                            2,
                            "ledger-20260715T010203Z-0011223344556677",
                        ),
                    ]
                ),
                encoding="utf-8",
            )

    def validate(group: Path, _key: Path, _restore_lister):
        assert group.name == "business-20260715T010203Z-0011223344556677"
        assert calls[-1][0] == "business_fetch"
        return {
            "backup_run_id": group.name,
            "backup_cutoff_utc": "2026-07-15T01:02:03Z",
        }

    validated_ledgers: list[str] = []

    def validate_ledger(group: Path, _key: Path, _history, *, minimum_cutoff_utc: str):
        validated_ledgers.append(group.name)
        return {
            "archive_run_id": group.name,
            "cutoff_utc": minimum_cutoff_utc,
            "signing_key_version": "v1",
        }

    scheduler.run_once(
        scheduler_env(tmp_path),
        now=lambda: datetime(2026, 7, 15, 1, 2, 3, tzinfo=timezone.utc),
        token_hex=lambda _size: "0011223344556677",
        execute=execute,
        validate_group=validate,
        validate_ledger_group=validate_ledger,
        lock=unlocked,
    )

    assert [stage for stage, _ in calls] == [
        "ledger_publish",
        "ledger_fetch",
        "business_publish",
        "business_fetch",
        "business_catalog",
        "catalog_ledger_fetch",
        "catalog_ledger_fetch",
    ]
    assert validated_ledgers == [
        "ledger-previous",
        "ledger-20260715T010203Z-0011223344556677",
    ]
    ledger_fetch = calls[1][1]
    assert ledger_fetch[ledger_fetch.index("--config-file") + 1].endswith("ledger-verify.conf")
    state = json.loads((tmp_path / "last-success.json").read_text(encoding="utf-8"))
    assert state == {
        "backup_cutoff_utc": "2026-07-15T01:02:03Z",
        "backup_run_id": "business-20260715T010203Z-0011223344556677",
        "schema_version": 2,
        "valid_restore_points": 2,
    }
    metrics = (tmp_path / "metrics" / "backup.prom").read_text(encoding="ascii")
    assert "ux09_backup_last_valid_restore_point_unixtime_seconds 1784077323" in metrics
    assert "ux09_backup_valid_restore_points 2" in metrics
    assert "ux09_backup_freshness_warning_threshold_seconds 64800" in metrics
    assert "ux09_backup_rpo_page_threshold_seconds 86400" in metrics
    if os.name != "nt":
        assert (tmp_path / "metrics").stat().st_mode & 0o777 == 0o755


def test_run_fails_closed_when_remote_catalog_has_fewer_than_two_valid_points(tmp_path: Path) -> None:
    scheduler = load_scheduler()

    def execute(stage: str, command: list[str], _environment: dict[str, str]) -> None:
        if stage.endswith("fetch"):
            Path(command[command.index("--output") + 1]).mkdir(parents=True)
        if stage == "catalog_ledger_fetch":
            write_fake_ledger_fetch(command)
        if stage == "business_catalog":
            Path(command[command.index("--output") + 1]).write_text(
                json.dumps(
                    [catalog_entry(
                        "business-20260715T010203Z-0011223344556677",
                        "2026-07-15T01:02:03Z",
                        1,
                        "ledger-20260715T010203Z-0011223344556677",
                    )]
                ),
                encoding="utf-8",
            )

    def validate(group: Path, *_args):
        return {"backup_run_id": group.name, "backup_cutoff_utc": "2026-07-15T01:02:03Z"}

    with pytest.raises(ValueError, match="at least two"):
        scheduler.run_once(
            scheduler_env(tmp_path),
            now=lambda: datetime(2026, 7, 15, 1, 2, 3, tzinfo=timezone.utc),
            token_hex=lambda _size: "0011223344556677",
            execute=execute,
            validate_group=validate,
            lock=unlocked,
        )

    assert not (tmp_path / "last-success.json").exists()


def test_run_preserves_reconciliation_identity_and_blocks_next_run(tmp_path: Path) -> None:
    scheduler = load_scheduler()

    def fail_unknown_publish(stage: str, command: list[str], _environment: dict[str, str]) -> None:
        if stage.endswith("fetch"):
            Path(command[command.index("--output") + 1]).mkdir(parents=True)
        if stage == "business_publish":
            raise scheduler.StageFailure(stage, 76)

    with pytest.raises(scheduler.StageFailure) as error:
        scheduler.run_once(
            scheduler_env(tmp_path),
            now=lambda: datetime(2026, 7, 15, 1, 2, 3, tzinfo=timezone.utc),
            token_hex=lambda _size: "0011223344556677",
            execute=fail_unknown_publish,
            lock=unlocked,
        )

    assert error.value.run_id == "business-20260715T010203Z-0011223344556677"
    pending = json.loads((tmp_path / "pending-run.json").read_text(encoding="utf-8"))
    assert pending == {
        "business_run_id": "business-20260715T010203Z-0011223344556677",
        "cutoff_utc": "2026-07-15T01:02:03Z",
        "ledger_run_id": "ledger-20260715T010203Z-0011223344556677",
        "schema_version": 1,
        "stage": "business_publish",
        "state": "reconciliation_required",
    }
    workspace = tmp_path / "runs" / pending["business_run_id"]
    assert workspace.is_dir()

    with pytest.raises(ValueError, match="reconciliation"):
        scheduler.run_once(
            scheduler_env(tmp_path),
            execute=lambda *_args: pytest.fail("a new run must not start"),
            lock=unlocked,
        )


def test_run_requires_every_counted_business_point_to_have_a_valid_ledger(tmp_path: Path) -> None:
    scheduler = load_scheduler()

    def execute(stage: str, command: list[str], _environment: dict[str, str]) -> None:
        if stage.endswith("fetch"):
            Path(command[command.index("--output") + 1]).mkdir(parents=True)
        if stage == "business_catalog":
            Path(command[command.index("--output") + 1]).write_text(
                json.dumps([
                    catalog_entry("business-previous", "2026-07-14T13:02:03Z", 1, "ledger-missing"),
                    catalog_entry(
                        "business-20260715T010203Z-0011223344556677",
                        "2026-07-15T01:02:03Z",
                        2,
                        "ledger-20260715T010203Z-0011223344556677",
                    ),
                ]),
                encoding="utf-8",
            )

    def validate_business(group: Path, *_args):
        return {"backup_run_id": group.name, "backup_cutoff_utc": "2026-07-15T01:02:03Z"}

    def reject_missing_ledger(group: Path, *_args, **_kwargs):
        if group.name == "ledger-missing":
            raise ValueError("historical ledger is unavailable")
        return {"archive_run_id": group.name, "cutoff_utc": "2026-07-15T01:02:03Z"}

    with pytest.raises(ValueError, match="historical ledger"):
        scheduler.run_once(
            scheduler_env(tmp_path),
            now=lambda: datetime(2026, 7, 15, 1, 2, 3, tzinfo=timezone.utc),
            token_hex=lambda _size: "0011223344556677",
            execute=execute,
            validate_group=validate_business,
            validate_ledger_group=reject_missing_ledger,
            lock=unlocked,
        )

    assert not (tmp_path / "last-success.json").exists()


def test_run_fails_closed_when_remote_catalog_contains_non_objects(tmp_path: Path) -> None:
    scheduler = load_scheduler()

    def execute(stage: str, command: list[str], _environment: dict[str, str]) -> None:
        if stage.endswith("fetch"):
            Path(command[command.index("--output") + 1]).mkdir(parents=True)
        if stage == "business_catalog":
            Path(command[command.index("--output") + 1]).write_text(
                '["not-a-catalog-object"]',
                encoding="utf-8",
            )

    def validate(group: Path, *_args):
        return {"backup_run_id": group.name, "backup_cutoff_utc": "2026-07-15T01:02:03Z"}

    with pytest.raises(ValueError, match="catalog entries"):
        scheduler.run_once(
            scheduler_env(tmp_path),
            now=lambda: datetime(2026, 7, 15, 1, 2, 3, tzinfo=timezone.utc),
            token_hex=lambda _size: "0011223344556677",
            execute=execute,
            validate_group=validate,
            lock=unlocked,
        )


def test_stage_failure_is_nonzero_and_preserves_previous_freshness(tmp_path: Path) -> None:
    scheduler = load_scheduler()
    previous = tmp_path / "last-success.json"
    previous.write_text('{"previous":true}\n', encoding="utf-8")

    def fail_ledger(stage: str, _command: list[str], _environment: dict[str, str]) -> None:
        assert stage == "ledger_publish"
        raise scheduler.StageFailure(stage, 74)

    with pytest.raises(scheduler.StageFailure) as error:
        scheduler.run_once(
            scheduler_env(tmp_path),
            execute=fail_ledger,
            lock=unlocked,
        )

    assert error.value.exit_code == 74
    assert previous.read_text(encoding="utf-8") == '{"previous":true}\n'
    assert not (tmp_path / "metrics" / "backup.prom").exists()


def test_validation_failure_never_advances_freshness(tmp_path: Path) -> None:
    scheduler = load_scheduler()

    def execute(_stage: str, command: list[str], _environment: dict[str, str]) -> None:
        if "--output" in command:
            Path(command[command.index("--output") + 1]).mkdir(parents=True)

    def reject(*_args):
        raise ValueError("remote complete group is invalid")

    with pytest.raises(ValueError, match="invalid"):
        scheduler.run_once(
            scheduler_env(tmp_path),
            execute=execute,
            validate_group=reject,
            lock=unlocked,
        )

    assert not (tmp_path / "last-success.json").exists()
    assert not (tmp_path / "metrics" / "backup.prom").exists()


@pytest.mark.parametrize(
    ("state", "now", "message"),
    [
        (None, "2026-07-15T20:00:00Z", "missing"),
        ({"schema_version": 2, "backup_run_id": "business-safe", "backup_cutoff_utc": "bad", "valid_restore_points": 2}, "2026-07-15T20:00:00Z", "timestamp"),
        ({"schema_version": 2, "backup_run_id": "business-safe", "backup_cutoff_utc": "2026-07-14T00:00:00Z", "valid_restore_points": 2}, "2026-07-15T20:00:00Z", "stale"),
        ({"schema_version": 2, "backup_run_id": "business-safe", "backup_cutoff_utc": "2026-07-15T20:10:00Z", "valid_restore_points": 2}, "2026-07-15T20:00:00Z", "future"),
        ({"schema_version": 2, "backup_run_id": "business-safe", "backup_cutoff_utc": "2026-07-15T08:00:00Z", "valid_restore_points": 1}, "2026-07-15T20:00:00Z", "at least two"),
    ],
)
def test_freshness_check_fails_closed(
    tmp_path: Path,
    state: dict[str, object] | None,
    now: str,
    message: str,
) -> None:
    scheduler = load_scheduler()
    if state is not None:
        (tmp_path / "last-success.json").write_text(json.dumps(state), encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        scheduler.check_freshness(
            scheduler_env(tmp_path),
            now=datetime.strptime(now, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc),
        )


def test_freshness_check_reports_age_for_valid_state(tmp_path: Path) -> None:
    scheduler = load_scheduler()
    state = {
        "schema_version": 2,
        "backup_run_id": "business-safe",
        "backup_cutoff_utc": "2026-07-15T08:00:00Z",
        "valid_restore_points": 2,
    }
    (tmp_path / "last-success.json").write_text(json.dumps(state), encoding="utf-8")

    assert scheduler.check_freshness(
        scheduler_env(tmp_path),
        now=datetime(2026, 7, 15, 20, 0, tzinfo=timezone.utc),
    ) == 43200


def test_run_stage_emits_structured_secret_safe_failure_log() -> None:
    scheduler = load_scheduler()
    output = io.StringIO()
    secret = "never-log-this-credential"

    def failed_run(*_args, **_kwargs):
        return subprocess.CompletedProcess([], 76, stdout="", stderr=secret)

    with pytest.raises(scheduler.StageFailure):
        scheduler.run_stage(
            "business_publish",
            ["/opt/ux09-backup/backupctl.py", "backup"],
            {"CREDENTIAL": secret},
            subprocess_run=failed_run,
            log_stream=output,
        )

    line = output.getvalue()
    assert secret not in line
    event = json.loads(line)
    assert event["event"] == "backup_stage"
    assert event["stage"] == "business_publish"
    assert event["result"] == "failed"
    assert event["exit_code"] == 76


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode and inode contract requires Linux")
@pytest.mark.parametrize("kind", ["wide-mode", "hardlink", "same-inode"])
def test_scheduler_rejects_unprotected_or_shared_config_inode(
    tmp_path: Path, kind: str
) -> None:
    scheduler = load_scheduler()
    environment = scheduler_env(tmp_path)
    config = Path(environment["BACKUP_VERIFY_CONFIG_FILE"])
    if kind == "wide-mode":
        config.chmod(0o640)
        message = "0600|permission"
    elif kind == "hardlink":
        os.link(config, tmp_path / "shared-config")
        message = "hardlink|inode|link"
    else:
        environment["LEDGER_VERIFY_CONFIG_FILE"] = str(config)
        message = "distinct|inode"

    with pytest.raises(ValueError, match=message):
        scheduler.run_once(environment, lock=unlocked)


@pytest.mark.skipif(os.name == "nt", reason="real fcntl lock requires Linux/POSIX")
def test_exclusive_lock_rejects_overlap_on_linux(tmp_path: Path) -> None:
    scheduler = load_scheduler()
    lock_path = tmp_path / "scheduler.lock"
    with scheduler.exclusive_lock(lock_path):
        with pytest.raises(scheduler.OverlapFailure):
            with scheduler.exclusive_lock(lock_path):
                pass


def _bash() -> str:
    git_bash = Path(r"C:\Program Files\Git\bin\bash.exe")
    executable = str(git_bash) if git_bash.is_file() else shutil.which("bash")
    if not executable:
        pytest.skip("bash is required for Linux deployment script tests")
    return executable


def _fake_systemd_analyze(tmp_path: Path) -> Path:
    helper = tmp_path / "systemd-analyze"
    helper.write_text(
        "#!/bin/sh\n"
        "set -eu\n"
        "case \"$1\" in\n"
        "  calendar) case \"$2\" in *INVALID*) exit 1 ;; esac ;;\n"
        "  verify) test -f \"$2\"; test -f \"$3\" ;;\n"
        "  *) exit 2 ;;\n"
        "esac\n",
        encoding="utf-8",
        newline="\n",
    )
    helper.chmod(0o755)
    return helper


def _shell_path(path: Path) -> str:
    resolved = path.resolve().as_posix()
    if os.name == "nt":
        drive, remainder = resolved.split(":", 1)
        return f"/{drive.lower()}{remainder}"
    return resolved


def _render_systemd(
    tmp_path: Path,
    *,
    environment_file_content: str = "BACKUP_FRESHNESS_THRESHOLD_SECONDS=64800\n",
    environment_file_mode: int = 0o600,
    share_environment_inode: bool = False,
    use_shared_environment_path: bool = False,
    **overrides: str,
) -> subprocess.CompletedProcess[str]:
    assert SYSTEMD_RENDERER.is_file(), "systemd unit renderer is missing"
    output = tmp_path / "units"
    environment_file = tmp_path / "backup.env"
    environment = {
        **os.environ,
        "UX09_DEPLOY_ROOT": _shell_path(ROOT),
        "BACKUP_SYSTEMD_ENV_FILE": _shell_path(environment_file),
        "BACKUP_SYSTEMD_OUTPUT_DIR": _shell_path(output),
        "SYSTEMD_ANALYZE_BIN": _shell_path(_fake_systemd_analyze(tmp_path)),
        **overrides,
    }
    environment_file.write_text(environment_file_content, encoding="ascii")
    environment_file.chmod(environment_file_mode)
    if share_environment_inode:
        shared_environment_file = tmp_path / "backup-shared.env"
        os.link(environment_file, shared_environment_file)
        if use_shared_environment_path:
            environment["BACKUP_SYSTEMD_ENV_FILE"] = _shell_path(shared_environment_file)
    return subprocess.run(
        [_bash(), str(SYSTEMD_RENDERER)],
        cwd=ROOT,
        env=environment,
        text=True,
        encoding="utf-8",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def test_systemd_renderer_produces_persistent_twice_daily_timer(tmp_path: Path) -> None:
    result = _render_systemd(tmp_path)
    assert result.returncode == 0, result.stderr
    timer = (tmp_path / "units" / "ux09-backup.timer").read_text(encoding="utf-8")
    service = (tmp_path / "units" / "ux09-backup.service").read_text(encoding="utf-8")
    assert "OnCalendar=*-*-* 00,12:00:00" in timer
    assert "Persistent=true" in timer
    assert "RandomizedDelaySec=900" in timer
    assert "TimeoutStartSec=21600" in service
    assert "/usr/bin/docker compose" in service
    assert "deploy/compose.backup.yaml" in service
    assert "run --rm backup-tool" in service
    assert "deploy/backup/scheduled-backup.py run" not in service
    assert "@" not in timer + service


def test_backup_compose_runs_scheduler_inside_immutable_tool_image() -> None:
    compose = BACKUP_COMPOSE.read_text(encoding="utf-8")
    assert '${BACKUP_IMAGE:?Set BACKUP_IMAGE' in compose
    assert '${BACKUP_IMAGE_DIGEST:?Set BACKUP_IMAGE_DIGEST' in compose
    assert '/opt/ux09-backup/scheduled-backup.py' in compose
    assert 'BACKUP_STATE_DIR: /var/lib/ux09-backup' in compose
    assert 'source: /var/lib/ux09-backup' in compose
    assert 'target: /var/lib/ux09-backup' in compose
    assert 'name: ${UX09_PRIVATE_NETWORK:-ux09_private}' in compose
    assert 'ports:' not in compose
    assert 'cap_drop: [ALL]' in compose
    assert 'no-new-privileges:true' in compose
    assert 'read_only: true' in compose
    assert 'mode: 0400' in compose
    service = (ROOT / "deploy" / "backup" / "systemd" / "ux09-backup.service.in").read_text(
        encoding="utf-8"
    )
    assert "install -d -o 10001 -g 10001 -m 0700 /var/lib/ux09-backup" in service
    assert "${BACKUP_STATE_DIR}" not in service


@pytest.mark.parametrize(
    ("override", "value", "message"),
    [
        ("UX09_DEPLOY_ROOT", "relative/path", "absolute"),
        ("BACKUP_RANDOMIZED_DELAY_SEC", "fifteen", "integer"),
        ("BACKUP_SERVICE_TIMEOUT_SEC", "0", "between"),
        ("BACKUP_ON_CALENDAR", "INVALID calendar", "calendar"),
        ("BACKUP_ON_CALENDAR", "*-*-* 00:00:00\nExecStart=/bin/true", "single line"),
    ],
)
def test_systemd_renderer_rejects_unsafe_configuration(
    tmp_path: Path,
    override: str,
    value: str,
    message: str,
) -> None:
    result = _render_systemd(tmp_path, **{override: value})
    assert result.returncode != 0
    assert message in result.stderr.lower()


def test_systemd_renderer_rejects_inline_secret_values(tmp_path: Path) -> None:
    result = _render_systemd(
        tmp_path,
        environment_file_content="BACKUP_DB_PASSWORD=must-not-be-inline\n",
    )
    assert result.returncode != 0
    assert "secret" in result.stderr.lower()


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode and inode contract requires Linux")
@pytest.mark.parametrize(
    ("mode", "share_inode", "use_shared_path", "message"),
    [
        (0o640, False, False, "0600|permission"),
        (0o660, False, False, "0600|permission"),
        (0o600, True, False, "hardlink|inode"),
        (0o600, True, True, "hardlink|inode"),
    ],
)
def test_systemd_renderer_rejects_wide_or_shared_environment_file(
    tmp_path: Path,
    mode: int,
    share_inode: bool,
    use_shared_path: bool,
    message: str,
) -> None:
    result = _render_systemd(
        tmp_path,
        environment_file_mode=mode,
        share_environment_inode=share_inode,
        use_shared_environment_path=use_shared_path,
    )
    assert result.returncode != 0
    assert re.search(message, result.stderr, re.IGNORECASE)


def test_backup_freshness_rules_warn_at_18h_page_at_24h_and_fail_closed() -> None:
    rules = ALERT_RULES.read_text(encoding="utf-8")
    assert "ux09_backup_last_success_age_seconds" in rules
    assert "BackupRestorePointFreshnessWarning" in rules
    assert "ux09_backup_freshness_warning_threshold_seconds" in rules
    assert "BackupRestorePointFreshnessCritical" in rules
    assert "ux09_backup_rpo_page_threshold_seconds" in rules
    assert "BackupRestorePointSignalMissing" in rules
    assert "absent(ux09_backup_last_valid_restore_point_unixtime_seconds)" in rules
    assert "ux09_backup_valid_restore_points < 2" in rules
