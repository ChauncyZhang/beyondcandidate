from __future__ import annotations

import asyncio
import uuid
from datetime import timezone
from types import SimpleNamespace

from sqlalchemy import func, select

from server.app.governance.audit import append_audit
from server.app.governance.deletion_models import DeletionArtifact, DeletionRequest, LegalHold
from server.app.governance.deletion_service import (
    DeletionDomainError,
    build_private_manifest,
    canonical_manifest_hash,
    execute_database_redaction,
    lock_deletion_request_context,
)
from server.app.governance.storage import (
    GovernanceStorageError,
    LedgerArtifact,
    LedgerEntry,
    LedgerEntryV2,
)
from server.app.queue.models import BackgroundJob
from server.app.queue.payloads import IntegerField, OpaqueIdField, PayloadSchema, UnsafePayload
from server.app.queue.repository import QueueRepository
from server.app.queue.service import PermanentJobError, RetryableJobError
from server.app.recruiting.models import Application, Candidate, Resume
from server.app.recruiting.service import RecruitingService
from server.app.reports.models import ExportCandidateMembership, ExportRecord
from server.app.screening.models import ScreeningItem, ScreeningRun
from server.app.screening.progress import aggregate_run


DELETE_CANDIDATE_PAYLOAD = PayloadSchema(
    {
        "organization_id": OpaqueIdField(),
        "deletion_request_id": OpaqueIdField(),
        "request_version": IntegerField(1, 2_147_483_647),
    }
)


class DeletionJobHandler:
    def __init__(
        self,
        sessions,
        governance_engine,
        object_deleter,
        ledger,
        *,
        resume_bucket: str,
        export_bucket: str,
    ) -> None:
        self._sessions = sessions
        self._governance_engine = governance_engine
        self._object_deleter = object_deleter
        self._ledger = ledger
        self._resume_bucket = resume_bucket
        self._export_bucket = export_bucket

    async def __call__(self, job: object) -> None:
        try:
            payload = DELETE_CANDIDATE_PAYLOAD.validate(job.payload)
            organization_id = uuid.UUID(payload["organization_id"])
            request_id = uuid.UUID(payload["deletion_request_id"])
            request_version = payload["request_version"]
            if uuid.UUID(str(job.organization_id)) != organization_id:
                raise UnsafePayload("job tenant does not match payload")
        except (AttributeError, TypeError, ValueError, UnsafePayload):
            raise PermanentJobError("deletion_payload_invalid") from None

        await asyncio.to_thread(
            self._execute,
            organization_id,
            request_id,
            request_version,
            getattr(job, "trace_id", None),
        )

    def _execute(self, organization_id, request_id, request_version, trace_id) -> None:
        if self._sessions is None:
            raise RuntimeError("deletion handler dependencies are unavailable")
        self._prevalidate_v2_evidence(
            organization_id, request_id, request_version
        )
        completed = self._claim(
            organization_id, request_id, request_version, trace_id
        )
        if completed:
            self._verify_completed(organization_id, request_id, request_version)
            return
        self._settle_exports(organization_id, request_id)
        self._delete_artifacts(organization_id, request_id)
        result = self._redact(organization_id, request_id)
        entry = self._persist_redaction_and_build_entry(
            organization_id, request_id, request_version, result
        )
        try:
            receipt = self._ledger.write(entry)
            verified = self._ledger.read(receipt.object_key)
        except GovernanceStorageError as error:
            if error.code in {
                "ledger_invalid",
                "ledger_signature_invalid",
                "ledger_existing_mismatch",
            }:
                raise PermanentJobError("deletion_ledger_invalid") from error
            raise RetryableJobError("deletion_ledger_unavailable") from error
        if verified != entry:
            raise PermanentJobError("deletion_ledger_invalid")
        self._complete(
            organization_id,
            request_id,
            request_version,
            entry,
            receipt,
            trace_id,
        )

    def _prevalidate_v2_evidence(
        self, organization_id, request_id, request_version
    ) -> None:
        with self._sessions() as db:
            request = db.scalar(
                select(DeletionRequest).where(
                    DeletionRequest.organization_id == organization_id,
                    DeletionRequest.id == request_id,
                    DeletionRequest.version == request_version,
                )
            )
            if request is None or request.status not in {"approved", "executing"}:
                return
            try:
                if request.status == "executing":
                    artifact_facts = tuple(
                        LedgerArtifact(
                            kind=kind,
                            bucket=(
                                self._resume_bucket
                                if kind == "resume_object"
                                else self._export_bucket
                            ),
                            storage_key=storage_key,
                        )
                        for kind, storage_key in db.execute(
                            select(
                                DeletionArtifact.kind,
                                DeletionArtifact.storage_key,
                            )
                            .where(
                                DeletionArtifact.organization_id == organization_id,
                                DeletionArtifact.request_id == request_id,
                            )
                            .order_by(
                                DeletionArtifact.kind,
                                DeletionArtifact.storage_key,
                            )
                        )
                    )
                else:
                    artifact_facts = tuple(
                        LedgerArtifact(
                            kind=kind,
                            bucket=(
                                self._resume_bucket
                                if kind == "resume_object"
                                else self._export_bucket
                            ),
                            storage_key=item["storage_key"],
                        )
                        for kind, manifest_key in (
                            ("resume_object", "resume_objects"),
                            ("report_export_object", "temporary_exports"),
                        )
                        for item in request.impact_manifest["objects"][manifest_key]
                    )
                artifacts = tuple(sorted(artifact_facts))
                requested_at = request.requested_at
                if requested_at.tzinfo is None:
                    requested_at = requested_at.replace(tzinfo=timezone.utc)
                entry = LedgerEntryV2(
                    organization_id=organization_id,
                    deletion_request_id=request.id,
                    candidate_id=request.candidate_id,
                    completed_request_version=request.version,
                    completed_at=requested_at,
                    requested_at=requested_at,
                    reason_code=request.reason_code,
                    impact_manifest=request.impact_manifest,
                    manifest_hash=request.manifest_hash,
                    recovery_generation=request.recovery_generation,
                    artifacts=artifacts,
                    database_redaction_checksum="0" * 64,
                )
                validate = getattr(self._ledger, "validate_entry", None)
                if validate is not None:
                    validate(entry)
            except (GovernanceStorageError, KeyError, TypeError, ValueError):
                raise PermanentJobError("deletion_ledger_invalid") from None

    def _claim(self, organization_id, request_id, request_version, trace_id) -> bool:
        with self._sessions.begin() as db:
            context = lock_deletion_request_context(db, organization_id, request_id)
            if context is None:
                raise PermanentJobError("deletion_job_stale")
            candidate, request = context
            if request.version != request_version:
                raise PermanentJobError("deletion_job_stale")
            if request.status == "completed":
                return True
            resuming = request.status == "executing"
            if not resuming and request.status != "approved":
                raise PermanentJobError("deletion_job_stale")
            if resuming and candidate.deleted_at is not None:
                return False
            self._settle_screening_work(db, organization_id, candidate.id)
            hold = db.scalar(
                select(LegalHold.id).where(
                    LegalHold.organization_id == organization_id,
                    LegalHold.candidate_id == candidate.id,
                    LegalHold.released_at.is_(None),
                )
            )
            active_application = db.scalar(
                select(func.count())
                .select_from(Application)
                .where(
                    Application.organization_id == organization_id,
                    Application.candidate_id == candidate.id,
                    Application.stage.not_in(RecruitingService.TERMINAL),
                )
            )
            if hold is not None:
                raise RetryableJobError("deletion_legal_hold_active")
            if active_application:
                raise RetryableJobError("deletion_active_application")
            current_manifest, _ = build_private_manifest(
                db, candidate, now=request.requested_at
            )
            if (
                candidate.version != request.candidate_version
                or canonical_manifest_hash(current_manifest) != request.manifest_hash
            ):
                raise PermanentJobError("deletion_manifest_stale")
            if resuming:
                return False
            request.status = "executing"
            request.execution_started_at = request.execution_started_at or QueueRepository(
                db
            ).database_now()
            request.safe_error_code = None
            objects = current_manifest.get("objects", {})
            for kind, manifest_key in (
                ("resume_object", "resume_objects"),
                ("report_export_object", "temporary_exports"),
            ):
                for item in objects.get(manifest_key, []):
                    db.add(
                        DeletionArtifact(
                            organization_id=organization_id,
                            request_id=request.id,
                            kind=kind,
                            storage_key=item["storage_key"],
                        )
                    )
            append_audit(
                db,
                actor=SimpleNamespace(
                    organization_id=organization_id,
                    user_id=request.approved_by,
                ),
                category="governance",
                event_type="governance.deletion_started",
                outcome="success",
                trace_id=trace_id,
                resource_type="deletion_request",
                resource_id=request.id,
                metadata={"request_version": request.version},
            )
            db.flush()
            return False

    @staticmethod
    def _settle_screening_work(db, organization_id, candidate_id) -> None:
        resume_ids = list(
            db.scalars(
                select(Resume.id).where(
                    Resume.organization_id == organization_id,
                    Resume.candidate_id == candidate_id,
                )
            )
        )
        queue = QueueRepository(db)
        now = queue.database_now()
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        if resume_ids:
            profile_jobs = list(
                db.scalars(
                    select(BackgroundJob)
                    .where(
                        BackgroundJob.organization_id == organization_id,
                        BackgroundJob.type == "screening.profile_resume",
                        BackgroundJob.status.in_(("queued", "running")),
                        BackgroundJob.payload["resume_id"].as_string().in_({str(value) for value in resume_ids}),
                    )
                    .order_by(BackgroundJob.id)
                    .with_for_update()
                )
            )
            for profile_job in profile_jobs:
                lease_expires_at = profile_job.lease_expires_at
                if lease_expires_at is not None and lease_expires_at.tzinfo is None:
                    lease_expires_at = lease_expires_at.replace(tzinfo=timezone.utc)
                if profile_job.status == "running" and lease_expires_at is not None and lease_expires_at > now:
                    raise RetryableJobError("deletion_screening_inflight")
                if profile_job.status == "queued":
                    queue.cancel(organization_id, profile_job.id)
        items = list(
            db.scalars(
                select(ScreeningItem)
                .where(
                    ScreeningItem.organization_id == organization_id,
                    ScreeningItem.candidate_id == candidate_id,
                )
                .order_by(ScreeningItem.id)
                .with_for_update()
            )
        )
        if not items:
            return
        items_by_id = {str(item.id): item for item in items}
        runs = list(
            db.scalars(
                select(ScreeningRun)
                .where(
                    ScreeningRun.organization_id == organization_id,
                    ScreeningRun.id.in_({item.run_id for item in items}),
                )
                .order_by(ScreeningRun.id)
                .with_for_update()
            )
        )
        runs_by_id = {run.id: run for run in runs}
        jobs = list(
            db.scalars(
                select(BackgroundJob)
                .where(
                    BackgroundJob.organization_id == organization_id,
                    BackgroundJob.type.in_(
                        (
                            "screening.parse_item",
                            "screening.score_item",
                            "screening.llm_score_item",
                        )
                    ),
                    BackgroundJob.status.in_(("queued", "running")),
                    BackgroundJob.payload["screening_item_id"]
                    .as_string()
                    .in_(items_by_id),
                )
                .order_by(BackgroundJob.id)
                .with_for_update()
            )
        )
        for job in jobs:
            item = items_by_id.get(str(job.payload.get("screening_item_id")))
            lease_expires_at = job.lease_expires_at
            if lease_expires_at is not None and lease_expires_at.tzinfo is None:
                lease_expires_at = lease_expires_at.replace(tzinfo=timezone.utc)
            if (
                job.status == "running"
                and lease_expires_at is not None
                and lease_expires_at > now
                and item is not None
                and (
                    (job.type == "screening.parse_item" and item.status in {"queued", "parsing"})
                    or (job.type == "screening.score_item" and item.status in {"parsed", "scoring"})
                    or (
                        job.type == "screening.llm_score_item"
                        and item.status == "scored"
                        and item.llm_status in {"queued", "running"}
                    )
                )
            ):
                raise RetryableJobError("deletion_screening_inflight")
        affected_run_ids = set()
        for job in jobs:
            if job.status == "queued":
                if not queue.cancel(organization_id, job.id):
                    continue
                item = items_by_id.get(str(job.payload.get("screening_item_id")))
                if item is None:
                    continue
                affected_run_ids.add(item.run_id)
                if job.type in {"screening.parse_item", "screening.score_item"}:
                    if item.status not in {"scored", "failed", "cancelled"}:
                        item.status = "cancelled"
                        item.safe_error_code = "candidate_unavailable"
                        item.finished_at = item.finished_at or now
                elif (
                    job.type == "screening.llm_score_item"
                    and item.status == "scored"
                    and item.llm_status == "queued"
                ):
                    item.llm_status = "skipped"
                    item.llm_safe_error_code = "candidate_unavailable"
                    item.llm_finished_at = item.llm_finished_at or now
                    item.finished_at = item.finished_at or now
        db.flush()
        for run_id in sorted(affected_run_ids):
            run = runs_by_id.get(run_id)
            if run is not None:
                aggregate_run(db, run)

    def _settle_exports(self, organization_id, request_id) -> None:
        with self._sessions.begin() as db:
            context = lock_deletion_request_context(db, organization_id, request_id)
            if context is None:
                raise PermanentJobError("deletion_job_stale")
            candidate, request = context
            if request.status != "executing":
                raise PermanentJobError("deletion_job_stale")
            if candidate.deleted_at is None:
                self._settle_screening_work(db, organization_id, candidate.id)
                if db.scalar(
                    select(LegalHold.id).where(
                        LegalHold.organization_id == organization_id,
                        LegalHold.candidate_id == candidate.id,
                        LegalHold.released_at.is_(None),
                    )
                ) is not None:
                    raise RetryableJobError("deletion_legal_hold_active")
                if db.scalar(
                    select(func.count())
                    .select_from(Application)
                    .where(
                        Application.organization_id == organization_id,
                        Application.candidate_id == candidate.id,
                        Application.stage.not_in(RecruitingService.TERMINAL),
                    )
                ):
                    raise RetryableJobError("deletion_active_application")
            exports = list(
                db.scalars(
                    select(ExportRecord)
                    .join(
                        ExportCandidateMembership,
                        (ExportCandidateMembership.organization_id == ExportRecord.organization_id)
                        & (ExportCandidateMembership.export_id == ExportRecord.id),
                    )
                    .where(
                        ExportRecord.organization_id == organization_id,
                        ExportCandidateMembership.candidate_id == request.candidate_id,
                        ExportRecord.status.in_(("queued", "running")),
                    )
                    .order_by(ExportRecord.id)
                    .with_for_update()
                )
            )
            now = QueueRepository(db).database_now()
            for export in exports:
                job = db.scalar(
                    select(BackgroundJob)
                    .where(
                        BackgroundJob.organization_id == organization_id,
                        BackgroundJob.id == export.background_job_id,
                    )
                    .with_for_update()
                )
                if export.status == "running" or (job is not None and job.status == "running"):
                    raise RetryableJobError("deletion_export_inflight")
                if job is not None and job.status == "queued":
                    QueueRepository(db).cancel(organization_id, job.id)
                export.status = "failed"
                export.safe_error_code = "deletion_in_progress"
                export.completed_at = export.completed_at or now
                export.updated_at = now

    def _delete_artifacts(self, organization_id, request_id) -> None:
        while True:
            with self._sessions() as db:
                artifact = db.scalar(
                    select(DeletionArtifact)
                    .where(
                        DeletionArtifact.organization_id == organization_id,
                        DeletionArtifact.request_id == request_id,
                        DeletionArtifact.status == "pending",
                    )
                    .order_by(DeletionArtifact.kind, DeletionArtifact.storage_key)
                    .limit(1)
                )
                if artifact is None:
                    return
                artifact_id = artifact.id
                kind = artifact.kind
                key = artifact.storage_key
            bucket = self._resume_bucket if kind == "resume_object" else self._export_bucket
            try:
                self._object_deleter.delete(bucket, key)
            except GovernanceStorageError as error:
                with self._sessions.begin() as db:
                    checkpoint = db.get(DeletionArtifact, artifact_id)
                    if checkpoint is not None and checkpoint.status == "pending":
                        checkpoint.attempts += 1
                        checkpoint.safe_error_code = "object_delete_failed"
                raise RetryableJobError("deletion_object_unavailable") from error
            with self._sessions.begin() as db:
                checkpoint = db.get(DeletionArtifact, artifact_id)
                if checkpoint is not None and checkpoint.status == "pending":
                    now = QueueRepository(db).database_now()
                    checkpoint.status = "deleted"
                    checkpoint.attempts += 1
                    checkpoint.safe_error_code = None
                    checkpoint.deleted_at = now

    def _redact(self, organization_id, request_id):
        with self._sessions.begin() as db:
            context = lock_deletion_request_context(db, organization_id, request_id)
            if context is None:
                raise PermanentJobError("deletion_job_stale")
            candidate, request = context
            if request.status != "executing":
                raise PermanentJobError("deletion_job_stale")
            candidate_id = request.candidate_id
            pending = db.scalar(
                select(func.count())
                .select_from(DeletionArtifact)
                .where(
                    DeletionArtifact.organization_id == organization_id,
                    DeletionArtifact.request_id == request_id,
                    DeletionArtifact.status != "deleted",
                )
            )
            if pending:
                raise RetryableJobError("deletion_artifacts_pending")
            if candidate.deleted_at is None:
                self._settle_screening_work(db, organization_id, candidate.id)
                if db.scalar(
                    select(LegalHold.id).where(
                        LegalHold.organization_id == organization_id,
                        LegalHold.candidate_id == candidate.id,
                        LegalHold.released_at.is_(None),
                    )
                ) is not None:
                    raise RetryableJobError("deletion_legal_hold_active")
                if db.scalar(
                    select(func.count())
                    .select_from(Application)
                    .where(
                        Application.organization_id == organization_id,
                        Application.candidate_id == candidate.id,
                        Application.stage.not_in(RecruitingService.TERMINAL),
                    )
                ):
                    raise RetryableJobError("deletion_active_application")
        try:
            with self._governance_engine.begin() as connection:
                return execute_database_redaction(
                    connection,
                    organization_id=organization_id,
                    request_id=request_id,
                    candidate_id=candidate_id,
                )
        except DeletionDomainError as error:
            if error.code in {"redaction_failed"}:
                raise RetryableJobError("deletion_redaction_unavailable") from error
            raise PermanentJobError(error.code) from error

    def _persist_redaction_and_build_entry(
        self, organization_id, request_id, request_version, result
    ) -> LedgerEntryV2:
        with self._sessions.begin() as db:
            request = db.scalar(
                select(DeletionRequest)
                .where(
                    DeletionRequest.organization_id == organization_id,
                    DeletionRequest.id == request_id,
                    DeletionRequest.version == request_version,
                    DeletionRequest.status == "executing",
                )
                .with_for_update()
            )
            if request is None:
                raise PermanentJobError("deletion_job_stale")
            candidate = db.get(Candidate, request.candidate_id)
            if candidate is None or candidate.deleted_at is None:
                raise RetryableJobError("deletion_redaction_unavailable")
            if (
                request.database_redaction_checksum is not None
                and request.database_redaction_checksum != result.checksum
            ):
                raise PermanentJobError("deletion_redaction_mismatch")
            request.database_redaction_checksum = result.checksum
            request.ledger_completed_at = request.ledger_completed_at or candidate.deleted_at
            artifacts = tuple(
                LedgerArtifact(
                    kind=kind,
                    bucket=self._resume_bucket if kind == "resume_object" else self._export_bucket,
                    storage_key=storage_key,
                )
                for kind, storage_key in db.execute(
                    select(DeletionArtifact.kind, DeletionArtifact.storage_key)
                    .where(
                        DeletionArtifact.organization_id == organization_id,
                        DeletionArtifact.request_id == request_id,
                        DeletionArtifact.status == "deleted",
                    )
                    .order_by(DeletionArtifact.kind, DeletionArtifact.storage_key)
                )
            )
            return LedgerEntryV2(
                organization_id=organization_id,
                deletion_request_id=request.id,
                candidate_id=request.candidate_id,
                completed_request_version=request.version,
                completed_at=request.ledger_completed_at.replace(tzinfo=timezone.utc)
                if request.ledger_completed_at.tzinfo is None
                else request.ledger_completed_at,
                requested_at=request.requested_at.replace(tzinfo=timezone.utc)
                if request.requested_at.tzinfo is None
                else request.requested_at,
                reason_code=request.reason_code,
                impact_manifest=request.impact_manifest,
                manifest_hash=request.manifest_hash,
                recovery_generation=request.recovery_generation,
                artifacts=artifacts,
                database_redaction_checksum=request.database_redaction_checksum,
            )

    def _complete(
        self, organization_id, request_id, request_version, entry, receipt, trace_id
    ) -> None:
        with self._sessions.begin() as db:
            candidate = db.scalar(
                select(Candidate)
                .where(
                    Candidate.organization_id == organization_id,
                    Candidate.id == entry.candidate_id,
                )
                .with_for_update()
            )
            request = db.scalar(
                select(DeletionRequest)
                .where(
                    DeletionRequest.organization_id == organization_id,
                    DeletionRequest.id == request_id,
                    DeletionRequest.version == request_version,
                    DeletionRequest.status == "executing",
                )
                .with_for_update()
            )
            if candidate is None or request is None or candidate.deleted_at is None:
                raise PermanentJobError("deletion_job_stale")
            request.ledger_object_key = receipt.object_key
            request.ledger_sha256 = receipt.sha256
            request.status = "completed"
            request.completed_at = request.ledger_completed_at
            request.safe_error_code = None
            append_audit(
                db,
                actor=SimpleNamespace(
                    organization_id=organization_id,
                    user_id=request.approved_by,
                ),
                category="governance",
                event_type="governance.deletion_completed",
                outcome="success",
                trace_id=trace_id,
                resource_type="deletion_request",
                resource_id=request.id,
                metadata={"request_version": request.version},
            )

    def _verify_completed(self, organization_id, request_id, request_version) -> None:
        with self._sessions() as db:
            request = db.get(DeletionRequest, request_id)
            if (
                request is None
                or request.organization_id != organization_id
                or request.version != request_version
                or request.status != "completed"
                or request.database_redaction_checksum is None
                or request.ledger_completed_at is None
                or request.ledger_object_key is None
            ):
                raise PermanentJobError("deletion_job_stale")
            artifact_rows = tuple(
                db.execute(
                    select(DeletionArtifact.kind, DeletionArtifact.storage_key)
                    .where(
                        DeletionArtifact.organization_id == organization_id,
                        DeletionArtifact.request_id == request_id,
                        DeletionArtifact.status == "deleted",
                    )
                    .order_by(DeletionArtifact.kind, DeletionArtifact.storage_key)
                )
            )
            object_key = request.ledger_object_key
            completed_at = (
                request.ledger_completed_at.replace(tzinfo=timezone.utc)
                if request.ledger_completed_at.tzinfo is None
                else request.ledger_completed_at
            )
            path_parts = object_key.rsplit("/", 3)
            if len(path_parts) == 4 and path_parts[-3] == "v1":
                entry = LedgerEntry(
                    organization_id=organization_id,
                    deletion_request_id=request.id,
                    candidate_id=request.candidate_id,
                    completed_at=completed_at,
                    manifest_hash=request.manifest_hash,
                    object_keys=tuple(sorted(storage_key for _, storage_key in artifact_rows)),
                    database_redaction_checksum=request.database_redaction_checksum,
                )
            else:
                entry = LedgerEntryV2(
                    organization_id=organization_id,
                    deletion_request_id=request.id,
                    candidate_id=request.candidate_id,
                    completed_request_version=request.version,
                    completed_at=completed_at,
                    requested_at=request.requested_at.replace(tzinfo=timezone.utc)
                    if request.requested_at.tzinfo is None
                    else request.requested_at,
                    reason_code=request.reason_code,
                    impact_manifest=request.impact_manifest,
                    manifest_hash=request.manifest_hash,
                    recovery_generation=request.recovery_generation,
                    artifacts=tuple(
                        LedgerArtifact(
                            kind=kind,
                            bucket=self._resume_bucket if kind == "resume_object" else self._export_bucket,
                            storage_key=storage_key,
                        )
                        for kind, storage_key in artifact_rows
                    ),
                    database_redaction_checksum=request.database_redaction_checksum,
                )
        try:
            verified = self._ledger.read(object_key)
        except GovernanceStorageError as error:
            if error.code in {"ledger_invalid", "ledger_signature_invalid"}:
                raise PermanentJobError("deletion_ledger_invalid") from error
            raise RetryableJobError("deletion_ledger_unavailable") from error
        if verified != entry:
            raise PermanentJobError("deletion_ledger_invalid")
