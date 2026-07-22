from __future__ import annotations

import os
from threading import Event

from prometheus_client import CollectorRegistry, start_http_server

from server.app.core.logging import configure_logging
from server.app.observability.collectors import PostgresQueueSnapshotProvider, QueueCollector


def build_registry(snapshot_provider) -> CollectorRegistry:  # type: ignore[no-untyped-def]
    registry = CollectorRegistry(auto_describe=True)
    registry.register(QueueCollector(snapshot_provider))
    return registry


def main() -> None:
    configure_logging()
    database_url = os.environ.get("OBSERVABILITY_DATABASE_URL", "")
    if not database_url:
        raise SystemExit("OBSERVABILITY_DATABASE_URL is required")
    port = int(os.environ.get("OBSERVABILITY_EXPORTER_PORT", "9108"))
    registry = build_registry(PostgresQueueSnapshotProvider(database_url))
    start_http_server(port, addr="0.0.0.0", registry=registry)
    Event().wait()


if __name__ == "__main__":
    main()
