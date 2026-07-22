import asyncio

from minio import Minio
from urllib3 import PoolManager, Retry, Timeout


def create_storage_client(
    endpoint: str,
    access_key: str,
    secret_key: str,
    *,
    secure: bool,
    connect_timeout_seconds: float,
    read_timeout_seconds: float,
    total_timeout_seconds: float,
) -> Minio:
    http_client = PoolManager(
        timeout=Timeout(
            connect=connect_timeout_seconds,
            read=read_timeout_seconds,
            total=total_timeout_seconds,
        ),
        retries=Retry(
            total=False,
            connect=False,
            read=False,
            redirect=0,
            status=0,
            other=0,
        ),
        cert_reqs="CERT_REQUIRED",
    )
    return Minio(
        endpoint,
        access_key=access_key,
        secret_key=secret_key,
        secure=secure,
        http_client=http_client,
    )


class ObjectStorageProbe:
    def __init__(self, client: Minio, bucket: str) -> None:
        self._client = client
        self._bucket = bucket

    async def check(self) -> None:
        exists = await asyncio.to_thread(self._client.bucket_exists, self._bucket)
        if not exists:
            raise RuntimeError("required private bucket is unavailable")
