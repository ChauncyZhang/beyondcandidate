"""Render the real Compose model without starting containers or using secrets."""
import json
from pathlib import Path
import subprocess
import os

import pytest


ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("production", [False, True])
def test_default_branding_is_shared_read_only_by_proxy_api_and_worker(production):
    files = ["-f", "deploy/compose.yaml"]
    if production:
        files += ["-f", "deploy/compose.production.yaml"]
    result = subprocess.run(
        ["docker", "compose", "--env-file", "deploy/.env.example", *files, "config", "--format", "json"],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=30,
        env={**os.environ, "SERVER_NAME": "example.test", "TLS_CERTIFICATE_PATH": "/tmp/test.crt", "TLS_PRIVATE_KEY_PATH": "/tmp/test.key"},
    )
    assert result.returncode == 0, result.stderr
    services = json.loads(result.stdout)["services"]
    expected = (ROOT / "frontend/public/branding.json").resolve()
    for service, target in (("proxy", "/usr/share/nginx/html/branding.json"), ("api", "/run/product-branding.json"), ("worker", "/run/product-branding.json")):
        matches = [volume for volume in services[service].get("volumes", []) if volume["target"] == target]
        assert len(matches) == 1, f"{service} must consume the shared branding file"
        assert matches[0]["read_only"] is True
        assert Path(matches[0]["source"]).resolve() == expected
        if service != "proxy":
            assert services[service]["environment"]["PRODUCT_BRANDING_FILE"] == target
