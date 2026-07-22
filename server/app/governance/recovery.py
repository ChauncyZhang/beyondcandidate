from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import func, select, text, tuple_

from server.app.governance.deletion_models import (
    DeletionArtifact,
    DeletionRecoveryCheckpoint,
    DeletionRecoveryRun,
    DeletionRequest,
)
from server.app.governance.deletion_service import (
    DeletionDomainError,
    execute_database_redaction,
)
from server.app.governance.storage import GovernanceStorageError, LedgerEntryV2
from server.app.identity.models import Organization
from server.app.queue.payloads import OpaqueIdField, PayloadSchema, UnsafePayload
from server.app.queue.repository import QueueRepository
from server.app.queue.service import PermanentJobError, RetryableJobError
from server.app.recruiting.models import Candidate


class RecoveryError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


RECOVERY_CANDIDATE_QUERY_BATCH_SIZE = 500


def _validate_candidate_presence(
    db, expected: set[tuple[UUID, UUID]]
) -> None:
    ordered = sorted(expected, key=lambda pair: (str(pair[0]), str(pair[1])))
    present: set[tuple[UUID, UUID]] = set()
    for offset in range(0, len(ordered), RECOVERY_CANDIDATE_QUERY_BATCH_SIZE):
        chunk = ordered[offset : offset + RECOVERY_CANDIDATE_QUERY_BATCH_SIZE]
        rows = db.execute(
            select(Candidate.organization_id, Candidate.id).where(
                tuple_(Candidate.organization_id, Candidate.id).in_(chunk)
            )
        )
        present.update((row[0], row[1]) for row in rows)
    if present != expected:
        raise RecoveryError("recovery_database_state_invalid")


def aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class PreparedLedger:
    object_key: str
    sha256: str
    entry: LedgerEntryV2

    def __post_init__(self) -> None:
        if not self.object_key or len(self.object_key) > 512:
            raise ValueError("recovery ledger reference is invalid")
        if (
            len(self.sha256) != 64
            or self.sha256 != self.sha256.lower()
            or any(char not in "0123456789abcdef" for char in self.sha256)
        ):
            raise ValueError("recovery ledger checksum is invalid")


class RecoveryCoordinator:
    def __init__(self, sessions, ledger, *, maximum_ledgers: int) -> None:
        if not 1 <= maximum_ledgers <= 100_000:
            raise ValueError("recovery ledger limit is outside bounds")
        self._sessions = sessions
        self._ledger = ledger
        self._maximum_ledgers = maximum_ledgers

    def prepare(self, restore_id: UUID, restored_at: datetime) -> int:
        restored_at = aware(restored_at)
        if self._sessions is None:
            raise RecoveryError("recovery_database_unavailable")
        with self._sessions() as db:
            existing = list(
                db.scalars(
                    select(DeletionRecoveryRun).where(
                        DeletionRecoveryRun.restore_id == restore_id
                    )
                )
            )
            if existing:
                if any(aware(row.restored_at) != restored_at for row in existing):
                    raise RecoveryError("recovery_restore_conflict")
                return 0

        try:
            prepared = tuple(
                self._ledger.discover_recovery_ledgers(
                    restored_at,
                    maximum=self._maximum_ledgers,
                )
            )
        except RecoveryError:
            raise
        except Exception as error:
            code = getattr(error, "code", "recovery_ledger_invalid")
            raise RecoveryError(code) from error
        if len(prepared) > self._maximum_ledgers:
            raise RecoveryError("recovery_ledger_limit_exceeded")
        seen_requests: dict[UUID, PreparedLedger] = {}
        seen_objects: set[str] = set()
        for item in prepared:
            if not isinstance(item, PreparedLedger):
                raise RecoveryError("recovery_ledger_invalid")
            if aware(item.entry.completed_at) <= restored_at:
                raise RecoveryError("recovery_ledger_not_applicable")
            prior = seen_requests.get(item.entry.deletion_request_id)
            if prior is not None and prior != item:
                raise RecoveryError("recovery_ledger_conflict")
            if item.object_key in seen_objects or item.entry.deletion_request_id in seen_requests:
                raise RecoveryError("recovery_ledger_duplicate")
            seen_requests[item.entry.deletion_request_id] = item
            seen_objects.add(item.object_key)

        organizations = {item.entry.organization_id for item in prepared}
        candidates = {(item.entry.organization_id, item.entry.candidate_id) for item in prepared}
        with self._sessions() as db:
            present_organizations = set(
                db.scalars(select(Organization.id).where(Organization.id.in_(organizations)))
            ) if organizations else set()
            _validate_candidate_presence(db, candidates)
            marker_organization_id = db.scalar(
                select(Organization.id).order_by(Organization.id).limit(1)
            )
        if present_organizations != organizations:
            raise RecoveryError("recovery_database_state_invalid")
        if marker_organization_id is None:
            raise RecoveryError("recovery_database_state_invalid")

        grouped: dict[UUID, list[PreparedLedger]] = {}
        for item in prepared:
            grouped.setdefault(item.entry.organization_id, []).append(item)
        if not grouped:
            grouped[marker_organization_id] = []
        with self._sessions.begin() as db:
            if db.get_bind().dialect.name == "postgresql":
                db.execute(
                    text(
                        "SELECT pg_advisory_xact_lock("
                        "hashtextextended(:lock_key, 0))"
                    ),
                    {"lock_key": f"governance-recovery:{restore_id}"},
                )
            concurrent = list(
                db.scalars(
                    select(DeletionRecoveryRun).where(
                        DeletionRecoveryRun.restore_id == restore_id
                    )
                )
            )
            if concurrent:
                if any(aware(row.restored_at) != restored_at for row in concurrent):
                    raise RecoveryError("recovery_restore_conflict")
                return 0
            queue = QueueRepository(db)
            now = aware(queue.database_now())
            for organization_id, items in sorted(grouped.items(), key=lambda pair: str(pair[0])):
                run = DeletionRecoveryRun(
                    organization_id=organization_id,
                    restore_id=restore_id,
                    restored_at=restored_at,
                    status="queued" if items else "completed",
                    queued_at=now,
                    completed_at=None if items else now,
                )
                db.add(run)
                db.flush()
                for item in sorted(items, key=lambda value: value.sha256):
                    restored_request = db.get(
                        DeletionRequest, item.entry.deletion_request_id
                    )
                    target_generation = max(
                        item.entry.recovery_generation,
                        restored_request.recovery_generation if restored_request else 0,
                    ) + 1
                    checkpoint = DeletionRecoveryCheckpoint(
                        organization_id=organization_id,
                        run_id=run.id,
                        deletion_request_id=item.entry.deletion_request_id,
                        ledger_object_key=item.object_key,
                        ledger_sha256=item.sha256,
                        target_generation=target_generation,
                        status="pending",
                    )
                    db.add(checkpoint)
                    db.flush()
                    job = queue.enqueue(
                        organization_id,
                        "governance.redelete_after_restore",
                        {
                            "organization_id": str(organization_id),
                            "recovery_run_id": str(run.id),
                            "checkpoint_id": str(checkpoint.id),
                        },
                        dedupe_key=f"recovery:{restore_id}:{item.sha256[:16]}",
                        max_attempts=5,
                    )
                    checkpoint.queue_job_id = job.id
            return len(prepared)


RECOVERY_JOB_PAYLOAD = PayloadSchema(
    {
        "organization_id": OpaqueIdField(),
        "recovery_run_id": OpaqueIdField(),
        "checkpoint_id": OpaqueIdField(),
    }
)


class RecoveryJobHandler:
    def __init__(self, sessions, governance_engine, object_deleter, ledger) -> None:
        self._sessions = sessions
        self._governance_engine = governance_engine
        self._object_deleter = object_deleter
        self._ledger = ledger

    async def __call__(self, job: object) -> None:
        try:
            payload = RECOVERY_JOB_PAYLOAD.validate(job.payload)
            organization_id = UUID(payload["organization_id"])
            run_id = UUID(payload["recovery_run_id"])
            checkpoint_id = UUID(payload["checkpoint_id"])
            if UUID(str(job.organization_id)) != organization_id:
                raise ValueError
        except (AttributeError, TypeError, ValueError, UnsafePayload):
            raise PermanentJobError("recovery_payload_invalid") from None
        await asyncio.to_thread(
            self._execute, organization_id, run_id, checkpoint_id
        )

    def _execute(self, organization_id: UUID, run_id: UUID, checkpoint_id: UUID) -> None:
        if self._sessions is None:
            raise RuntimeError("recovery handler dependencies are unavailable")
        with self._sessions() as db:
            checkpoint = db.get(DeletionRecoveryCheckpoint, checkpoint_id)
            run = db.get(DeletionRecoveryRun, run_id)
            if (
                checkpoint is None
                or run is None
                or checkpoint.organization_id != organization_id
                or checkpoint.run_id != run_id
                or run.organization_id != organization_id
            ):
                raise PermanentJobError("recovery_job_stale")
            if checkpoint.status == "completed":
                return
            object_key = checkpoint.ledger_object_key
            expected_sha256 = checkpoint.ledger_sha256
            restored_at = aware(run.restored_at)
        try:
            entry = self._ledger.read_recovery(object_key, expected_sha256)
        except GovernanceStorageError as error:
            if error.code in {
                "recovery_ledger_changed",
                "recovery_ledger_invalid",
                "recovery_ledger_unsupported",
                "ledger_invalid",
                "ledger_signature_invalid",
            }:
                raise PermanentJobError("recovery_ledger_invalid") from error
            raise RetryableJobError("recovery_ledger_unavailable") from error
        if (
            entry.organization_id != organization_id
            or entry.deletion_request_id != checkpoint.deletion_request_id
            or aware(entry.completed_at) <= restored_at
        ):
            raise PermanentJobError("recovery_ledger_invalid")
        if self._prepare(organization_id, run_id, checkpoint_id, entry):
            return
        self._delete_objects(organization_id, entry)
        try:
            with self._governance_engine.begin() as connection:
                execute_database_redaction(
                    connection,
                    organization_id=organization_id,
                    request_id=entry.deletion_request_id,
                    candidate_id=entry.candidate_id,
                )
        except DeletionDomainError as error:
            if error.code == "redaction_failed":
                raise RetryableJobError("recovery_redaction_unavailable") from error
            raise PermanentJobError("recovery_redaction_invalid") from error
        self._complete(organization_id, run_id, checkpoint_id, entry)

    def _prepare(self, organization_id, run_id, checkpoint_id, entry) -> bool:
        with self._sessions.begin() as db:
            run = db.scalar(
                select(DeletionRecoveryRun)
                .where(
                    DeletionRecoveryRun.organization_id == organization_id,
                    DeletionRecoveryRun.id == run_id,
                )
                .with_for_update()
            )
            checkpoint = db.scalar(
                select(DeletionRecoveryCheckpoint)
                .where(
                    DeletionRecoveryCheckpoint.organization_id == organization_id,
                    DeletionRecoveryCheckpoint.id == checkpoint_id,
                    DeletionRecoveryCheckpoint.run_id == run_id,
                )
                .with_for_update()
            )
            if run is None or checkpoint is None:
                raise PermanentJobError("recovery_job_stale")
            if checkpoint.status == "completed":
                return True
            candidate = db.scalar(
                select(Candidate)
                .where(
                    Candidate.organization_id == organization_id,
                    Candidate.id == entry.candidate_id,
                )
                .with_for_update()
            )
            if candidate is None:
                raise PermanentJobError("recovery_database_state_invalid")
            request = db.scalar(
                select(DeletionRequest)
                .where(
                    DeletionRequest.organization_id == organization_id,
                    DeletionRequest.id == entry.deletion_request_id,
                )
                .with_for_update()
            )
            exact_artifacts = {
                (artifact.kind, artifact.storage_key) for artifact in entry.artifacts
            }
            existing_artifacts = set()
            if request is not None:
                existing_artifacts = set(
                    db.execute(
                        select(DeletionArtifact.kind, DeletionArtifact.storage_key).where(
                            DeletionArtifact.organization_id == organization_id,
                            DeletionArtifact.request_id == request.id,
                        )
                    )
                )
            resumable = (
                request is not None
                and request.status == "executing"
                and request.recovery_generation == checkpoint.target_generation
                and request.manifest_hash == entry.manifest_hash
                and request.impact_manifest == entry.impact_manifest
                and existing_artifacts == exact_artifacts
            )
            if not resumable:
                stale_requests = list(
                    db.scalars(
                        select(DeletionRequest)
                        .where(
                            DeletionRequest.organization_id == organization_id,
                            DeletionRequest.candidate_id == entry.candidate_id,
                            DeletionRequest.id != entry.deletion_request_id,
                            DeletionRequest.status != "completed",
                        )
                        .order_by(DeletionRequest.id)
                        .with_for_update()
                    )
                )
                for stale in stale_requests:
                    db.delete(stale)
                if stale_requests:
                    db.flush()
                if request is None:
                    request = DeletionRequest(
                        id=entry.deletion_request_id,
                        organization_id=organization_id,
                        candidate_id=entry.candidate_id,
                        reason_code=entry.reason_code,
                        impact_manifest=entry.impact_manifest,
                        manifest_hash=entry.manifest_hash,
                        policy_version=entry.impact_manifest["policy_version"],
                        candidate_version=entry.impact_manifest["candidate_version"],
                    )
                    db.add(request)
                else:
                    for artifact in list(
                        db.scalars(
                            select(DeletionArtifact).where(
                                DeletionArtifact.organization_id == organization_id,
                                DeletionArtifact.request_id == request.id,
                            )
                        )
                    ):
                        db.delete(artifact)
                request.status = "executing"
                request.version = entry.completed_request_version
                request.reason_code = entry.reason_code
                request.requested_by = None
                request.requested_at = entry.requested_at
                request.approved_by = None
                request.approved_at = None
                request.execution_started_at = QueueRepository(db).database_now()
                request.completed_at = None
                request.safe_error_code = None
                request.impact_manifest = entry.impact_manifest
                request.manifest_hash = entry.manifest_hash
                request.manifest_schema_version = 1
                request.policy_version = entry.impact_manifest["policy_version"]
                request.candidate_version = entry.impact_manifest["candidate_version"]
                request.recovery_generation = checkpoint.target_generation
                request.database_redaction_checksum = entry.database_redaction_checksum
                request.ledger_completed_at = entry.completed_at
                request.ledger_object_key = checkpoint.ledger_object_key
                request.ledger_sha256 = checkpoint.ledger_sha256
                db.flush()
                for artifact in entry.artifacts:
                    db.add(
                        DeletionArtifact(
                            organization_id=organization_id,
                            request_id=request.id,
                            kind=artifact.kind,
                            storage_key=artifact.storage_key,
                            status="pending",
                        )
                    )
            checkpoint.status = "running"
            checkpoint.attempts += 1
            checkpoint.started_at = checkpoint.started_at or QueueRepository(db).database_now()
            checkpoint.safe_error_code = None
            run.status = "running"
            run.started_at = run.started_at or checkpoint.started_at
            return False

    def _delete_objects(self, organization_id: UUID, entry: LedgerEntryV2) -> None:
        for descriptor in entry.artifacts:
            with self._sessions() as db:
                artifact = db.scalar(
                    select(DeletionArtifact).where(
                        DeletionArtifact.organization_id == organization_id,
                        DeletionArtifact.request_id == entry.deletion_request_id,
                        DeletionArtifact.kind == descriptor.kind,
                        DeletionArtifact.storage_key == descriptor.storage_key,
                    )
                )
                if artifact is None:
                    raise PermanentJobError("recovery_checkpoint_invalid")
                if artifact.status == "deleted":
                    continue
                artifact_id = artifact.id
            try:
                self._object_deleter.delete(descriptor.bucket, descriptor.storage_key)
            except GovernanceStorageError as error:
                raise RetryableJobError("recovery_object_unavailable") from error
            with self._sessions.begin() as db:
                artifact = db.get(DeletionArtifact, artifact_id)
                if artifact is not None and artifact.status == "pending":
                    now = QueueRepository(db).database_now()
                    artifact.status = "deleted"
                    artifact.attempts += 1
                    artifact.safe_error_code = None
                    artifact.deleted_at = now

    def _complete(self, organization_id, run_id, checkpoint_id, entry) -> None:
        with self._sessions.begin() as db:
            candidate = db.get(Candidate, entry.candidate_id)
            request = db.get(DeletionRequest, entry.deletion_request_id)
            checkpoint = db.get(DeletionRecoveryCheckpoint, checkpoint_id)
            run = db.get(DeletionRecoveryRun, run_id)
            if (
                candidate is None
                or candidate.deleted_at is None
                or request is None
                or checkpoint is None
                or run is None
            ):
                raise RetryableJobError("recovery_redaction_unavailable")
            request.status = "completed"
            request.completed_at = entry.completed_at
            request.safe_error_code = None
            checkpoint.status = "completed"
            checkpoint.completed_at = QueueRepository(db).database_now()
            checkpoint.safe_error_code = None
            completed = db.scalar(
                select(func.count())
                .select_from(DeletionRecoveryCheckpoint)
                .where(
                    DeletionRecoveryCheckpoint.run_id == run_id,
                    DeletionRecoveryCheckpoint.status == "completed",
                    DeletionRecoveryCheckpoint.id != checkpoint_id,
                )
            ) + 1
            total = db.scalar(
                select(func.count())
                .select_from(DeletionRecoveryCheckpoint)
                .where(DeletionRecoveryCheckpoint.run_id == run_id)
            )
            run.restored_candidate_count = completed
            run.requeued_request_count = completed
            if completed == total:
                run.status = "completed"
                run.completed_at = checkpoint.completed_at
