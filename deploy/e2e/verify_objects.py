import json
import os
import sys
from uuid import UUID

from minio import Minio
from sqlalchemy import select

from server.app.identity.store import IdentityStore
from server.app.recruiting.models import FileObject
from server.app.screening.models import ScreeningItem


def required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def main() -> None:
    run_id = UUID(sys.argv[1])
    expected_count = int(sys.argv[2])
    store = IdentityStore(required("DATABASE_URL"))
    with store.sync_session() as db:
        rows = db.execute(
            select(FileObject.storage_key, FileObject.size_bytes)
            .join(
                ScreeningItem,
                (ScreeningItem.organization_id == FileObject.organization_id)
                & (ScreeningItem.file_object_id == FileObject.id),
            )
            .where(ScreeningItem.run_id == run_id)
            .order_by(FileObject.storage_key)
        ).all()

    if len(rows) != expected_count:
        raise RuntimeError(f"expected {expected_count} file objects, found {len(rows)}")
    if len({storage_key for storage_key, _ in rows}) != expected_count:
        raise RuntimeError("file object storage keys are not unique")

    client = Minio(
        required("OBJECT_STORAGE_ENDPOINT"),
        access_key=required("OBJECT_STORAGE_ACCESS_KEY"),
        secret_key=required("OBJECT_STORAGE_SECRET_KEY"),
        secure=False,
    )
    bucket = required("OBJECT_STORAGE_BUCKET")
    total_bytes = 0
    for storage_key, expected_size in rows:
        metadata = client.stat_object(bucket, storage_key)
        if metadata.size != expected_size:
            raise RuntimeError(
                f"MinIO size mismatch for {storage_key}: {metadata.size} != {expected_size}"
            )
        total_bytes += metadata.size

    print(json.dumps({"bucket": bucket, "objects": len(rows), "bytes": total_bytes}))


if __name__ == "__main__":
    main()
