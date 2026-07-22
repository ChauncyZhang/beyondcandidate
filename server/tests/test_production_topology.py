from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import ssl
import subprocess
import time
from urllib.error import HTTPError, URLError
from urllib.request import urlopen
from uuid import uuid4

import pytest


ROOT = Path(__file__).resolve().parents[2]
BASE_COMPOSE = ROOT / "deploy" / "compose.yaml"
PRODUCTION_COMPOSE = ROOT / "deploy" / "compose.production.yaml"
OBSERVABILITY_COMPOSE = ROOT / "deploy" / "compose.observability.yaml"
ENV_EXAMPLE = ROOT / "deploy" / ".env.example"
NGINX_TEMPLATE = ROOT / "deploy" / "nginx" / "production.conf.template"
LOCAL_NGINX_CONFIG = ROOT / "deploy" / "nginx" / "default.conf"
SECURITY_HEADERS = ROOT / "deploy" / "nginx" / "snippets" / "security-headers.conf"
PRODUCTION_PREFLIGHT = ROOT / "deploy" / "production-preflight.sh"
FRONTEND_DOCKERFILE = ROOT / "deploy" / "nginx" / "Dockerfile"
FRONTEND_PACKAGE = ROOT
RELEASE_COMPOSE_VALIDATOR = ROOT / "deploy" / "release-compose-validator.py"
SERVER_DOCKERFILE = ROOT / "server" / "Dockerfile"

APP_IMAGE = "registry.synthetic.test/ux09-server"
APP_IMAGE_DIGEST = f"sha256:{'1' * 64}"
FRONTEND_IMAGE = "registry.synthetic.test/ux09-frontend"
FRONTEND_IMAGE_DIGEST = f"sha256:{'2' * 64}"

FORBIDDEN_API_ENVIRONMENT = {
    "GOVERNANCE_DATABASE_URL",
    "GOVERNANCE_DELETE_ACCESS_KEY",
    "GOVERNANCE_DELETE_SECRET_KEY",
    "GOVERNANCE_LEDGER_ACCESS_KEY",
    "GOVERNANCE_LEDGER_SECRET_KEY",
    "GOVERNANCE_LEDGER_SIGNING_KEY",
    "GOVERNANCE_STORAGE_ENDPOINT",
}


def _compose_environment(
    cert_path: Path, key_path: Path, *, server_name: str | None
) -> dict[str, str]:
    environment = os.environ.copy()
    environment.update(
        {
            "HTTPS_BIND_ADDRESS": "127.0.0.1",
            "HTTPS_PORT": "443",
            "APP_IMAGE": APP_IMAGE,
            "APP_IMAGE_DIGEST": APP_IMAGE_DIGEST,
            "FRONTEND_IMAGE": FRONTEND_IMAGE,
            "FRONTEND_IMAGE_DIGEST": FRONTEND_IMAGE_DIGEST,
            "QUEUE_METRICS_DB_PASSWORD": "synthetic-queue-metrics-password",
            "QUEUE_METRICS_DB_USER": "ux09_queue_metrics",
            "POSTGRES_EXPORTER_DB_PASSWORD": "synthetic-postgres-exporter-password",
            "POSTGRES_EXPORTER_DB_USER": "ux09_postgres_exporter",
            "TLS_CERTIFICATE_PATH": str(cert_path),
            "TLS_PRIVATE_KEY_PATH": str(key_path),
        }
    )
    if server_name is None:
        environment.pop("SERVER_NAME", None)
    else:
        environment["SERVER_NAME"] = server_name
    return environment


def _run_compose_config(environment: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
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
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )


def _merged_compose(cert_path: Path, key_path: Path) -> dict:
    environment = _compose_environment(
        cert_path, key_path, server_name="recruiting.example.test"
    )
    result = _run_compose_config(environment)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _run_release_compose_validator(model: dict) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["python", str(RELEASE_COMPOSE_VALIDATOR)],
        cwd=ROOT,
        input=json.dumps(model),
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.fixture(scope="module")
def frontend_release_image() -> str:
    image = f"ux09-frontend-topology-test:{uuid4().hex[:12]}"
    built = subprocess.run(
        [
            "docker",
            "build",
            "--pull=false",
            "-t",
            image,
            "-f",
            str(FRONTEND_DOCKERFILE),
            str(FRONTEND_PACKAGE),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert built.returncode == 0, built.stdout + built.stderr
    try:
        yield image
    finally:
        subprocess.run(
            ["docker", "image", "rm", "-f", image],
            capture_output=True,
            text=True,
            check=False,
        )


def _generate_certificate(cert_path: Path, key_path: Path) -> None:
    openssl = shutil.which("openssl")
    assert openssl is not None, "OpenSSL is required for the disposable TLS syntax gate"
    subprocess.run(
        [
            openssl,
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "1",
            "-subj",
            "/CN=recruiting.example.test",
            "-keyout",
            str(key_path),
            "-out",
            str(cert_path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )


def _render_nginx(tmp_path: Path) -> tuple[Path, Path, Path]:
    cert_path = tmp_path / "tls.crt"
    key_path = tmp_path / "tls.key"
    _generate_certificate(cert_path, key_path)
    rendered = tmp_path / "default.conf"
    rendered.write_text(
        NGINX_TEMPLATE.read_text(encoding="utf-8").replace(
            "${SERVER_NAME}", "recruiting.example.test"
        ),
        encoding="utf-8",
    )
    return rendered, cert_path, key_path


def _nginx_container_arguments(
    image: str, rendered: Path, cert_path: Path, key_path: Path
) -> list[str]:
    return [
        "--add-host",
        "api:127.0.0.1",
        "--mount",
        f"type=bind,source={rendered},target=/etc/nginx/conf.d/default.conf,readonly",
        "--mount",
        f"type=bind,source={SECURITY_HEADERS},target=/etc/nginx/snippets/security-headers.conf,readonly",
        "--mount",
        f"type=bind,source={cert_path},target=/etc/nginx/tls/tls.crt,readonly",
        "--mount",
        f"type=bind,source={key_path},target=/etc/nginx/tls/tls.key,readonly",
        image,
    ]


def test_merged_production_topology_has_one_https_host_entry(tmp_path: Path) -> None:
    cert_path = tmp_path / "tls.crt"
    key_path = tmp_path / "tls.key"
    cert_path.touch()
    key_path.touch()

    model = _merged_compose(cert_path, key_path)
    published = [
        (service_name, port)
        for service_name, service in model["services"].items()
        for port in service.get("ports", [])
    ]

    assert len(published) == 1
    service_name, port = published[0]
    assert service_name == "proxy"
    assert str(port["published"]) == "443"
    assert port["target"] == 8443
    assert port["protocol"] == "tcp"


def test_only_application_services_receive_provider_egress(tmp_path: Path) -> None:
    cert_path = tmp_path / "tls.crt"
    key_path = tmp_path / "tls.key"
    cert_path.touch()
    key_path.touch()

    model = _merged_compose(cert_path, key_path)

    assert model["networks"]["private"]["internal"] is True
    assert model["networks"]["provider-egress"].get("internal", False) is False
    assert set(model["services"]["api"]["networks"]) == {
        "private",
        "provider-egress",
    }
    assert set(model["services"]["worker"]["networks"]) == {
        "private",
        "provider-egress",
    }
    for service_name in ("postgres", "minio", "minio-provision", "clamav"):
        assert set(model["services"][service_name]["networks"]) == {"private"}


def test_default_organization_identity_is_wired_only_to_api(tmp_path: Path) -> None:
    cert_path = tmp_path / "tls.crt"
    key_path = tmp_path / "tls.key"
    cert_path.touch()
    key_path.touch()
    environment = _compose_environment(
        cert_path, key_path, server_name="recruiting.example.test"
    )
    environment.update(
        {
            "DEFAULT_ORGANIZATION_SLUG": "acme",
            "DEFAULT_ORGANIZATION_NAME": "Acme Recruiting",
        }
    )

    result = _run_compose_config(environment)

    assert result.returncode == 0, result.stderr
    model = json.loads(result.stdout)
    assert model["services"]["api"]["environment"]["DEFAULT_ORGANIZATION_SLUG"] == "acme"
    assert model["services"]["api"]["environment"]["DEFAULT_ORGANIZATION_NAME"] == "Acme Recruiting"
    assert "DEFAULT_ORGANIZATION_SLUG" not in model["services"]["worker"]["environment"]
    assert "DEFAULT_ORGANIZATION_NAME" not in model["services"]["worker"]["environment"]


def test_production_services_use_immutable_release_images_without_builds(
    tmp_path: Path,
) -> None:
    cert_path = tmp_path / "tls.crt"
    key_path = tmp_path / "tls.key"
    cert_path.touch()
    key_path.touch()

    model = _merged_compose(cert_path, key_path)
    expected_app = f"{APP_IMAGE}@{APP_IMAGE_DIGEST}"
    expected_frontend = f"{FRONTEND_IMAGE}@{FRONTEND_IMAGE_DIGEST}"

    for service_name in ("api", "worker", "queue-exporter"):
        service = model["services"][service_name]
        assert service["image"] == expected_app
        assert "build" not in service

    proxy = model["services"]["proxy"]
    assert proxy["image"] == expected_frontend
    assert "build" not in proxy
    assert all(
        volume["target"] != "/usr/share/nginx/html"
        for volume in proxy.get("volumes", [])
    )


def test_every_merged_production_service_uses_an_immutable_image(
    tmp_path: Path,
) -> None:
    cert_path = tmp_path / "tls.crt"
    key_path = tmp_path / "tls.key"
    cert_path.touch()
    key_path.touch()

    model = _merged_compose(cert_path, key_path)

    for service_name, service in model["services"].items():
        assert re.fullmatch(
            r"[^@\s]+@sha256:[0-9a-f]{64}", service.get("image", "")
        ), service_name
        assert "build" not in service, service_name


def test_default_production_model_does_not_run_legacy_on_host_backup(
    tmp_path: Path,
) -> None:
    cert_path = tmp_path / "tls.crt"
    key_path = tmp_path / "tls.key"
    cert_path.touch()
    key_path.touch()

    model = _merged_compose(cert_path, key_path)

    assert "backup" not in model["services"]


def test_release_compose_validator_accepts_the_merged_production_model(
    tmp_path: Path,
) -> None:
    cert_path = tmp_path / "tls.crt"
    key_path = tmp_path / "tls.key"
    cert_path.touch()
    key_path.touch()

    result = _run_release_compose_validator(_merged_compose(cert_path, key_path))

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    ("service_name", "bad_image"),
    [
        ("api", f"{APP_IMAGE}:latest"),
        ("worker", f"{APP_IMAGE}@sha256:{'a' * 63}"),
        ("queue-exporter", f"{APP_IMAGE}@sha256:{'A' * 64}"),
        ("proxy", f"{FRONTEND_IMAGE}@latest"),
        ("proxy", f"registry.example.test/ux09-frontend@sha256:{'2' * 64}"),
        ("postgres", "postgres:16.9-alpine"),
    ],
)
def test_release_compose_validator_rejects_mutable_or_malformed_images(
    tmp_path: Path, service_name: str, bad_image: str,
) -> None:
    cert_path = tmp_path / "tls.crt"
    key_path = tmp_path / "tls.key"
    cert_path.touch()
    key_path.touch()
    model = _merged_compose(cert_path, key_path)
    model["services"][service_name]["image"] = bad_image

    result = _run_release_compose_validator(model)

    assert result.returncode != 0
    assert service_name in result.stderr


@pytest.mark.parametrize(
    "violation", ["build", "static-mount", "legacy-backup", "zero-digest"]
)
def test_release_compose_validator_rejects_local_release_bypasses(
    tmp_path: Path, violation: str,
) -> None:
    cert_path = tmp_path / "tls.crt"
    key_path = tmp_path / "tls.key"
    cert_path.touch()
    key_path.touch()
    model = _merged_compose(cert_path, key_path)
    if violation == "build":
        model["services"]["api"]["build"] = {"context": ".."}
    elif violation == "static-mount":
        model["services"]["proxy"].setdefault("volumes", []).append(
            {"type": "bind", "source": "./nginx/static", "target": "/usr/share/nginx/html"}
        )
    elif violation == "legacy-backup":
        model["services"]["backup"] = {"image": "postgres:16.9-alpine"}
    else:
        zero_image = f"{APP_IMAGE}@sha256:{'0' * 64}"
        for service_name in ("api", "worker", "queue-exporter"):
            model["services"][service_name]["image"] = zero_image

    result = _run_release_compose_validator(model)

    assert result.returncode != 0
    assert violation in result.stderr


def test_frontend_release_image_contains_real_vite_application(
    tmp_path: Path, frontend_release_image: str
) -> None:
    container_id = ""
    try:
        created = subprocess.run(
            ["docker", "create", frontend_release_image],
            capture_output=True,
            text=True,
            check=False,
        )
        assert created.returncode == 0, created.stdout + created.stderr
        container_id = created.stdout.strip()
        html_root = tmp_path / "html"
        html_root.mkdir()
        copied = subprocess.run(
            [
                "docker",
                "cp",
                f"{container_id}:/usr/share/nginx/html/.",
                str(html_root),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert copied.returncode == 0, copied.stdout + copied.stderr
        index = (html_root / "index.html").read_text(encoding="utf-8")
        assert '<div id="root"></div>' in index
        assert list((html_root / "assets").glob("*.js"))
        assert "frontend assets mount here" not in index.lower()
    finally:
        if container_id:
            subprocess.run(
                ["docker", "rm", "-f", container_id],
                capture_output=True,
                text=True,
                check=False,
            )


@pytest.mark.parametrize("dockerfile", [SERVER_DOCKERFILE, FRONTEND_DOCKERFILE])
def test_release_dockerfiles_pin_every_base_image_by_digest(dockerfile: Path) -> None:
    external_images: list[str] = []
    stages: set[str] = set()
    for line in dockerfile.read_text(encoding="utf-8").splitlines():
        if not line.startswith("FROM "):
            continue
        parts = line.split()
        image = parts[1]
        if image not in stages:
            external_images.append(image)
        if len(parts) == 4 and parts[2].upper() == "AS":
            stages.add(parts[3])

    assert external_images
    assert all(re.fullmatch(r"[^@\s]+@sha256:[0-9a-f]{64}", image) for image in external_images)


def test_merged_production_topology_mounts_tls_files_without_api_privilege_leak(
    tmp_path: Path,
) -> None:
    cert_path = tmp_path / "tls.crt"
    key_path = tmp_path / "tls.key"
    cert_path.touch()
    key_path.touch()

    sentinel = f"phase6a-governance-sentinel-{uuid4().hex}"
    environment = _compose_environment(
        cert_path, key_path, server_name="recruiting.example.test"
    )
    for name in (
        "GOVERNANCE_DATABASE_URL",
        "GOVERNANCE_DB_USER",
        "GOVERNANCE_DB_PASSWORD",
        "GOVERNANCE_DELETE_ACCESS_KEY",
        "GOVERNANCE_DELETE_SECRET_KEY",
        "GOVERNANCE_EXPORT_BUCKET",
        "GOVERNANCE_EXPORT_PREFIX",
        "GOVERNANCE_LEDGER_ACCESS_KEY",
        "GOVERNANCE_LEDGER_BUCKET",
        "GOVERNANCE_LEDGER_PREFIX",
        "GOVERNANCE_LEDGER_SECRET_KEY",
        "GOVERNANCE_LEDGER_SIGNING_KEY",
        "GOVERNANCE_RESUME_BUCKET",
        "GOVERNANCE_RESUME_PREFIX",
        "GOVERNANCE_STORAGE_ENDPOINT",
    ):
        environment[name] = sentinel
    result = _run_compose_config(environment)
    assert result.returncode == 0, result.stderr
    model = json.loads(result.stdout)
    proxy = model["services"]["proxy"]
    mounts = {volume["target"]: volume for volume in proxy["volumes"]}

    assert mounts["/etc/nginx/tls/tls.crt"]["source"] == str(cert_path)
    assert mounts["/etc/nginx/tls/tls.key"]["source"] == str(key_path)
    assert mounts["/etc/nginx/tls/tls.crt"]["read_only"] is True
    assert mounts["/etc/nginx/tls/tls.key"]["read_only"] is True
    assert "TLS_PRIVATE_KEY" not in proxy.get("environment", {})
    assert proxy["environment"]["NGINX_ENVSUBST_FILTER"] == "^SERVER_NAME"
    api_environment = model["services"]["api"].get("environment", {})
    assert FORBIDDEN_API_ENVIRONMENT.isdisjoint(api_environment)
    assert all(sentinel not in str(value) for value in api_environment.values())
    assert model["services"]["api"]["environment"]["APP_ENVIRONMENT"] == "production"
    assert model["services"]["worker"]["environment"]["APP_ENVIRONMENT"] == "production"


def test_production_server_name_is_required(tmp_path: Path) -> None:
    cert_path = tmp_path / "tls.crt"
    key_path = tmp_path / "tls.key"
    cert_path.touch()
    key_path.touch()

    result = _run_compose_config(
        _compose_environment(cert_path, key_path, server_name=None)
    )

    assert result.returncode != 0
    assert "SERVER_NAME" in result.stderr


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("APP_IMAGE_DIGEST", "latest"),
        ("APP_IMAGE_DIGEST", f"sha256:{'a' * 63}"),
        ("FRONTEND_IMAGE_DIGEST", f"sha256:{'A' * 64}"),
    ],
)
def test_production_preflight_rejects_malformed_release_digest(
    tmp_path: Path, name: str, value: str,
) -> None:
    cert_path = tmp_path / "tls.crt"
    key_path = tmp_path / "tls.key"
    cert_path.touch()
    key_path.touch()
    environment = _preflight_environment(cert_path, key_path)
    environment[name] = value

    result = _run_preflight(environment)

    assert result.returncode != 0
    assert "sha256" in result.stderr


def test_production_nginx_contract_is_https_only_and_keeps_metrics_private() -> None:
    template = NGINX_TEMPLATE.read_text(encoding="utf-8")
    headers = SECURITY_HEADERS.read_text(encoding="utf-8")

    assert re.findall(r"\$\{([A-Z0-9_]+)\}", template) == ["SERVER_NAME"]
    assert "listen 8443 ssl" in template
    assert "ssl_protocols TLSv1.2 TLSv1.3" in template
    assert "ssl_certificate /etc/nginx/tls/tls.crt" in template
    assert "ssl_certificate_key /etc/nginx/tls/tls.key" in template
    assert "client_max_body_size 11m" in template
    assert "proxy_pass http://api:8000" in template
    assert "proxy_set_header X-Trace-ID $http_x_trace_id" in template
    assert "try_files $uri $uri/ /index.html" in template
    assert "location = /metrics" in template
    assert "return 404" in template
    assert "Cache-Control \"no-store\"" in template

    required_headers = (
        "Strict-Transport-Security",
        "X-Content-Type-Options \"nosniff\"",
        "X-Frame-Options \"DENY\"",
        "Referrer-Policy \"no-referrer\"",
        "Permissions-Policy",
        "default-src 'self'",
        "object-src 'none'",
        "base-uri 'self'",
        "frame-ancestors 'none'",
        "form-action 'self'",
    )
    for directive in required_headers:
        assert directive in headers


def test_nginx_configs_serve_pdf_worker_modules_as_javascript() -> None:
    for path in (LOCAL_NGINX_CONFIG, NGINX_TEMPLATE):
        config = path.read_text(encoding="utf-8")
        assert r"location ~* \.mjs$" in config
        assert "application/javascript mjs;" in config


def test_nginx_security_headers_allow_authorized_pdf_blob_reads() -> None:
    local = LOCAL_NGINX_CONFIG.read_text(encoding="utf-8")
    production = SECURITY_HEADERS.read_text(encoding="utf-8")
    assert "connect-src 'self' blob:" in local
    assert "connect-src 'self' blob:" in production


def test_nginx_security_headers_allow_pdf_wasm_without_javascript_eval() -> None:
    for path in (LOCAL_NGINX_CONFIG, SECURITY_HEADERS):
        config = path.read_text(encoding="utf-8")
        assert "script-src 'self' 'wasm-unsafe-eval'" in config
        assert "script-src 'self' 'unsafe-eval'" not in config


def test_rendered_production_nginx_passes_nginx_t_with_disposable_certificate(
    tmp_path: Path, frontend_release_image: str,
) -> None:
    rendered, cert_path, key_path = _render_nginx(tmp_path)

    result = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            *_nginx_container_arguments(
                frontend_release_image, rendered, cert_path, key_path
            ),
            "nginx",
            "-t",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_metrics_paths_return_404_from_running_production_nginx(
    tmp_path: Path, frontend_release_image: str
) -> None:
    rendered, cert_path, key_path = _render_nginx(tmp_path)
    started = subprocess.run(
        [
            "docker",
            "run",
            "-d",
            "-p",
            "127.0.0.1::8443",
            *_nginx_container_arguments(
                frontend_release_image, rendered, cert_path, key_path
            ),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    container_id = started.stdout.strip()
    try:
        port_result = subprocess.run(
            ["docker", "port", container_id, "8443/tcp"],
            capture_output=True,
            text=True,
            check=True,
        )
        host_port = int(port_result.stdout.strip().rsplit(":", 1)[1])
        context = ssl._create_unverified_context()
        root_payload = ""
        root_status = None
        for _ in range(30):
            try:
                with urlopen(
                    f"https://127.0.0.1:{host_port}/",
                    context=context,
                    timeout=1,
                ) as response:
                    root_status = response.status
                    root_payload = response.read().decode("utf-8")
                break
            except URLError:
                time.sleep(0.1)
        assert root_status == 200
        assert '<div id="root"></div>' in root_payload
        asset_match = re.search(r'<script[^>]+src="([^"]+\.js)"', root_payload)
        assert asset_match is not None
        with urlopen(
            f"https://127.0.0.1:{host_port}{asset_match.group(1)}",
            context=context,
            timeout=2,
        ) as asset_response:
            assert asset_response.status == 200
            assert asset_response.read(32)

        for path in ("/metrics", "/metrics/child"):
            status = None
            for _ in range(30):
                try:
                    with urlopen(
                        f"https://127.0.0.1:{host_port}{path}",
                        context=context,
                        timeout=1,
                    ) as response:
                        status = response.status
                except HTTPError as error:
                    status = error.code
                except URLError:
                    time.sleep(0.1)
                    continue
                break
            assert status == 404, path
    finally:
        subprocess.run(
            ["docker", "rm", "-f", container_id],
            capture_output=True,
            text=True,
            check=False,
        )


def _preflight_environment(cert_path: Path, key_path: Path) -> dict[str, str]:
    environment = _compose_environment(
        cert_path, key_path, server_name="recruiting.example.test"
    )
    environment["COMPOSE_ENV_FILE"] = "deploy/.env.example"
    return environment


def _shell_path(path: Path | str) -> str:
    value = Path(path).resolve().as_posix()
    if len(value) >= 3 and value[1:3] == ":/":
        return f"/{value[0].lower()}{value[2:]}"
    return value


def _run_preflight(
    environment: dict[str, str], *, shim_directory: Path | None = None
) -> subprocess.CompletedProcess[str]:
    shell = shutil.which("sh") or "sh"
    shim = _shell_path(shim_directory) if shim_directory is not None else ""
    return subprocess.run(
        [
            shell,
            "-c",
            'if [ -n "$1" ]; then PATH="$1:$PATH"; export PATH; fi; '
            "exec sh deploy/production-preflight.sh",
            "phase6a-preflight",
            shim,
        ],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )


def _write_docker_version_shim(tmp_path: Path) -> Path:
    shim = tmp_path / "docker"
    shim.write_text(
        """#!/bin/sh
if [ "$1" = compose ] && [ "$2" = version ] && [ "$3" = --short ]; then
    printf '%s\\n' "$FAKE_COMPOSE_VERSION"
    exit 0
fi
if [ -n "${CONFIG_CALL_MARKER:-}" ]; then
    printf '%s\\n' "$*" > "$CONFIG_CALL_MARKER"
fi
if [ "${ALLOW_REAL_DOCKER:-0}" = 1 ]; then
    exec "$REAL_DOCKER" "$@"
fi
printf '%s\\n' unexpected-compose-config-call > "$UNEXPECTED_CALL_MARKER"
exit 91
""",
        encoding="utf-8",
    )
    shim.chmod(0o755)
    return shim


def test_preflight_rejects_compose_older_than_minimum_before_config(
    tmp_path: Path,
) -> None:
    cert_path = tmp_path / "tls.crt"
    key_path = tmp_path / "tls.key"
    cert_path.touch()
    key_path.touch()
    _write_docker_version_shim(tmp_path)
    marker = tmp_path / "unexpected-call"
    environment = _preflight_environment(cert_path, key_path)
    environment.update(
        {
            "FAKE_COMPOSE_VERSION": "2.24.3",
            "UNEXPECTED_CALL_MARKER": _shell_path(marker),
        }
    )

    result = _run_preflight(environment, shim_directory=tmp_path)

    assert result.returncode != 0
    assert "2.24.4" in result.stderr
    assert not marker.exists()


def test_preflight_accepts_minimum_version_and_validates_merged_model(
    tmp_path: Path,
) -> None:
    cert_path = tmp_path / "tls.crt"
    key_path = tmp_path / "tls.key"
    cert_path.touch()
    key_path.touch()
    _write_docker_version_shim(tmp_path)
    real_docker = shutil.which("docker")
    assert real_docker is not None
    config_call = tmp_path / "config-call"
    environment = _preflight_environment(cert_path, key_path)
    environment.update(
        {
            "ALLOW_REAL_DOCKER": "1",
            "CONFIG_CALL_MARKER": _shell_path(config_call),
            "FAKE_COMPOSE_VERSION": "2.24.4",
            "REAL_DOCKER": _shell_path(real_docker),
        }
    )

    result = _run_preflight(environment, shim_directory=tmp_path)

    assert result.returncode == 0, result.stderr
    invoked = config_call.read_text(encoding="utf-8")
    assert "deploy/compose.yaml" in invoked.replace("\\", "/")
    assert "deploy/compose.production.yaml" in invoked.replace("\\", "/")


def test_preflight_accepts_installed_compose_and_validates_merged_model(
    tmp_path: Path,
) -> None:
    cert_path = tmp_path / "tls.crt"
    key_path = tmp_path / "tls.key"
    cert_path.touch()
    key_path.touch()

    result = _run_preflight(_preflight_environment(cert_path, key_path))

    assert result.returncode == 0, result.stderr


def test_remote_release_protection_is_part_of_repository_gate() -> None:
    script = (ROOT / "deploy" / "deploy-remote.ps1").read_text(encoding="utf-8")

    gate_index = script.index("Invoke-SharedNginxReleaseGate $repositoryRoot")
    validate_only_index = script.index("if ($ValidateOnly)")
    validate_only_return = script.index("return", validate_only_index)
    side_effect_indices = [
        script.index("Invoke-Native docker build"),
        script.index("New-Item -ItemType Directory -Force -Path $localStaging"),
        script.index("Invoke-Native tar -czf $sourceArchive"),
        script.index("Invoke-Native ssh"),
        script.index("Copy-RemoteArtifact $sourceArchive"),
    ]

    assert "test_shared_nginx_release_validator.py" in script
    assert "test_remote_deploy_scripts.py" in script
    assert "shared-nginx-smoke.sh" in script
    assert gate_index < validate_only_index < validate_only_return < min(side_effect_indices)
