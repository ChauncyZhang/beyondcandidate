from __future__ import annotations

import asyncio
import argparse
from datetime import date, datetime, time, timedelta, timezone
from uuid import UUID

from sqlalchemy import exists, select

from server.app.governance.deletion_models import DeletionRequest, LegalHold
from server.app.governance.deletion_service import (
    build_private_manifest,
    canonical_manifest_hash,
)
from server.app.governance.models import RetentionPolicy
from server.app.governance.retention import aware, candidate_due_dates, recalculate_due_dates
from server.app.identity.models import Organization
from server.app.queue.models import BackgroundJob
from server.app.queue.payloads import IdentifierField, OpaqueIdField, PayloadSchema, UnsafePayload
from server.app.queue.repository import QueueRepository
from server.app.queue.service import PermanentJobError
from server.app.recruiting.models import Application, Candidate
from server.app.recruiting.service import RecruitingService


RETENTION_SWEEP_PAYLOAD = PayloadSchema(
    {
        "organization_id": OpaqueIdField(),
        "scheduled_date": IdentifierField(),
    }
)


def retention_candidate_claim(organization_id: UUID, now: datetime, *, limit: int):
    active_application = exists(
        select(Application.id).where(
            Application.organization_id == organization_id,
            Application.candidate_id == Candidate.id,
            Application.stage.not_in(RecruitingService.TERMINAL),
        )
    )
    active_hold = exists(
        select(LegalHold.id).where(
            LegalHold.organization_id == organization_id,
            LegalHold.candidate_id == Candidate.id,
            LegalHold.released_at.is_(None),
        )
    )
    open_request = exists(
        select(DeletionRequest.id).where(
            DeletionRequest.organization_id == organization_id,
            DeletionRequest.candidate_id == Candidate.id,
            DeletionRequest.status != "completed",
        )
    )
    return (
        select(Candidate)
        .where(
            Candidate.organization_id == organization_id,
            Candidate.deleted_at.is_(None),
            Candidate.retention_due_at.is_not(None),
            Candidate.retention_due_at <= now,
            ~active_application,
            ~active_hold,
            ~open_request,
        )
        .order_by(Candidate.retention_due_at, Candidate.id)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )


class RetentionSweepJobHandler:
    def __init__(self, sessions, *, batch_size: int) -> None:
        if not 1 <= batch_size <= 1_000:
            raise ValueError("retention sweep batch size is outside bounds")
        self._sessions = sessions
        self._batch_size = batch_size

    async def __call__(self, job: object) -> None:
        try:
            payload = RETENTION_SWEEP_PAYLOAD.validate(job.payload)
            organization_id = UUID(payload["organization_id"])
            scheduled_date = date.fromisoformat(payload["scheduled_date"])
            if str(scheduled_date) != payload["scheduled_date"]:
                raise ValueError
            if UUID(str(job.organization_id)) != organization_id:
                raise ValueError
        except (AttributeError, TypeError, ValueError, UnsafePayload):
            raise PermanentJobError("retention_sweep_payload_invalid") from None
        await asyncio.to_thread(self._execute, organization_id, scheduled_date)

    def _execute(self, organization_id: UUID, scheduled_date: date) -> int:
        if self._sessions is None:
            raise RuntimeError("retention sweep dependencies are unavailable")
        with self._sessions.begin() as db:
            queue = QueueRepository(db)
            now = aware(queue.database_now())
            policy = db.scalar(
                select(RetentionPolicy).where(
                    RetentionPolicy.organization_id == organization_id
                )
            )
            if policy is None:
                raise PermanentJobError("retention_policy_unavailable")
            candidates = list(
                db.scalars(
                    retention_candidate_claim(
                        organization_id, now, limit=self._batch_size
                    )
                )
            )
            created = 0
            for candidate in candidates:
                current_due = candidate_due_dates(
                    db,
                    organization_id,
                    policy.terminal_days,
                    {candidate.id},
                )[candidate.id]
                if current_due is None or aware(current_due) > now:
                    recalculate_due_dates(db, organization_id, {candidate.id: current_due})
                    continue
                if aware(candidate.retention_due_at) != aware(current_due):
                    recalculate_due_dates(db, organization_id, {candidate.id: current_due})
                    continue
                manifest, current_policy = build_private_manifest(db, candidate, now=now)
                if current_policy.version != policy.version or candidate.version != manifest["candidate_version"]:
                    continue
                db.add(
                    DeletionRequest(
                        organization_id=organization_id,
                        candidate_id=candidate.id,
                        status="requested",
                        reason_code="retention_expired",
                        requested_by=None,
                        requested_at=now,
                        impact_manifest=manifest,
                        manifest_hash=canonical_manifest_hash(manifest),
                        manifest_schema_version=1,
                        policy_version=current_policy.version,
                        candidate_version=candidate.version,
                        recovery_generation=0,
                    )
                )
                created += 1
            next_date = scheduled_date + timedelta(days=1)
            queue.enqueue(
                organization_id,
                "governance.retention_sweep",
                {
                    "organization_id": str(organization_id),
                    "scheduled_date": str(next_date),
                },
                run_after=datetime.combine(next_date, time.min, tzinfo=timezone.utc),
                dedupe_key=f"retention-sweep:{organization_id}:{next_date}",
                max_attempts=3,
            )
            return created


def seed_retention_sweeps(sessions, scheduled_date: date) -> int:
    with sessions.begin() as db:
        queue = QueueRepository(db)
        created = 0
        for organization_id in db.scalars(
            select(Organization.id)
            .where(Organization.status == "active")
            .order_by(Organization.id)
        ):
            dedupe_key = f"retention-sweep:{organization_id}:{scheduled_date}"
            existing = db.scalar(
                select(BackgroundJob.id)
                .where(
                    BackgroundJob.organization_id == organization_id,
                    BackgroundJob.type == "governance.retention_sweep",
                    BackgroundJob.dedupe_key == dedupe_key,
                    BackgroundJob.status.in_(("queued", "running")),
                )
            )
            if existing is not None:
                continue
            queue.enqueue(
                organization_id,
                "governance.retention_sweep",
                {
                    "organization_id": str(organization_id),
                    "scheduled_date": str(scheduled_date),
                },
                run_after=datetime.combine(scheduled_date, time.min, tzinfo=timezone.utc),
                dedupe_key=dedupe_key,
                max_attempts=3,
            )
            created += 1
        return created


def main() -> int:
    from server.app.core.settings import Settings
    from server.app.identity.store import IdentityStore

    parser = argparse.ArgumentParser(description="Seed the first retention sweep jobs")
    parser.add_argument("--scheduled-date", required=True, type=date.fromisoformat)
    args = parser.parse_args()
    store = IdentityStore(Settings.from_environment().database_url)
    seeded = seed_retention_sweeps(store.sync_session, args.scheduled_date)
    print(f"retention_sweeps_seeded={seeded}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
