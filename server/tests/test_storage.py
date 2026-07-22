import asyncio
import socket
import threading
import time
from contextlib import closing

import pytest
from urllib3.exceptions import HTTPError

from server.app.core.storage import ObjectStorageProbe, create_storage_client


def test_storage_client_has_finite_timeouts_and_no_retries() -> None:
    client = create_storage_client(
        "minio:9000",
        "access-key",
        "secret-key",
        secure=False,
        connect_timeout_seconds=0.1,
        read_timeout_seconds=0.2,
        total_timeout_seconds=0.3,
    )

    pool = client._http  # type: ignore[attr-defined]
    timeout = pool.connection_pool_kw["timeout"]
    retries = pool.connection_pool_kw["retries"]

    assert timeout.connect_timeout == 0.1
    assert timeout.read_timeout == 0.2
    assert timeout.total == 0.3
    assert retries.total is False
    assert retries.connect is False
    assert retries.read is False
    assert retries.other == 0


def test_storage_probe_network_operation_has_finite_read_deadline() -> None:
    with closing(socket.socket()) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]

        def accept_without_responding() -> None:
            connection, _ = listener.accept()
            with connection:
                time.sleep(0.5)

        thread = threading.Thread(target=accept_without_responding, daemon=True)
        thread.start()
        client = create_storage_client(
            f"127.0.0.1:{port}",
            "access-key",
            "secret-key",
            secure=False,
            connect_timeout_seconds=0.05,
            read_timeout_seconds=0.05,
            total_timeout_seconds=0.1,
        )
        probe = ObjectStorageProbe(client, "resumes")

        started = time.monotonic()
        with pytest.raises(HTTPError):
            asyncio.run(probe.check())
        elapsed = time.monotonic() - started

    assert elapsed < 0.4
