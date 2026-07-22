from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4, uuid5

import pytest

from sqlalchemy import func, select

from server.app.governance.models import RetentionPolicy
from server.app.identity.models import Job
from server.app.recruiting.models import Application, Candidate
from server.app.screening.models import ScreeningRun
from server.app.talent import service as talent_service
from server.app.talent.models import TalentPool, TalentPoolMembership
from server.app.talent.service import (
    DEFERRED_POOL_SYSTEM_KEY,
    DeferredPoolUnavailableError,
    ensure_deferred_membership,
)
from server.tests.test_recruiting_api import make_app
from server.tests.test_screening_routing import seed_routing_case


EXPECTED_POOL_CREATE_MAX_ATTEMPTS = 5


def expected_system_pool_names(organization_id):
    names = ["AI 初筛暂缓"]
    for attempt in range(1, EXPECTED_POOL_CREATE_MAX_ATTEMPTS):
        suffix = uuid5(
            organization_id,
            f"{DEFERRED_POOL_SYSTEM_KEY}:{attempt}",
        ).hex[:16]
        names.append(f"AI 初筛暂缓 [{suffix}]")
    return names


def add_ordinary_pool(db, case, name, purpose):
    pool = TalentPool(
        organization_id=case.organization_id,
        name=name,
        purpose=purpose,
        visibility="private",
        owner_id=case.creator_id,
        system_key=None,
        suitable_roles=[],
        retention_days=730,
    )
    db.add(pool)
    return pool


def test_deferred_membership_uses_system_pool_admin_and_retention_policy(tmp_path):
    app = make_app(tmp_path)
    case = seed_routing_case(app, suffix="deferred")
    with app.state.identity_store.sync_session() as db:
        policy = db.scalar(
            select(RetentionPolicy).where(
                RetentionPolicy.organization_id == case.organization_id
            )
        )
        policy.talent_pool_days = 365
        application = db.get(Application, case.application_id)
        candidate = db.get(Candidate, case.candidate_id)
        job = db.get(Job, case.job_id)
        run = db.get(ScreeningRun, case.run_id)
        before = datetime.now(timezone.utc)
        membership = ensure_deferred_membership(
            db,
            application=application,
            candidate=candidate,
            job=job,
            run=run,
            score=59,
            transferable_capabilities=["Distributed systems", "Python", "SQL"],
        )
        db.flush()
        pool = db.get(TalentPool, membership.pool_id)
        assert pool.system_key == DEFERRED_POOL_SYSTEM_KEY
        assert pool.visibility == "recruiting_team"
        assert pool.owner_id == case.admin_id
        assert pool.retention_days == 365
        assert membership.source_application_id == case.application_id
        assert membership.owner_id == case.creator_id
        assert membership.reason == "LLM 初筛分低于 60"
        assert membership.tags == [
            "Platform Engineer deferred",
            "LLM 59分",
            "Distributed systems",
            "Python",
            "SQL",
        ]
        assert membership.retention_until >= before


def test_deferred_membership_upsert_does_not_duplicate_pool_or_membership(tmp_path):
    app = make_app(tmp_path)
    case = seed_routing_case(app, suffix="upsert", with_admin=False)
    with app.state.identity_store.sync_session() as db:
        policy = db.scalar(
            select(RetentionPolicy).where(
                RetentionPolicy.organization_id == case.organization_id
            )
        )
        policy.talent_pool_days = 365
        application = db.get(Application, case.application_id)
        candidate = db.get(Candidate, case.candidate_id)
        job = db.get(Job, case.job_id)
        run = db.get(ScreeningRun, case.run_id)
        first = ensure_deferred_membership(
            db,
            application=application,
            candidate=candidate,
            job=job,
            run=run,
            score=10,
            transferable_capabilities=["A", "B", "C", "D", "E", "ignored"],
        )
        db.flush()
        pool = db.get(TalentPool, first.pool_id)
        initial_pool_version = pool.version
        ensure_deferred_membership(
            db,
            application=application,
            candidate=candidate,
            job=job,
            run=run,
            score=15,
            transferable_capabilities=["Unchanged retention"],
        )
        assert pool.version == initial_pool_version
        policy.talent_pool_days = 400
        second = ensure_deferred_membership(
            db,
            application=application,
            candidate=candidate,
            job=job,
            run=run,
            score=20,
            transferable_capabilities=["Updated"],
        )
        assert first.id == second.id
        assert second.tags == ["Platform Engineer upsert", "LLM 20分", "Updated"]
        pool = db.get(TalentPool, second.pool_id)
        assert pool.owner_id == case.creator_id
        assert pool.retention_days == 400
        assert pool.version == initial_pool_version + 1
        ensure_deferred_membership(
            db,
            application=application,
            candidate=candidate,
            job=job,
            run=run,
            score=25,
            transferable_capabilities=["Still 400 days"],
        )
        assert pool.version == initial_pool_version + 1
        assert db.scalar(select(func.count(TalentPool.id))) == 1
        assert db.scalar(select(func.count(TalentPoolMembership.id))) == 1


def test_system_deferred_pool_uses_bounded_backup_after_multiple_name_conflicts(tmp_path):
    app = make_app(tmp_path)
    case = seed_routing_case(app, suffix="pool-name-conflict")
    with app.state.identity_store.sync_session() as db:
        candidate_names = expected_system_pool_names(case.organization_id)
        ordinary_pools = [
            add_ordinary_pool(db, case, name, f"Ordinary pool {index}")
            for index, name in enumerate(candidate_names[:3])
        ]
        db.flush()
        ordinary_snapshot = [
            (pool.id, pool.name, pool.purpose, pool.visibility, pool.system_key)
            for pool in ordinary_pools
        ]
        membership = ensure_deferred_membership(
            db,
            application=db.get(Application, case.application_id),
            candidate=db.get(Candidate, case.candidate_id),
            job=db.get(Job, case.job_id),
            run=db.get(ScreeningRun, case.run_id),
            score=59,
        )
        db.flush()

        system_pool = db.get(TalentPool, membership.pool_id)
        assert system_pool.id not in {pool.id for pool in ordinary_pools}
        assert system_pool.system_key == DEFERRED_POOL_SYSTEM_KEY
        assert system_pool.name == candidate_names[3]
        assert [
            (pool.id, pool.name, pool.purpose, pool.visibility, pool.system_key)
            for pool in ordinary_pools
        ] == ordinary_snapshot
        assert db.scalar(select(func.count(TalentPool.id))) == 4


def test_system_deferred_pool_exhaustion_is_bounded_and_preserves_outer_transaction(
    tmp_path,
):
    app = make_app(tmp_path)
    case = seed_routing_case(app, suffix="pool-name-exhausted")
    with app.state.identity_store.sync_session() as db:
        candidate_names = expected_system_pool_names(case.organization_id)
        ordinary_pools = [
            add_ordinary_pool(db, case, name, f"Occupied {index}")
            for index, name in enumerate(candidate_names)
        ]
        sentinel = add_ordinary_pool(db, case, "Outer transaction sentinel", "keep-me")
        db.flush()
        ordinary_snapshot = [
            (pool.id, pool.name, pool.purpose, pool.visibility, pool.system_key)
            for pool in ordinary_pools
        ]
        real_begin_nested = db.begin_nested
        attempts = 0

        def counted_begin_nested():
            nonlocal attempts
            attempts += 1
            return real_begin_nested()

        with patch.object(db, "begin_nested", side_effect=counted_begin_nested):
            with pytest.raises(
                DeferredPoolUnavailableError,
                match="deferred_pool_name_exhausted",
            ):
                ensure_deferred_membership(
                    db,
                    application=db.get(Application, case.application_id),
                    candidate=db.get(Candidate, case.candidate_id),
                    job=db.get(Job, case.job_id),
                    run=db.get(ScreeningRun, case.run_id),
                    score=59,
                )

        assert (
            talent_service.DEFERRED_POOL_CREATE_MAX_ATTEMPTS
            == EXPECTED_POOL_CREATE_MAX_ATTEMPTS
        )
        assert attempts == EXPECTED_POOL_CREATE_MAX_ATTEMPTS
        assert db.in_transaction()
        assert db.get(TalentPool, sentinel.id).purpose == "keep-me"
        assert [
            (pool.id, pool.name, pool.purpose, pool.visibility, pool.system_key)
            for pool in ordinary_pools
        ] == ordinary_snapshot
        assert db.scalar(select(func.count(TalentPool.id))) == len(ordinary_pools) + 1


def test_deferred_membership_rejects_cross_tenant_run_context(tmp_path):
    app = make_app(tmp_path)
    case = seed_routing_case(app, suffix="deferred-tenant")
    with app.state.identity_store.sync_session() as db:
        application = db.get(Application, case.application_id)
        candidate = db.get(Candidate, case.candidate_id)
        job = db.get(Job, case.job_id)
        wrong_run = SimpleNamespace(
            organization_id=uuid4(),
            job_id=case.job_id,
            created_by=case.creator_id,
        )
        with pytest.raises(ValueError, match="deferred_membership_context_mismatch"):
            ensure_deferred_membership(
                db,
                application=application,
                candidate=candidate,
                job=job,
                run=wrong_run,
                score=59,
            )
