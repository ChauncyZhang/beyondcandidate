from __future__ import annotations

import argparse
import io
import secrets
import sys
from datetime import datetime, timezone
from typing import Sequence
from urllib.parse import unquote, urlsplit
from uuid import UUID, uuid4

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from server.app.core.logging import configure_logging
from server.app.core.settings import GovernanceSettings, Settings
from server.app.core.storage import create_storage_client
from server.app.governance.recovery import RecoveryCoordinator, RecoveryError
from server.app.governance.storage import SignedLedgerAdapter


def _rfc3339(value: str) -> datetime:
    try:
        has_zone = value.endswith("Z") or "+" in value[10:] or "-" in value[10:]
        if "T" not in value or not has_zone:
            raise ValueError
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError
        return parsed.astimezone(timezone.utc)
    except ValueError:
        raise argparse.ArgumentTypeError(
            "restored-at must be an RFC3339 timestamp"
        ) from None


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Re-delete data resurrected by a restore"
    )
    parser.add_argument("--restore-id", required=True, type=UUID)
    parser.add_argument("--restored-at", required=True, type=_rfc3339)
    return parser.parse_args(argv)


def _sync_database_url(database_url: str) -> str:
    return database_url.replace("postgresql+asyncpg://", "postgresql+psycopg://", 1)


def _database_identity(engine, database_url: str):
    expected_user = unquote(urlsplit(database_url).username or "")
    try:
        with engine.connect() as connection:
            if connection.dialect.name != "postgresql":
                raise RecoveryError("recovery_database_identity_invalid")
            row = connection.execute(
                text(
                    "SELECT current_user, "
                    "pg_has_role(current_user, 'ux09_governance_executor', 'member'), "
                    "to_regprocedure('public.redact_candidate_data(uuid,uuid,uuid)') "
                    "IS NOT NULL, "
                    "COALESCE(has_function_privilege(current_user, "
                    "to_regprocedure('public.redact_candidate_data(uuid,uuid,uuid)'), "
                    "'EXECUTE'), false)"
                )
            ).one()
    except RecoveryError:
        raise
    except Exception as error:
        raise RecoveryError("recovery_database_unavailable") from error
    if row[0] != expected_user:
        raise RecoveryError("recovery_database_identity_invalid")
    return row


def _validate_application_database_identity(engine, database_url: str) -> None:
    row = _database_identity(engine, database_url)
    if row[1] is True:
        raise RecoveryError("recovery_database_identity_invalid")


def _validate_governance_database_identity(engine, database_url: str) -> None:
    row = _database_identity(engine, database_url)
    if len(row) != 4 or row[1:] != (True, True, True):
        raise RecoveryError("recovery_database_identity_invalid")
    canary = {
        "organization_id": uuid4(),
        "request_id": uuid4(),
        "candidate_id": uuid4(),
    }
    try:
        with engine.connect() as connection:
            connection.execute(
                text(
                    "SELECT * FROM public.redact_candidate_data("
                    ":organization_id, :request_id, :candidate_id)"
                ),
                canary,
            )
    except Exception as error:
        original = getattr(error, "orig", error)
        sqlstate = getattr(original, "sqlstate", None)
        if sqlstate == "22023" and "redaction_context_invalid" in str(original):
            return
        raise RecoveryError("recovery_database_identity_invalid") from error
    raise RecoveryError("recovery_database_identity_invalid")


def _validate_storage_permissions(
    canary_client,
    delete_client,
    ledger_client,
    *,
    resume_bucket: str,
    resume_prefix: str,
    export_bucket: str,
    export_prefix: str,
    ledger_bucket: str,
    ledger_prefix: str,
) -> None:
    try:
        next(
            iter(
                ledger_client.list_objects(
                    ledger_bucket, prefix=ledger_prefix, recursive=True
                )
            ),
            None,
        )
    except Exception as error:
        raise RecoveryError("recovery_storage_permission_invalid") from error
    locations = tuple(
        dict.fromkeys(
            (
                (resume_bucket, resume_prefix),
                (export_bucket, export_prefix),
            )
        )
    )
    for bucket, prefix in locations:
        key = f"{prefix}.governance-delete-canary/{uuid4().hex}"
        payload = secrets.token_bytes(32)
        failure = None
        try:
            canary_client.put_object(
                bucket,
                key,
                io.BytesIO(payload),
                len(payload),
                content_type="application/octet-stream",
            )
            delete_client.remove_object(bucket, key)
            try:
                canary_client.stat_object(bucket, key)
            except Exception as error:
                if getattr(error, "code", None) not in {
                    "NoSuchKey",
                    "NoSuchObject",
                    "NoSuchObjectName",
                }:
                    raise
            else:
                raise RuntimeError("delete canary remains present")
        except Exception as error:
            failure = error
        finally:
            try:
                canary_client.remove_object(bucket, key)
            except Exception as cleanup_error:
                if failure is None:
                    failure = cleanup_error
        if failure is not None:
            raise RecoveryError("recovery_storage_permission_invalid") from failure


def run(args: argparse.Namespace) -> int:
    ordinary = Settings.from_environment()
    governance = GovernanceSettings.from_environment(ordinary)
    application_database_url = _sync_database_url(ordinary.database_url)
    governance_database_url = _sync_database_url(
        governance.database_url.get_secret_value()
    )
    application_engine = create_engine(application_database_url, pool_pre_ping=True)
    governance_engine = create_engine(governance_database_url, pool_pre_ping=True)
    _validate_application_database_identity(
        application_engine, application_database_url
    )
    _validate_governance_database_identity(
        governance_engine, governance_database_url
    )
    canary_client = create_storage_client(
        governance.storage_endpoint,
        ordinary.object_storage_access_key,
        ordinary.object_storage_secret_key,
        secure=governance.storage_secure,
        connect_timeout_seconds=ordinary.object_storage_connect_timeout_seconds,
        read_timeout_seconds=ordinary.object_storage_read_timeout_seconds,
        total_timeout_seconds=ordinary.object_storage_total_timeout_seconds,
    )
    delete_client = create_storage_client(
        governance.storage_endpoint,
        governance.delete_access_key,
        governance.delete_secret_key.get_secret_value(),
        secure=governance.storage_secure,
        connect_timeout_seconds=ordinary.object_storage_connect_timeout_seconds,
        read_timeout_seconds=ordinary.object_storage_read_timeout_seconds,
        total_timeout_seconds=ordinary.object_storage_total_timeout_seconds,
    )
    ledger_client = create_storage_client(
        governance.storage_endpoint,
        governance.ledger_access_key,
        governance.ledger_secret_key.get_secret_value(),
        secure=governance.storage_secure,
        connect_timeout_seconds=ordinary.object_storage_connect_timeout_seconds,
        read_timeout_seconds=ordinary.object_storage_read_timeout_seconds,
        total_timeout_seconds=ordinary.object_storage_total_timeout_seconds,
    )
    _validate_storage_permissions(
        canary_client,
        delete_client,
        ledger_client,
        resume_bucket=governance.resume_bucket,
        resume_prefix=governance.resume_prefix,
        export_bucket=governance.export_bucket,
        export_prefix=governance.export_prefix,
        ledger_bucket=governance.ledger_bucket,
        ledger_prefix=governance.ledger_prefix,
    )
    ledger = SignedLedgerAdapter(
        ledger_client,
        governance.ledger_bucket,
        governance.ledger_prefix,
        governance.signing_key.get_secret_value().encode("utf-8"),
        allowed_buckets={governance.resume_bucket, governance.export_bucket},
        allowed_locations={
            "resume_object": (governance.resume_bucket, governance.resume_prefix),
            "report_export_object": (governance.export_bucket, governance.export_prefix),
        },
    )
    sessions = sessionmaker(application_engine, expire_on_commit=False)
    prepared = RecoveryCoordinator(
        sessions,
        ledger,
        maximum_ledgers=governance.recovery_max_ledgers,
    ).prepare(args.restore_id, args.restored_at)
    print(f"recovery_prepared={prepared}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    configure_logging()
    try:
        return run(parse_args(argv))
    except (RecoveryError, ValueError) as error:
        code = getattr(error, "code", "recovery_configuration_invalid")
        print(f"recovery_failed={code}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
