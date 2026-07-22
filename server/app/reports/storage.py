from tempfile import SpooledTemporaryFile
from typing import BinaryIO, Protocol

from server.app.recruiting.storage import StorageObjectTooLarge, StorageReadFailed


MAX_EXPORT_BYTES = 50 * 1024 * 1024


class PrivateExportStorage(Protocol):
    def write(self, storage_key: str, content: bytes, content_type: str) -> None: ...
    def open_download(self, storage_key: str, max_bytes: int = MAX_EXPORT_BYTES) -> BinaryIO: ...


class MinioExportStorage:
    def __init__(self, client, bucket: str) -> None:
        self.client = client
        self.bucket = bucket

    def write(self, storage_key: str, content: bytes, content_type: str) -> None:
        stream = SpooledTemporaryFile(max_size=min(len(content), 1024 * 1024), mode="w+b")
        try:
            stream.write(content)
            stream.seek(0)
            self.client.put_object(self.bucket, storage_key, stream, len(content), content_type=content_type)
        except Exception as error:
            raise StorageReadFailed from error
        finally:
            stream.close()

    def open_download(self, storage_key: str, max_bytes: int = MAX_EXPORT_BYTES) -> BinaryIO:
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
