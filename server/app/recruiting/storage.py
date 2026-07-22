from tempfile import SpooledTemporaryFile
from typing import BinaryIO, Protocol


MAX_DOWNLOAD_BYTES = 10 * 1024 * 1024
MAX_PREVIEW_BYTES = 1024 * 1024


class StorageReadFailed(Exception):
    pass


class StorageObjectTooLarge(Exception):
    pass


class PrivateResumeStorage(Protocol):
    def open_download(self, storage_key: str, max_bytes: int = MAX_DOWNLOAD_BYTES) -> BinaryIO: ...


class MinioResumeStorage:
    def __init__(self, client, bucket: str) -> None:
        self.client = client
        self.bucket = bucket

    def open_download(self, storage_key: str, max_bytes: int = MAX_DOWNLOAD_BYTES) -> BinaryIO:
        response = None
        spool = SpooledTemporaryFile(max_size=min(max_bytes, 1024 * 1024), mode="w+b")
        try:
            response = self.client.get_object(self.bucket, storage_key)
            total = 0
            for chunk in response.stream(64 * 1024):
                total += len(chunk)
                if total > max_bytes:
                    raise StorageObjectTooLarge
                spool.write(chunk)
            spool.seek(0)
            return spool
        except StorageObjectTooLarge:
            spool.close()
            raise
        except Exception as error:
            spool.close()
            raise StorageReadFailed from error
        finally:
            if response is not None:
                response.close()
                response.release_conn()
