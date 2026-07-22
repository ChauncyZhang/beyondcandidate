from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import time
from urllib.parse import urlparse
from urllib.request import urlopen
from uuid import uuid4


ROOT = Path(__file__).resolve().parents[2]
BASE_COMPOSE = ROOT / "deploy" / "compose.yaml"
PRODUCTION_COMPOSE = ROOT / "deploy" / "compose.production.yaml"
OBSERVABILITY_COMPOSE = ROOT / "deploy" / "compose.observability.yaml"
ENV_EXAMPLE = ROOT / "deploy" / ".env.example"
PROMETHEUS_CONFIG = ROOT / "deploy" / "observability" / "prometheus.yml"
ALERT_RULES = ROOT / "deploy" / "observability" / "alerts" / "ux09.rules.yml"
ALERTMANAGER_CONFIG = ROOT / "deploy" / "observability" / "alertmanager.yml"
RUNBOOK = ROOT / "deploy" / "observability" / "runbook.md"
RUNBOOK_URL = (
    "https://github.com/ChauncyZhang/beyondcandidate/blob/main/"
    "deploy/observability/runbook.md"
)


def _compose_environment(tmp_path: Path) -> dict[str, str]:
    cert = tmp_path / "tls.crt"
    key = tmp_path / "tls.key"
    cert.touch()
    key.touch()
    environment = os.environ.copy()
    environment.update(
        {
            "HTTPS_BIND_ADDRESS": "127.0.0.1",
            "HTTPS_PORT": "443",
            "SERVER_NAME": "recruiting.example.test",
            "TLS_CERTIFICATE_PATH": str(cert),
            "TLS_PRIVATE_KEY_PATH": str(key),
            "QUEUE_METRICS_DB_USER": "ux09_queue_metrics",
            "QUEUE_METRICS_DB_PASSWORD": "synthetic-queue-metrics-password",
            "POSTGRES_EXPORTER_DB_USER": "ux09_postgres_exporter",
            "POSTGRES_EXPORTER_DB_PASSWORD": "synthetic-postgres-exporter-password",
        }
    )
    return environment


def _merged_model(tmp_path: Path) -> dict:
    result = subprocess.run(
        [
            "docker",
            "compose",
            "--env-file",
            str(ENV_EXAMPLE),
            "-f",
            str(BASE_COMPOSE),
            "-f",
            str(PRODUCTION_COMPOSE),
            "-f",
            str(OBSERVABILITY_COMPOSE),
            "config",
            "--format",
            "json",
        ],
        cwd=ROOT,
        env=_compose_environment(tmp_path),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_three_compose_files_publish_only_the_https_proxy(tmp_path: Path) -> None:
    model = _merged_model(tmp_path)
    published = [
        (name, port)
        for name, service in model["services"].items()
        for port in service.get("ports", [])
    ]

    assert len(published) == 1
    assert published[0][0] == "proxy"
    assert published[0][1]["target"] == 8443
    for name in (
        "prometheus",
        "alertmanager",
        "queue-exporter",
        "node-exporter",
        "postgres-exporter",
    ):
        assert model["services"][name].get("ports", []) == []
        assert "private" in model["services"][name]["networks"]

    assert "cadvisor" not in model["services"]
    overlay = OBSERVABILITY_COMPOSE.read_text(encoding="utf-8").lower()
    for forbidden in (
        "privileged:",
        "/var/run",
        "docker.sock",
        "/var/lib/docker",
    ):
        assert forbidden not in overlay
    root_mounts = [
        (service_name, volume)
        for service_name, service in model["services"].items()
        for volume in service.get("volumes", [])
        if volume.get("source") == "/"
    ]
    assert len(root_mounts) == 1
    service_name, root_mount = root_mounts[0]
    assert service_name == "node-exporter"
    assert root_mount["target"] == "/host/root"
    assert root_mount["read_only"] is True
    assert root_mount["bind"]["propagation"] == "rslave"

    node = model["services"]["node-exporter"]
    assert node["read_only"] is True
    assert node["cap_drop"] == ["ALL"]
    assert "no-new-privileges:true" in node["security_opt"]
    assert node["pid"] == "host"
    assert "--path.rootfs=/host/root" in node["command"]
    assert "--collector.textfile.directory=/var/lib/node_exporter/textfile_collector" in node["command"]
    textfile_mounts = [
        volume
        for volume in node["volumes"]
        if volume["target"] == "/var/lib/node_exporter/textfile_collector"
    ]
    assert len(textfile_mounts) == 1
    assert textfile_mounts[0]["source"] == "/var/lib/ux09-backup/metrics"
    assert textfile_mounts[0]["read_only"] is True
    runbook = RUNBOOK.read_text(encoding="utf-8")
    assert "production Linux host filesystems" in runbook
    assert "Docker Desktop Linux VM" in runbook

    queue_url = model["services"]["queue-exporter"]["environment"][
        "OBSERVABILITY_DATABASE_URL"
    ]
    postgres_url = model["services"]["postgres-exporter"]["environment"][
        "DATA_SOURCE_NAME"
    ]
    assert "ux09_queue_metrics" in queue_url
    assert "ux09_postgres_exporter" in postgres_url
    assert queue_url != postgres_url


def test_node_exporter_runtime_exposes_host_filesystem_series(
    tmp_path: Path,
) -> None:
    project = f"ux09-node-runtime-{uuid4().hex[:8]}"
    container = f"{project}-exporter"
    compose = [
        "docker",
        "compose",
        "-p",
        project,
        "--env-file",
        str(ENV_EXAMPLE),
        "-f",
        str(BASE_COMPOSE),
        "-f",
        str(PRODUCTION_COMPOSE),
        "-f",
        str(OBSERVABILITY_COMPOSE),
    ]
    environment = _compose_environment(tmp_path)
    backup_state = tmp_path / "backup-state"
    (backup_state / "metrics").mkdir(parents=True)
    environment["BACKUP_STATE_DIR"] = str(backup_state)
    started = subprocess.run(
        [
            *compose,
            "run",
            "-d",
            "--no-deps",
            "--name",
            container,
            "-p",
            "127.0.0.1::9100",
            "node-exporter",
        ],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    diagnostic_container: str | None = None
    if started.returncode == 0:
        container_id = started.stdout.strip()
    else:
        operating_system = subprocess.run(
            ["docker", "info", "--format", "{{.OperatingSystem}}"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        assert operating_system == "Docker Desktop"
        assert "not a shared or slave mount" in started.stderr
        subprocess.run(
            [*compose, "down", "--remove-orphans"],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        diagnostic_container = f"{container}-desktop-diagnostic"
        diagnostic = subprocess.run(
            [
                "docker",
                "run",
                "-d",
                "--name",
                diagnostic_container,
                "--read-only",
                "--cap-drop=ALL",
                "--security-opt=no-new-privileges:true",
                "--pid=host",
                "-p",
                "127.0.0.1::9100",
                "--mount",
                "type=bind,source=/,target=/host/root,readonly",
                "prom/node-exporter:v1.9.1",
                "--path.rootfs=/host/root",
                "--collector.filesystem.mount-points-exclude=^/(sys|proc|dev|host|etc)($|/)",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert diagnostic.returncode == 0, diagnostic.stdout + diagnostic.stderr
        container_id = diagnostic.stdout.strip()
    try:
        port_result = subprocess.run(
            ["docker", "port", container_id, "9100/tcp"],
            capture_output=True,
            text=True,
            check=False,
        )
        assert port_result.returncode == 0, subprocess.run(
            ["docker", "logs", container_id],
            capture_output=True,
            text=True,
            check=False,
        ).stderr
        host_port = int(port_result.stdout.strip().rsplit(":", 1)[1])
        payload = ""
        for _ in range(30):
            try:
                with urlopen(
                    f"http://127.0.0.1:{host_port}/metrics", timeout=1
                ) as response:
                    payload = response.read().decode("utf-8")
                break
            except OSError:
                time.sleep(0.1)

        avail = {
            labels: float(value)
            for labels, value in re.findall(
                r'^node_filesystem_avail_bytes\{([^}]*)\}\s+([0-9.eE+-]+)$',
                payload,
                flags=re.MULTILINE,
            )
            if re.search(r'fstype="(?!tmpfs"|overlay")[^"]+"', labels)
        }
        size = {
            labels: float(value)
            for labels, value in re.findall(
                r'^node_filesystem_size_bytes\{([^}]*)\}\s+([0-9.eE+-]+)$',
                payload,
                flags=re.MULTILINE,
            )
        }
        assert any(labels in size and size[labels] > 0 for labels in avail), payload
    finally:
        if diagnostic_container is not None:
            subprocess.run(
                ["docker", "rm", "-f", diagnostic_container],
                capture_output=True,
                text=True,
                check=False,
            )
        subprocess.run(
            [*compose, "down", "--remove-orphans"],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )


def test_prometheus_scrapes_only_private_service_targets() -> None:
    config = PROMETHEUS_CONFIG.read_text(encoding="utf-8")

    for target in (
        "api:8000",
        "queue-exporter:9108",
        "node-exporter:9100",
        "postgres-exporter:9187",
        "minio:9000",
    ):
        assert target in config
    assert "localhost" not in config
    assert "host.docker.internal" not in config
    assert "cadvisor" not in config.lower()


def test_real_prometheus_and_alertmanager_images_load_configuration() -> None:
    prometheus = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--entrypoint",
            "/bin/promtool",
            "-v",
            f"{PROMETHEUS_CONFIG}:/etc/prometheus/prometheus.yml:ro",
            "-v",
            f"{ALERT_RULES}:/etc/prometheus/rules/ux09.rules.yml:ro",
            "prom/prometheus:v3.5.0",
            "check",
            "config",
            "/etc/prometheus/prometheus.yml",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert prometheus.returncode == 0, prometheus.stdout + prometheus.stderr

    alertmanager = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--entrypoint",
            "/bin/amtool",
            "-v",
            f"{ALERTMANAGER_CONFIG}:/etc/alertmanager/alertmanager.yml:ro",
            "prom/alertmanager:v0.28.1",
            "check-config",
            "/etc/alertmanager/alertmanager.yml",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert alertmanager.returncode == 0, alertmanager.stdout + alertmanager.stderr


def test_alerts_have_durations_runbooks_and_no_governance_contract() -> None:
    rules = ALERT_RULES.read_text(encoding="utf-8")
    runbook = RUNBOOK.read_text(encoding="utf-8")

    assert rules.count("for:") >= 12
    assert rules.count("runbook_url:") >= 12
    for required in (
        "ApiHigh5xxRate",
        "ApiHighLatency",
        "QueueOldestReadyTooOld",
        "QueueExpiredLeases",
        "QueueDeadLetters",
        "ParseFailureRateHigh",
        "LlmFailureRateHigh",
        "HostStorageLow",
        "PostgresConnectionsHigh",
    ):
        assert required in rules
    assert "BackupStale" not in rules
    assert 'sum by (dependency) (rate(ux09_readiness_checks_total{result=~"failed|cancelled"}[5m])) > 0' in rules
    assert "governance" not in rules.lower()

    anchors = {
        re.sub(r"[^a-z0-9 -]", "", heading.lower()).replace(" ", "-")
        for heading in re.findall(r"^### (.+)$", runbook, flags=re.MULTILINE)
    }
    urls = re.findall(r"runbook_url:\s*(\S+)", rules)
    for url in urls:
        parsed = urlparse(url)
        assert parsed.scheme == "https"
        assert url.startswith(f"{RUNBOOK_URL}#")
        assert parsed.fragment in anchors


def test_promtool_triggers_and_resolves_representative_alerts(tmp_path: Path) -> None:
    rule_test = tmp_path / "rules-test.yml"
    rule_test.write_text(
        """
rule_files:
  - /etc/prometheus/rules/ux09.rules.yml
evaluation_interval: 1m
tests:
  - interval: 1m
    input_series:
      - series: 'ux09_expired_leases{queue="job"}'
        values: '1x20 0x20'
    alert_rule_test:
      - eval_time: 15m
        alertname: QueueExpiredLeases
        exp_alerts:
          - exp_labels: {severity: warning}
            exp_annotations:
              summary: Queue leases remain expired
              runbook_url: {RUNBOOK_URL}#queueexpiredleases
      - eval_time: 30m
        alertname: QueueExpiredLeases
        exp_alerts: []
  - interval: 1m
    input_series:
      - series: 'ux09_job_dead_letters{job_type="screening.parse_item",organization="alice.canary@example.test"}'
        values: '1x15 0x15'
    alert_rule_test:
      - eval_time: 10m
        alertname: QueueDeadLetters
        exp_alerts:
          - exp_labels: {severity: warning}
            exp_annotations:
              summary: Dead-letter jobs require operator review
              runbook_url: {RUNBOOK_URL}#queuedeadletters
      - eval_time: 25m
        alertname: QueueDeadLetters
        exp_alerts: []
  - interval: 1m
    input_series:
      - series: 'ux09_readiness_checks_total{dependency="storage",result="cancelled"}'
        values: '0+60x10 600+0x20'
    alert_rule_test:
      - eval_time: 10m
        alertname: ApiReadinessFailure
        exp_alerts:
          - exp_labels: {dependency: storage, severity: warning}
            exp_annotations:
              summary: A required API dependency is failing readiness checks
              runbook_url: {RUNBOOK_URL}#apireadinessfailure
      - eval_time: 25m
        alertname: ApiReadinessFailure
        exp_alerts: []
  - interval: 1m
    input_series:
      - series: 'node_filesystem_avail_bytes{device="disk",fstype="ext4",mountpoint="/data"}'
        values: '10x20 50x20'
      - series: 'node_filesystem_size_bytes{device="disk",fstype="ext4",mountpoint="/data"}'
        values: '100x40'
    alert_rule_test:
      - eval_time: 16m
        alertname: HostStorageLowWarning
        exp_alerts:
          - exp_labels: {severity: warning}
            exp_annotations:
              summary: Host filesystem free capacity is below 20 percent
              runbook_url: {RUNBOOK_URL}#hoststoragelowwarning
      - eval_time: 30m
        alertname: HostStorageLowWarning
        exp_alerts: []
  - interval: 1m
    input_series:
      - series: 'ux09_job_attempts_total{job_type="screening.parse_item",result="failed",error_class="parse"}'
        values: '0+60x40 2400+0x30'
    alert_rule_test:
      - eval_time: 30m
        alertname: ParseFailureRateHigh
        exp_alerts:
          - exp_labels: {severity: warning}
            exp_annotations:
              summary: Resume parse failure ratio is above 10 percent
              runbook_url: {RUNBOOK_URL}#parsefailureratehigh
      - eval_time: 60m
        alertname: ParseFailureRateHigh
        exp_alerts: []
""".lstrip().replace("{RUNBOOK_URL}", RUNBOOK_URL),
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--entrypoint",
            "/bin/promtool",
            "-v",
            f"{ALERT_RULES}:/etc/prometheus/rules/ux09.rules.yml:ro",
            "-v",
            f"{rule_test}:/tmp/rules-test.yml:ro",
            "prom/prometheus:v3.5.0",
            "test",
            "rules",
            "/tmp/rules-test.yml",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
