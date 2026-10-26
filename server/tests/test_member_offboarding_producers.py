import asyncio
from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select

from server.app.identity.models import AuditLog, Job, User, UserStatus
from server.app.identity.offboarding_resolver import (
    OperationalOwnerUnavailable,
    resolve_operational_user_id,
)
from server.app.queue.service import RetryableJobError
from server.app.recruiting.models import Application, Candidate, FileObject
from server.app.screening.models import ScreeningItem, ScreeningRun
from server.app.screening.routing import route_llm_screening_terminal
from server.app.talent.models import TalentPool, TalentPoolMembership
from server.app.talent.service import ensure_deferred_membership
from server.tests.test_recruiting_api import make_app, seed_user
from server.tests.test_screening_pipeline import seeded_pipeline
from server.tests.test_screening_routing import seed_routing_case


def remove_member(db, organization_id, user_id, successor_id):
    from server.app.identity.offboarding_models import MemberOffboarding

    db.get(User, user_id).status = UserStatus.DISABLED
    record = MemberOffboarding(
        organization_id=organization_id,
        user_id=user_id,
        successor_user_id=successor_id,
        removed_by=user_id,
        removed_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
        version=1,
        status="completed" if successor_id else "pending",
        unresolved=[],
    )
    db.add(record)
    db.flush()
    return record


def test_active_original_keeps_baseline_without_new_role_or_job_checks(tmp_path):
    app = make_app(tmp_path)
    user_id = seed_user(app, "system_admin", "original@example.test")
    with app.state.identity_store.sync_session() as db:
        organization_id = db.get(User, user_id).organization_id
        assert resolve_operational_user_id(db, organization_id, user_id, job_id=uuid4()) == user_id


def test_disabled_source_without_durable_handover_is_unavailable(tmp_path):
    app = make_app(tmp_path)
    user_id = seed_user(app, "recruiting_admin", "original@example.test")
    with app.state.identity_store.sync_session() as db:
        user = db.get(User, user_id)
        user.status = UserStatus.DISABLED
        db.flush()
        with pytest.raises(OperationalOwnerUnavailable):
            resolve_operational_user_id(db, user.organization_id, user_id)


def test_removed_original_follows_latest_successor_chain(tmp_path):
    app = make_app(tmp_path)
    case = seed_routing_case(app)
    destination = seed_user(app, "recruiting_admin", "destination@example.test")
    with app.state.identity_store.sync_session() as db:
        remove_member(db, case.organization_id, case.creator_id, case.admin_id)
        remove_member(db, case.organization_id, case.admin_id, destination)
        assert resolve_operational_user_id(db, case.organization_id, case.creator_id, job_id=case.job_id) == destination


@pytest.mark.parametrize("failure", ["no_successor", "disabled", "invited", "missing", "cycle", "role", "job_access", "tenant"])
def test_unavailable_successor_never_falls_back_to_another_admin(tmp_path, failure):
    app = make_app(tmp_path)
    case = seed_routing_case(app)
    role = "hiring_manager" if failure == "role" else "recruiter" if failure == "job_access" else "recruiting_admin"
    destination = seed_user(app, role, "destination@example.test")
    with app.state.identity_store.sync_session() as db:
        successor_id = None if failure == "no_successor" else destination
        record = remove_member(db, case.organization_id, case.creator_id, successor_id)
        if failure == "disabled":
            db.get(User, destination).status = UserStatus.DISABLED
            db.flush()
        elif failure == "invited":
            db.get(User, destination).status = UserStatus.INVITED
            db.flush()
        elif failure == "missing":
            # Keep an impossible destination in-memory; the durable FK separately rejects it.
            record.successor_user_id = uuid4()
        elif failure == "cycle":
            remove_member(db, case.organization_id, destination, case.creator_id)
        if failure == "tenant":
            organization_id = uuid4()
        else:
            organization_id = case.organization_id
        with db.no_autoflush:
            with pytest.raises(OperationalOwnerUnavailable) as error:
                resolve_operational_user_id(db, organization_id, case.creator_id, job_id=case.job_id)
        assert isinstance(error.value, RetryableJobError)


def test_successor_uses_existing_recruiter_organization_scope(tmp_path):
    app = make_app(tmp_path)
    case = seed_routing_case(app)
    successor = seed_user(app, "recruiter", "successor@example.test")
    with app.state.identity_store.sync_session() as db:
        db.get(User, successor).recruiting_scope_type = "organization"
        remove_member(db, case.organization_id, case.creator_id, successor)
        assert resolve_operational_user_id(db, case.organization_id, case.creator_id, job_id=case.job_id) == successor


def test_late_system_pool_uses_selected_successor_not_an_arbitrary_admin(tmp_path):
    app = make_app(tmp_path)
    case = seed_routing_case(app)
    successor = seed_user(app, "recruiter", "successor@example.test")
    with app.state.identity_store.sync_session() as db:
        db.get(User, successor).recruiting_scope_type = "organization"
        remove_member(db, case.organization_id, case.creator_id, successor)
        membership = ensure_deferred_membership(
            db, application=db.get(Application, case.application_id),
            candidate=db.get(Candidate, case.candidate_id), job=db.get(Job, case.job_id),
            run=db.get(ScreeningRun, case.run_id), score=45,
        )
        db.flush()
        assert membership.owner_id == successor
        assert db.get(TalentPool, membership.pool_id).owner_id == successor


@pytest.mark.parametrize("has_successor", [True, False])
def test_late_parse_uses_operational_owner_and_preserves_original_authors(tmp_path, has_successor):
    app, pipeline, _, _, queue_job, run_data, item_data = seeded_pipeline(tmp_path)
    successor = seed_user(app, "recruiting_admin", "successor@example.test")
    with app.state.identity_store.sync_session() as db:
        run = db.get(ScreeningRun, UUID(run_data["id"]))
        creator_id = run.created_by
        remove_member(db, run.organization_id, creator_id, successor if has_successor else None)
        db.commit()
    if has_successor:
        asyncio.run(pipeline.parse_item(queue_job))
    else:
        with pytest.raises(OperationalOwnerUnavailable):
            asyncio.run(pipeline.parse_item(queue_job))
    with app.state.identity_store.sync_session() as db:
        item = db.get(ScreeningItem, UUID(item_data["id"]))
        run = db.get(ScreeningRun, UUID(run_data["id"]))
        assert run.created_by == creator_id
        assert db.get(FileObject, item.file_object_id).uploaded_by == creator_id
        if has_successor:
            assert db.get(Candidate, item.candidate_id).owner_id == successor
            assert db.get(Application, item.application_id).owner_id == successor
            assert item.status == "parsed"
        else:
            assert item.status not in {"parsed", "scored"}
            assert item.finished_at is None
            assert db.scalar(select(func.count(Candidate.id))) == 0
            assert db.scalar(select(func.count(Application.id))) == 0


def test_parse_retries_after_pending_handover_gets_a_successor(tmp_path):
    from server.app.identity.offboarding_models import MemberOffboarding

    app, pipeline, _, _, queue_job, run_data, item_data = seeded_pipeline(tmp_path)
    successor = seed_user(app, "recruiting_admin", "successor@example.test")
    with app.state.identity_store.sync_session() as db:
        run = db.get(ScreeningRun, UUID(run_data["id"]))
        creator_id, organization_id = run.created_by, run.organization_id
        record_id = remove_member(db, organization_id, creator_id, None).id
        db.commit()
    for attempt in (1, 2):
        queue_job.attempts = attempt
        with pytest.raises(OperationalOwnerUnavailable):
            asyncio.run(pipeline.parse_item(queue_job))
    with app.state.identity_store.sync_session() as db:
        db.get(MemberOffboarding, record_id).successor_user_id = successor
        db.commit()
    queue_job.attempts = 3
    asyncio.run(pipeline.parse_item(queue_job))
    with app.state.identity_store.sync_session() as db:
        item = db.get(ScreeningItem, UUID(item_data["id"]))
        assert db.get(Application, item.application_id).owner_id == successor
        assert db.get(ScreeningRun, UUID(run_data["id"])).created_by == creator_id


@pytest.mark.parametrize("existing", [True, False])
def test_late_talent_creation_and_followup_do_not_restore_removed_owner(tmp_path, existing):
    app = make_app(tmp_path)
    case = seed_routing_case(app)
    with app.state.identity_store.sync_session() as db:
        context = dict(application=db.get(Application, case.application_id), candidate=db.get(Candidate, case.candidate_id), job=db.get(Job, case.job_id), run=db.get(ScreeningRun, case.run_id), score=45)
        if existing:
            membership = ensure_deferred_membership(db, **context)
            db.flush()
            membership.owner_id = case.admin_id
        remove_member(db, case.organization_id, case.creator_id, case.admin_id)
        membership = ensure_deferred_membership(db, **context)
        db.flush()
        assert membership.owner_id == case.admin_id
        assert context["run"].created_by == case.creator_id
        assert db.scalar(select(func.count(TalentPoolMembership.id))) == 1


@pytest.mark.parametrize("score", [45, 91])
def test_late_talent_without_successor_rolls_back_routing_and_keeps_audit_authors(tmp_path, score):
    app = make_app(tmp_path)
    case = seed_routing_case(app)
    with app.state.identity_store.sync_session() as db:
        remove_member(db, case.organization_id, case.creator_id, None)
        db.commit()
    with app.state.identity_store.sync_session() as db:
        with pytest.raises(OperationalOwnerUnavailable):
            route_llm_screening_terminal(db, organization_id=case.organization_id, item_id=case.item_id, actor_user_id=case.creator_id, score=score, ai_status="succeeded", safe_error_code=None, trace_id="offboarding-test", evaluation_id=uuid4(), invocation_id=uuid4())
        db.rollback()
        assert db.get(Application, case.application_id).stage == "new"
        assert db.scalar(select(func.count(TalentPoolMembership.id))) == 0
        assert db.scalar(select(func.count(TalentPool.id))) == 0
        assert db.scalar(select(func.count(AuditLog.id))) == 0


@pytest.mark.parametrize("score", [45, 91])
def test_late_routing_preserves_immutable_audit_actor(tmp_path, score):
    app = make_app(tmp_path)
    case = seed_routing_case(app)
    with app.state.identity_store.sync_session() as db:
        remove_member(db, case.organization_id, case.creator_id, case.admin_id)
        route_llm_screening_terminal(db, organization_id=case.organization_id, item_id=case.item_id, actor_user_id=case.creator_id, score=score, ai_status="succeeded", safe_error_code=None, trace_id="offboarding-test", evaluation_id=uuid4(), invocation_id=uuid4())
        if score < 60:
            assert db.scalar(select(TalentPoolMembership.owner_id)) == case.admin_id
        assert db.get(Candidate, case.candidate_id).owner_id == case.admin_id
        assert db.get(Application, case.application_id).owner_id == case.admin_id
        assert db.scalar(select(AuditLog.actor_user_id)) == case.creator_id
        assert db.get(ScreeningRun, case.run_id).created_by == case.creator_id


@pytest.mark.parametrize("removed", [False, True])
def test_routing_preserves_other_active_candidate_and_application_owners(tmp_path, removed):
    app = make_app(tmp_path)
    case = seed_routing_case(app)
    other_owner = seed_user(app, "recruiting_admin", "other-owner@example.test")
    with app.state.identity_store.sync_session() as db:
        candidate = db.get(Candidate, case.candidate_id)
        application = db.get(Application, case.application_id)
        candidate.owner_id = application.owner_id = other_owner
        if removed:
            remove_member(db, case.organization_id, case.creator_id, case.admin_id)
        route_llm_screening_terminal(db, organization_id=case.organization_id, item_id=case.item_id, actor_user_id=case.creator_id, score=91, ai_status="succeeded", safe_error_code=None, trace_id="offboarding-test", evaluation_id=uuid4(), invocation_id=uuid4())
        assert candidate.owner_id == application.owner_id == other_owner


@pytest.mark.parametrize("when", ["queued", "during_evaluation"])
def test_late_llm_without_successor_does_not_succeed_on_retry(tmp_path, when):
    from server.app.identity.offboarding_models import MemberOffboarding
    from server.app.screening.llm_pipeline import LlmScreeningPipeline
    from server.tests.test_llm_pipeline import Gateway, prepared, screening_evaluation

    app, cipher, queue_job = prepared(tmp_path)
    record_id = creator_id = None

    def offboard_once():
        nonlocal record_id, creator_id
        if record_id is None:
            with app.state.identity_store.sync_session() as db:
                item = db.get(ScreeningItem, UUID(queue_job.payload["screening_item_id"]))
                run = db.get(ScreeningRun, item.run_id)
                creator_id = run.created_by
                record_id = remove_member(db, run.organization_id, creator_id, None).id
                db.commit()

    gateway = Gateway(outcome=screening_evaluation(45), inspect=offboard_once if when == "during_evaluation" else None)
    pipeline = LlmScreeningPipeline(app.state.identity_store.sync_session, gateway, cipher)
    if when == "queued":
        offboard_once()
    for attempt in (1, 2):
        queue_job.attempts = attempt
        with pytest.raises(OperationalOwnerUnavailable):
            asyncio.run(pipeline.evaluate_item(queue_job))
        with app.state.identity_store.sync_session() as db:
            item = db.get(ScreeningItem, UUID(queue_job.payload["screening_item_id"]))
            assert item.finished_at is None
            assert item.llm_status not in {"succeeded", "failed", "skipped"}
            assert db.scalar(select(func.count(TalentPoolMembership.id))) == 0
    successor = seed_user(app, "recruiting_admin", "successor@example.test")
    with app.state.identity_store.sync_session() as db:
        db.get(MemberOffboarding, record_id).successor_user_id = successor
        db.commit()
    queue_job.attempts = 3
    asyncio.run(pipeline.evaluate_item(queue_job))
    with app.state.identity_store.sync_session() as db:
        item = db.get(ScreeningItem, UUID(queue_job.payload["screening_item_id"]))
        assert item.llm_status == "succeeded"
        assert item.finished_at is not None
        assert db.scalar(select(TalentPoolMembership.owner_id)) == successor
        assert db.get(ScreeningRun, item.run_id).created_by == creator_id
