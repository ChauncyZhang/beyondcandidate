from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess


ROOT = Path(__file__).resolve().parents[2]
PREFLIGHT = ROOT / "deploy" / "observability-preflight.sh"
RUNBOOK = ROOT / "deploy" / "observability" / "runbook.md"


def _shell_path(path: Path) -> str:
    value = path.resolve().as_posix()
    if len(value) >= 3 and value[1:3] == ":/":
        return f"/{value[0].lower()}{value[2:]}"
    return value


def test_observability_preflight_is_executable_and_calls_fixed_three_file_model(
    tmp_path: Path,
) -> None:
    assert PREFLIGHT.is_file()
    index_entry = subprocess.run(
        ["git", "ls-files", "--stage", "--", str(PREFLIGHT.relative_to(ROOT))],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert index_entry.startswith("100755 ")
    source = PREFLIGHT.read_text(encoding="utf-8")
    assert "production-preflight.sh" in source

    docker_shim = tmp_path / "docker"
    docker_shim.write_text(
        """#!/bin/sh
printf '%s\n' "$*" >> "$COMPOSE_CALL_LOG"
if [ "$1" = compose ] && [ "$2" = version ] && [ "$3" = --short ]; then
    printf '%s\n' '2.24.4'
fi
case " $* " in
    *" config --format json "*)
        printf '%s\n' '{"services":{"api":{"image":"registry.test/app@sha256:1111111111111111111111111111111111111111111111111111111111111111"},"worker":{"image":"registry.test/app@sha256:1111111111111111111111111111111111111111111111111111111111111111"},"queue-exporter":{"image":"registry.test/app@sha256:1111111111111111111111111111111111111111111111111111111111111111"},"proxy":{"image":"registry.test/frontend@sha256:2222222222222222222222222222222222222222222222222222222222222222"}}}'
        ;;
esac
""",
        encoding="utf-8",
    )
    docker_shim.chmod(0o755)
    curl_shim = tmp_path / "curl"
    curl_shim.write_text(
        """#!/bin/sh
printf '%s\n' "$*" >> "$NETWORK_CALL_LOG"
exit 93
""",
        encoding="utf-8",
    )
    curl_shim.chmod(0o755)
    call_log = tmp_path / "compose-calls.log"
    network_call_log = tmp_path / "network-calls.log"
    environment = os.environ.copy()
    environment.update(
        {
            "COMPOSE_CALL_LOG": _shell_path(call_log),
            "COMPOSE_ENV_FILE": "deploy/.env.example",
            "NETWORK_CALL_LOG": _shell_path(network_call_log),
        }
    )
    shell = shutil.which("sh") or "sh"

    result = subprocess.run(
        [
            shell,
            "-c",
            'PATH="$1:$PATH"; export PATH; exec sh "$2"',
            "observability-preflight-test",
            _shell_path(tmp_path),
            _shell_path(PREFLIGHT),
        ],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    calls = call_log.read_text(encoding="utf-8").splitlines()
    assert calls[0] == "compose version --short"
    config_calls = [line.replace("\\", "/") for line in calls if " config " in line]
    assert len(config_calls) == 2
    assert "deploy/compose.yaml" in config_calls[0]
    assert "deploy/compose.production.yaml" in config_calls[0]
    assert "deploy/compose.observability.yaml" not in config_calls[0]
    assert "deploy/compose.yaml" in config_calls[1]
    assert "deploy/compose.production.yaml" in config_calls[1]
    assert "deploy/compose.observability.yaml" in config_calls[1]
    assert not network_call_log.exists()


def _run_production_preflight(
    tmp_path: Path, remote_runbook: Path, *, curl_exit_code: int = 0
) -> tuple[subprocess.CompletedProcess[str], Path]:
    docker_shim = tmp_path / "docker"
    docker_shim.write_text(
        """#!/bin/sh
if [ "$1" = compose ] && [ "$2" = version ] && [ "$3" = --short ]; then
    printf '%s\n' '2.24.4'
fi
case " $* " in
    *" config --format json "*)
        printf '%s\n' '{"services":{"api":{"image":"registry.test/app@sha256:1111111111111111111111111111111111111111111111111111111111111111"},"worker":{"image":"registry.test/app@sha256:1111111111111111111111111111111111111111111111111111111111111111"},"queue-exporter":{"image":"registry.test/app@sha256:1111111111111111111111111111111111111111111111111111111111111111"},"proxy":{"image":"registry.test/frontend@sha256:2222222222222222222222222222222222222222222222222222222222222222"}}}'
        ;;
esac
""",
        encoding="utf-8",
    )
    docker_shim.chmod(0o755)
    curl_shim = tmp_path / "curl"
    curl_shim.write_text(
        """#!/bin/sh
printf '%s\n' "$*" >> "$NETWORK_CALL_LOG"
if [ "$CURL_EXIT_CODE" -ne 0 ]; then
    printf '%s\n' 'curl: simulated HTTP 404' >&2
    exit "$CURL_EXIT_CODE"
fi
case "$*" in
    *raw.githubusercontent.com*) cat "$REMOTE_RUNBOOK_SOURCE" ;;
esac
""",
        encoding="utf-8",
    )
    curl_shim.chmod(0o755)
    network_call_log = tmp_path / "network-calls.log"
    environment = os.environ.copy()
    environment.update(
        {
            "COMPOSE_ENV_FILE": "deploy/.env.example",
            "CURL_EXIT_CODE": str(curl_exit_code),
            "NETWORK_CALL_LOG": _shell_path(network_call_log),
            "OBSERVABILITY_PREFLIGHT_MODE": "production",
            "REMOTE_RUNBOOK_SOURCE": _shell_path(remote_runbook),
        }
    )
    shell = shutil.which("sh") or "sh"
    result = subprocess.run(
        [
            shell,
            "-c",
            'PATH="$1:$PATH"; export PATH; exec sh "$2"',
            "observability-production-preflight-test",
            _shell_path(tmp_path),
            _shell_path(PREFLIGHT),
        ],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    return result, network_call_log


def test_production_preflight_checks_canonical_blob_raw_and_remote_anchors(
    tmp_path: Path,
) -> None:
    remote_runbook = tmp_path / "remote-runbook.md"
    remote_runbook.write_text(RUNBOOK.read_text(encoding="utf-8"), encoding="utf-8")

    result, network_call_log = _run_production_preflight(tmp_path, remote_runbook)

    assert result.returncode == 0, result.stderr
    assert network_call_log.is_file()
    calls = network_call_log.read_text(encoding="utf-8")
    assert (
        "https://github.com/ChauncyZhang/beyondcandidate/blob/main/"
        "deploy/observability/runbook.md" in calls
    )
    assert (
        "https://raw.githubusercontent.com/ChauncyZhang/beyondcandidate/main/"
        "deploy/observability/runbook.md" in calls
    )


def test_production_preflight_rejects_remote_runbook_with_only_matching_headings(
    tmp_path: Path,
) -> None:
    remote_runbook = tmp_path / "remote-runbook.md"
    headings_only = "\n".join(
        line
        for line in RUNBOOK.read_text(encoding="utf-8").splitlines()
        if line.startswith("#")
    )
    remote_runbook.write_text(f"{headings_only}\n", encoding="utf-8")

    result, _ = _run_production_preflight(tmp_path, remote_runbook)

    assert result.returncode != 0
    assert "content" in result.stderr.lower()


def test_production_preflight_rejects_stale_remote_runbook_body(
    tmp_path: Path,
) -> None:
    remote_runbook = tmp_path / "remote-runbook.md"
    stale = RUNBOOK.read_text(encoding="utf-8").replace(
        "The first-release SLIs", "The stale-release SLIs", 1
    )
    remote_runbook.write_text(stale, encoding="utf-8")

    result, _ = _run_production_preflight(tmp_path, remote_runbook)

    assert result.returncode != 0
    assert "content" in result.stderr.lower()


def test_production_preflight_fails_closed_when_canonical_runbook_is_404(
    tmp_path: Path,
) -> None:
    remote_runbook = tmp_path / "remote-runbook.md"
    remote_runbook.write_text(RUNBOOK.read_text(encoding="utf-8"), encoding="utf-8")

    result, _ = _run_production_preflight(
        tmp_path, remote_runbook, curl_exit_code=22
    )

    assert result.returncode == 22
    assert "404" in result.stderr


def test_runbook_requires_main_publication_before_production_alert_deploy() -> None:
    runbook = RUNBOOK.read_text(encoding="utf-8")

    assert "OBSERVABILITY_PREFLIGHT_MODE=production" in runbook
    assert "published to `main`" in runbook
    assert "before deploying alert rules" in runbook
    assert "full published runbook content" in runbook
    assert "CRLF/LF" in runbook
