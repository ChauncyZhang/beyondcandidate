#!/usr/bin/env python3
from __future__ import annotations

import json
import re
import sys


IMMUTABLE_IMAGE = re.compile(r"^[^@\s]+@sha256:[0-9a-f]{64}$")


def fail(message: str) -> None:
    raise ValueError(message)


def validate_service_image(services: dict, name: str) -> str:
    service = services.get(name)
    if not isinstance(service, dict):
        fail(f"release compose: required service is missing: {name}")
    image = service.get("image")
    if not isinstance(image, str) or not IMMUTABLE_IMAGE.fullmatch(image):
        fail(f"release compose: {name} image must use repository@sha256:<64 lowercase hex>")
    repository, digest = image.rsplit("@sha256:", 1)
    registry = repository.partition("/")[0]
    if registry == "registry.example.test" or registry.endswith(".invalid"):
        fail(f"release compose: {name} image uses an example/invalid registry")
    if digest == "0" * 64:
        fail(f"release compose: zero-digest violation on service: {name}")
    if "build" in service:
        fail(f"release compose: build violation on service: {name}")
    return image


def validate(model: object) -> None:
    if not isinstance(model, dict) or not isinstance(model.get("services"), dict):
        fail("release compose: services object is required")
    services = model["services"]
    for required in ("api", "worker", "proxy"):
        if required not in services:
            fail(f"release compose: required service is missing: {required}")
    if "backup" in services:
        fail("release compose: legacy-backup violation; use the off-host scheduler")

    images = {
        name: validate_service_image(services, name)
        for name in sorted(services)
    }

    app_image = images["api"]
    if images["worker"] != app_image:
        fail("release compose: worker image must equal api image")
    if "queue-exporter" in services:
        if images["queue-exporter"] != app_image:
            fail("release compose: queue-exporter image must equal api image")

    for volume in services["proxy"].get("volumes", []):
        if isinstance(volume, dict) and volume.get("target") == "/usr/share/nginx/html":
            fail("release compose: static-mount violation on proxy")

def main() -> int:
    try:
        validate(json.load(sys.stdin))
    except (json.JSONDecodeError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 1
    print("release compose: immutable application and frontend model is valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
