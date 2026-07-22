from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from server.app.governance.deletion_models import DeletionRequest, LegalHold
from server.app.governance.deletion_service import build_private_manifest, canonical_manifest_hash
from server.app.queue.models import BackgroundJob
from server.app.recruiting.models import Candidate
from server.tests.test_governance_deletion_api import candidate_for, make_app
from server.tests.test_recruiting_api import seed_user


def _job(job_organization_id, scheduled_date="2026-07-15", **changes):
    payload = {
        "organization_id": str(job_organization_id),
        "scheduled_date": scheduled_date,
    }
    payload.update(changes)
    return SimpleNamespace(
        organization_id=job_organization_id,
        payload=payload,
        trace_id="retention-sweep-test",
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"scheduled_date": "15-07-2026"},
        {"organization_id": "not-a-uuid"},
        {"unexpected": "field"},
    ],
)
def test_retention_handler_rejects_non_exact_payload_before_dependencies(changes) -> None:
    from server.app.governance.retention_sweep import RetentionSweepJobHandler
    from server.app.queue.service import PermanentJobError

    organization_id = uuid4()
    with pytest.raises(PermanentJobError) as raised:
        asyncio.run(RetentionSweepJobHandler(None, batch_size=10)(_job(organization_id, **changes)))

    assert raised.value.safe_code == "retention_sweep_payload_invalid"


def test_retention_sweep_creates_only_current_eligible_requests_and_schedules_next_day(
    tmp_path,
) -> None:
    from server.app.governance.retention_sweep import RetentionSweepJobHandler

    app = make_app(tmp_path)
    user_id = seed_user(app, "system_admin", "retention-sweep@example.test")
    candidate_ids = [candidate_for(app, user_id) for _ in range(5)]
    eligible_id, tombstone_id, hold_id, open_id, stale_id = candidate_ids
    old = datetime(2020, 1, 1, tzinfo=timezone.utc)
    with app.state.identity_store.sync_session.begin() as db:
        user = db.get(__import__("server.app.identity.models", fromlist=["User"]).User, user_id)
        for candidate_id in candidate_ids:
            candidate = db.get(Candidate, candidate_id)
            candidate.updated_at = old
            candidate.retention_due_at = old + timedelta(days=365)
        db.get(Candidate, tombstone_id).deleted_at = old
        db.add(
            LegalHold(
                organization_id=user.organization_id,
                candidate_id=hold_id,
                reason="hold",
                placed_by=user_id,
            )
        )
        open_candidate = db.get(Candidate, open_id)
        manifest, policy = build_private_manifest(db, open_candidate, now=old)
        db.add(
            DeletionRequest(
                organization_id=user.organization_id,
                candidate_id=open_id,
                reason_code="candidate_request",
                requested_by=user_id,
                impact_manifest=manifest,
                manifest_hash=canonical_manifest_hash(manifest),
                policy_version=policy.version,
                candidate_version=open_candidate.version,
            )
        )
        db.get(Candidate, stale_id).updated_at = datetime.now(timezone.utc)
        organization_id = user.organization_id

    handler = RetentionSweepJobHandler(
        app.state.identity_store.sync_session,
        batch_size=10,
    )
    asyncio.run(handler(_job(organization_id)))

    with app.state.identity_store.sync_session() as db:
        requests = list(db.scalars(select(DeletionRequest).order_by(DeletionRequest.created_at)))
        generated = [row for row in requests if row.reason_code == "retention_expired"]
        assert len(generated) == 1
        row = generated[0]
        assert row.candidate_id == eligible_id
        assert row.status == "requested"
        assert row.requested_by is None
        assert row.policy_version == row.impact_manifest["policy_version"]
        assert row.candidate_version == row.impact_manifest["candidate_version"]
        assert row.manifest_hash == canonical_manifest_hash(row.impact_manifest)
        assert db.get(Candidate, stale_id).retention_due_at > datetime.now(timezone.utc).replace(tzinfo=None)
        next_jobs = list(
            db.scalars(
                select(BackgroundJob).where(
                    BackgroundJob.type == "governance.retention_sweep"
                )
            )
        )
        assert len(next_jobs) == 1
        assert next_jobs[0].dedupe_key == f"retention-sweep:{organization_id}:2026-07-16"
        assert next_jobs[0].payload == {
            "organization_id": str(organization_id),
            "scheduled_date": "2026-07-16",
        }
        assert db.scalar(
            select(func.count()).select_from(DeletionRequest).where(
                DeletionRequest.status != "requested"
            )
        ) == 0


def test_retention_candidate_claim_is_bounded_and_skip_locked() -> None:
    from sqlalchemy.dialects import postgresql
    from server.app.governance.retention_sweep import retention_candidate_claim

    statement = retention_candidate_claim(uuid4(), datetime.now(timezone.utc), limit=25)
    rendered = str(statement.compile(dialect=postgresql.dialect())).upper()

    assert "LIMIT" in rendered
    assert "FOR UPDATE SKIP LOCKED" in rendered


def test_explicit_release_seed_is_idempotent_and_never_runs_as_migration_side_effect(
    tmp_path,
) -> None:
    from server.app.governance.retention_sweep import seed_retention_sweeps

    app = make_app(tmp_path)
    seed_user(app, "system_admin", "retention-seed@example.test")

    assert seed_retention_sweeps(
        app.state.identity_store.sync_session, date(2026, 7, 15)
    ) == 1
    assert seed_retention_sweeps(
        app.state.identity_store.sync_session, date(2026, 7, 15)
    ) == 0
    with app.state.identity_store.sync_session() as db:
        jobs = list(
            db.scalars(
                select(BackgroundJob).where(
                    BackgroundJob.type == "governance.retention_sweep"
                )
            )
        )
        assert len(jobs) == 1
        assert jobs[0].dedupe_key.endswith(":2026-07-15")
