#!/usr/bin/env python3
from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILES = (
    ROOT / "deploy" / "compose.yaml",
    ROOT / "deploy" / "compose.production.yaml",
    ROOT / "deploy" / "compose.observability.yaml",
)


def validate_runtime(model: dict, records: dict[str, dict[str, str]]) -> None:
    services = model.get("services")
    if not isinstance(services, dict):
        raise ValueError("release runtime: services object is required")
    expected_services = {
        name: service
        for name, service in services.items()
        if isinstance(service, dict) and service.get("restart") != "no"
    }
    for name, service in expected_services.items():
        record = records.get(name)
        if not isinstance(record, dict):
            raise ValueError(f"release runtime: running container is missing: {name}")
        expected_image = service.get("image")
        if record.get("config_image") != expected_image:
            raise ValueError(f"release runtime: configured image mismatch: {name}")
        if record.get("container_image_id") != record.get("resolved_image_id"):
            raise ValueError(f"release runtime: content image ID mismatch: {name}")


ENV_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def clean_compose_environment(env_file: Path) -> dict[str, str]:
    environment = os.environ.copy()
    for raw_line in env_file.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, separator, _ = line.partition("=")
        key = key.strip()
        if separator and ENV_KEY.fullmatch(key):
            environment.pop(key, None)
    return environment


def run(command: list[str], *, environment: dict[str, str] | None = None) -> str:
    result = subprocess.run(
        command,
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise ValueError(
            f"release runtime: command failed ({command[0]} {command[1]}): "
            f"{result.stderr.strip()}"
        )
    return result.stdout


def load_compose_validator():
    path = ROOT / "deploy" / "release-compose-validator.py"
    spec = importlib.util.spec_from_file_location("ux09_release_compose_validator", path)
    if spec is None or spec.loader is None:
        raise ValueError("release runtime: compose validator cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def compose_command(env_file: Path) -> list[str]:
    command = ["docker", "compose", "--env-file", str(env_file)]
    for compose_file in COMPOSE_FILES:
        command.extend(("-f", str(compose_file)))
    return command


def inspect_runtime(env_file: Path) -> tuple[dict, dict[str, dict[str, str]]]:
    compose = compose_command(env_file)
    environment = clean_compose_environment(env_file)
    model = json.loads(
        run([*compose, "config", "--format", "json"], environment=environment)
    )
    load_compose_validator().validate(model)
    records: dict[str, dict[str, str]] = {}
    for name, service in model["services"].items():
        if service.get("restart") == "no":
            continue
        container_ids = [
            item
            for item in run(
                [*compose, "ps", "-q", name], environment=environment
            ).splitlines()
            if item
        ]
        if len(container_ids) != 1:
            raise ValueError(
                f"release runtime: expected one running container for {name}; "
                f"found {len(container_ids)}"
            )
        container = json.loads(
            run(
                ["docker", "inspect", container_ids[0]], environment=environment
            )
        )[0]
        expected_image = service["image"]
        resolved = json.loads(
            run(
                ["docker", "image", "inspect", expected_image],
                environment=environment,
            )
        )[0]
        records[name] = {
            "config_image": container["Config"]["Image"],
            "container_image_id": container["Image"],
            "resolved_image_id": resolved["Id"],
        }
    return model, records


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify running UX09 containers match the immutable release model."
    )
    parser.add_argument("--env-file", type=Path, required=True)
    args = parser.parse_args()
    try:
        env_file = args.env_file.resolve(strict=True)
        model, records = inspect_runtime(env_file)
        validate_runtime(model, records)
    except (FileNotFoundError, json.JSONDecodeError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 1
    print(f"release runtime: verified {len(records)} immutable running containers")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
