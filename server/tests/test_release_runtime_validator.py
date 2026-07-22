from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
VALIDATOR = ROOT / "deploy" / "release-runtime-validator.py"
APP_IMAGE = "registry.synthetic.test/ux09-server@sha256:" + "1" * 64
FRONTEND_IMAGE = "registry.synthetic.test/ux09-frontend@sha256:" + "2" * 64


def load_validator():
    assert VALIDATOR.is_file(), "release runtime validator is missing"
    spec = importlib.util.spec_from_file_location("ux09_release_runtime_validator", VALIDATOR)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def model() -> dict:
    return {
        "services": {
            "api": {"image": APP_IMAGE, "restart": "unless-stopped"},
            "worker": {"image": APP_IMAGE, "restart": "unless-stopped"},
            "proxy": {"image": FRONTEND_IMAGE, "restart": "unless-stopped"},
            "one-shot": {"image": APP_IMAGE, "restart": "no"},
        }
    }


def records() -> dict:
    return {
        "api": {
            "config_image": APP_IMAGE,
            "container_image_id": "sha256:app-local-id",
            "resolved_image_id": "sha256:app-local-id",
        },
        "worker": {
            "config_image": APP_IMAGE,
            "container_image_id": "sha256:app-local-id",
            "resolved_image_id": "sha256:app-local-id",
        },
        "proxy": {
            "config_image": FRONTEND_IMAGE,
            "container_image_id": "sha256:frontend-local-id",
            "resolved_image_id": "sha256:frontend-local-id",
        },
    }


def test_runtime_validator_accepts_exact_running_release_images() -> None:
    validator = load_validator()

    validator.validate_runtime(model(), records())


@pytest.mark.parametrize(
    ("service", "field", "value"),
    [
        ("api", "config_image", FRONTEND_IMAGE),
        ("worker", "container_image_id", "sha256:wrong-local-id"),
    ],
)
def test_runtime_validator_rejects_config_or_content_identity_mismatch(
    service: str, field: str, value: str,
) -> None:
    validator = load_validator()
    runtime = records()
    runtime[service][field] = value

    with pytest.raises(ValueError, match=service):
        validator.validate_runtime(model(), runtime)


def test_runtime_validator_requires_every_long_running_service() -> None:
    validator = load_validator()
    runtime = records()
    del runtime["proxy"]

    with pytest.raises(ValueError, match="proxy"):
        validator.validate_runtime(model(), runtime)


def test_runtime_validator_removes_env_file_keys_from_process_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    validator = load_validator()
    env_file = tmp_path / ".env"
    env_file.write_text(
        "APP_IMAGE=registry.synthetic.test/from-file\n"
        "APP_IMAGE_DIGEST=sha256:" + "1" * 64 + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("APP_IMAGE", "registry.synthetic.test/from-shell")
    monkeypatch.setenv("APP_IMAGE_DIGEST", "sha256:" + "9" * 64)
    monkeypatch.setenv("UNRELATED_OPERATOR_SETTING", "preserved")

    environment = validator.clean_compose_environment(env_file)

    assert "APP_IMAGE" not in environment
    assert "APP_IMAGE_DIGEST" not in environment
    assert environment["UNRELATED_OPERATOR_SETTING"] == "preserved"


def test_runbook_restores_complete_image_refs_in_clean_environment() -> None:
    runbook = (ROOT / "deploy" / "production-operations-runbook.md").read_text(
        encoding="utf-8"
    )
    rollback = runbook.split("For an application-only rollback", maxsplit=1)[1]

    assert "APP_IMAGE`, `APP_IMAGE_DIGEST`, `FRONTEND_IMAGE`, and" in rollback
    assert "`FRONTEND_IMAGE_DIGEST`" in rollback
    assert (
        "unset APP_IMAGE APP_IMAGE_DIGEST FRONTEND_IMAGE FRONTEND_IMAGE_DIGEST"
        in rollback
    )
    assert "config --images" in rollback
