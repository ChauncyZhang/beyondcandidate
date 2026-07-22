from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from server.app.governance import audit as governance_audit
from server.app.governance.audit import AuditValidationError
from server.app.identity.models import AuditLog, Job, Organization, User
from server.app.recruiting.models import (
    Application,
    ApplicationReviewTask,
    ApplicationStageEvent,
    Candidate,
    FileObject,
    JobJdVersion,
    Resume,
    ScreeningRuleVersion,
)
from server.app.screening.models import ScreeningItem, ScreeningRun
from server.app.screening.routing import (
    ScreeningOutcome,
    ScreeningRoutingConflict,
    ScreeningRoutingResult,
    derive_screening_outcome,
    route_llm_screening_terminal,
)
from server.app.talent.models import TalentPoolMembership
from server.tests.test_recruiting_api import make_app, seed_user


def seed_routing_case(app, *, suffix="route", with_admin=True):
    creator_id = seed_user(app, "recruiter", f"creator-{suffix}@example.test")
    manager_id = seed_user(app, "hiring_manager", f"manager-{suffix}@example.test")
    admin_id = (
        seed_user(app, "recruiting_admin", f"admin-{suffix}@example.test")
        if with_admin
        else None
    )
    with app.state.identity_store.sync_session() as db:
        creator = db.get(User, creator_id)
        organization = db.get(Organization, creator.organization_id)
        job = Job(
            organization_id=organization.id,
            title=f"Platform Engineer {suffix}",
            owner_id=creator.id,
            hiring_owner_id=manager_id,
            status="open",
        )
        candidate = Candidate(
            organization_id=organization.id,
            display_name=f"Candidate {suffix}",
            owner_id=creator.id,
        )
        file_object = FileObject(
            organization_id=organization.id,
            storage_key=f"private/{suffix}.pdf",
            original_filename=f"{suffix}.pdf",
            mime_type="application/pdf",
            size_bytes=1,
            sha256=(suffix[0] * 64),
            uploaded_by=creator.id,
        )
        db.add_all([job, candidate, file_object])
        db.flush()
        resume = Resume(
            organization_id=organization.id,
            candidate_id=candidate.id,
            file_object_id=file_object.id,
            version_number=1,
            parsed_text="private resume text that must never enter audit metadata",
        )
        jd = JobJdVersion(
            organization_id=organization.id,
            job_id=job.id,
            version_number=1,
            content={"text": "private JD text that must never enter audit metadata"},
            created_by=creator.id,
        )
        rule = ScreeningRuleVersion(
            organization_id=organization.id,
            job_id=job.id,
            version_number=1,
            content={"required_terms": [], "bonus_terms": []},
            created_by=creator.id,
        )
        db.add_all([resume, jd, rule])
        db.flush()
        application = Application(
            organization_id=organization.id,
            candidate_id=candidate.id,
            job_id=job.id,
            resume_id=resume.id,
            owner_id=creator.id,
            stage="new",
            source="screening",
        )
        run = ScreeningRun(
            organization_id=organization.id,
            job_id=job.id,
            jd_version_id=jd.id,
            rule_version_id=rule.id,
            source="upload",
            status="llm_scoring",
            total_count=1,
            processed_count=0,
            succeeded_count=0,
            failed_count=0,
            created_by=creator.id,
        )
        db.add_all([application, run])
        db.flush()
        item = ScreeningItem(
            organization_id=organization.id,
            run_id=run.id,
            file_object_id=file_object.id,
            candidate_id=candidate.id,
            resume_id=resume.id,
            application_id=application.id,
            status="scored",
            attempts=1,
        )
        db.add(item)
        db.commit()
        return SimpleNamespace(
            organization_id=organization.id,
            creator_id=creator.id,
            manager_id=manager_id,
            admin_id=admin_id,
            job_id=job.id,
            candidate_id=candidate.id,
            application_id=application.id,
            run_id=run.id,
            item_id=item.id,
        )


def evidence_ids():
    return {"evaluation_id": uuid4(), "invocation_id": uuid4()}


@pytest.mark.parametrize(
    ("score", "recommendation", "stage"),
    [
        (85, "优先评审", "review"),
        (60, "建议评审", "review"),
        (59, "暂缓", "deferred"),
        (0, "暂缓", "deferred"),
    ],
)
def test_derive_screening_outcome(score, recommendation, stage):
    assert derive_screening_outcome(score) == ScreeningOutcome(recommendation, stage)


@pytest.mark.parametrize("score", [-1, 101])
def test_derive_screening_outcome_rejects_out_of_range_scores(score):
    with pytest.raises(ValueError, match="score_out_of_range"):
        derive_screening_outcome(score)


def test_successful_terminal_route_persists_explicit_evidence_ids(tmp_path):
    app = make_app(tmp_path)
    case = seed_routing_case(app, suffix="evidence-ids")
    evaluation_id = uuid4()
    invocation_id = uuid4()

    with app.state.identity_store.sync_session() as db:
        route_llm_screening_terminal(
            db,
            organization_id=case.organization_id,
            item_id=case.item_id,
            actor_user_id=case.creator_id,
            score=48,
            ai_status="succeeded",
            safe_error_code=None,
            trace_id="trace-evidence-ids",
            evaluation_id=evaluation_id,
            invocation_id=invocation_id,
        )
        audit = db.scalar(select(AuditLog).where(AuditLog.event_type == "screening.terminal_routed"))

    assert audit.metadata_json["evaluation_id"] == str(evaluation_id)
    assert audit.metadata_json["invocation_id"] == str(invocation_id)


def test_final_llm_failure_fails_open_without_fake_score_or_internal_commit(tmp_path):
    app = make_app(tmp_path)
    case = seed_routing_case(app, suffix="failed")

    with app.state.identity_store.sync_session() as db:
        with patch.object(db, "commit", side_effect=AssertionError("router committed")):
            result = route_llm_screening_terminal(
                db,
                organization_id=case.organization_id,
                item_id=case.item_id,
                actor_user_id=case.creator_id,
                score=None,
                ai_status="failed",
                safe_error_code="provider_quota_or_rate_limited",
                trace_id="trace-safe",
            )
        assert result.stage == "review"
        assert result.score is None
        assert result.recommendation == "AI评分不可用"
        application = db.get(Application, case.application_id)
        task = db.scalar(select(ApplicationReviewTask))
        audit = db.scalar(select(AuditLog).where(AuditLog.event_type == "screening.terminal_routed"))
        assert application.stage == "review" and application.version == 2
        assert task.status == "open" and task.ai_status == "failed"
        assert task.safe_error_code == "provider_quota_or_rate_limited"
        assert set(audit.metadata_json) == {
            "application_id",
            "item_id",
            "from_stage",
            "to_stage",
            "ai_status",
            "recommendation",
            "safe_error_code",
        }
        assert "private" not in str(audit.metadata_json).lower()


def test_failure_route_normalizes_unsafe_error_text_before_persistence(tmp_path):
    app = make_app(tmp_path)
    case = seed_routing_case(app, suffix="unsafe")
    with app.state.identity_store.sync_session() as db:
        route_llm_screening_terminal(
            db,
            organization_id=case.organization_id,
            item_id=case.item_id,
            actor_user_id=case.creator_id,
            score=None,
            ai_status="failed",
            safe_error_code="candidate_alice_resume_prompt",
            trace_id="trace-safe",
        )
        task = db.scalar(select(ApplicationReviewTask))
        audit = db.scalar(
            select(AuditLog).where(AuditLog.event_type == "screening.terminal_routed")
        )
        assert task.safe_error_code == "internal_error"
        assert audit.metadata_json["safe_error_code"] == "internal_error"
        assert "candidate_alice_resume_prompt" not in str(audit.metadata_json)


def test_router_uses_central_audit_metadata_validation(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    case = seed_routing_case(app, suffix="audit-boundary")
    monkeypatch.setitem(
        governance_audit.EVENT_METADATA_ALLOWLIST,
        "screening.terminal_routed",
        {},
    )
    with app.state.identity_store.sync_session() as db:
        with pytest.raises(AuditValidationError, match="application_id"):
            route_llm_screening_terminal(
                db,
                organization_id=case.organization_id,
                item_id=case.item_id,
                actor_user_id=case.creator_id,
                score=85,
                ai_status="succeeded",
                safe_error_code=None,
                trace_id="trace-audit-boundary",
                **evidence_ids(),
            )


def test_repeated_and_stale_routing_does_not_duplicate_side_effects(tmp_path):
    app = make_app(tmp_path)
    case = seed_routing_case(app, suffix="repeat")

    with app.state.identity_store.sync_session() as db:
        first = route_llm_screening_terminal(
            db,
            organization_id=case.organization_id,
            item_id=case.item_id,
            actor_user_id=case.creator_id,
            score=60,
            ai_status="succeeded",
            safe_error_code=None,
            trace_id="trace-first",
            **evidence_ids(),
        )
        second = route_llm_screening_terminal(
            db,
            organization_id=case.organization_id,
            item_id=case.item_id,
            actor_user_id=case.creator_id,
            score=59,
            ai_status="succeeded",
            safe_error_code=None,
            trace_id="trace-stale",
        )
        assert first.stage == second.stage == "review"
        assert second.routed is False
        assert db.scalar(select(func.count(ApplicationStageEvent.id))) == 1
        assert db.scalar(select(func.count(ApplicationReviewTask.id))) == 1
        assert db.scalar(select(func.count(TalentPoolMembership.id))) == 0
        assert db.scalar(
            select(func.count(AuditLog.id)).where(
                AuditLog.event_type == "screening.terminal_routed"
            )
        ) == 1


def test_successful_retry_refreshes_fail_open_route_without_regressing_progress(tmp_path):
    app = make_app(tmp_path)
    case = seed_routing_case(app, suffix="fail-open-retry")

    with app.state.identity_store.sync_session() as db:
        route_llm_screening_terminal(
            db,
            organization_id=case.organization_id,
            item_id=case.item_id,
            actor_user_id=case.creator_id,
            score=None,
            ai_status="failed",
            safe_error_code="provider_unavailable",
            trace_id="trace-failed",
        )
        application = db.get(Application, case.application_id)
        application.stage = "contact"
        application.version += 1
        db.flush()

        retry_evidence = evidence_ids()
        retry = route_llm_screening_terminal(
            db,
            organization_id=case.organization_id,
            item_id=case.item_id,
            actor_user_id=case.creator_id,
            score=59,
            ai_status="succeeded",
            safe_error_code=None,
            trace_id="trace-retry",
            **retry_evidence,
        )
        replay = route_llm_screening_terminal(
            db,
            organization_id=case.organization_id,
            item_id=case.item_id,
            actor_user_id=case.creator_id,
            score=59,
            ai_status="succeeded",
            safe_error_code=None,
            trace_id="trace-retry",
            **retry_evidence,
        )

        audits = db.scalars(
            select(AuditLog)
            .where(AuditLog.event_type == "screening.terminal_routed")
            .order_by(AuditLog.created_at, AuditLog.id)
        ).all()
        task = db.scalar(select(ApplicationReviewTask))
        assert retry == ScreeningRoutingResult(
            recommendation="暂缓",
            stage="contact",
            score=59,
            routed=True,
        )
        assert replay == ScreeningRoutingResult(
            recommendation="暂缓",
            stage="contact",
            score=59,
            routed=False,
        )
        assert application.stage == "contact" and application.version == 3
        assert db.scalar(select(func.count(ApplicationStageEvent.id))) == 1
        assert db.scalar(select(func.count(ApplicationReviewTask.id))) == 1
        assert task.ai_status == "succeeded" and task.safe_error_code is None
        assert len(audits) == 2
        assert audits[-1].metadata_json == {
            "application_id": str(case.application_id),
            "item_id": str(case.item_id),
            "from_stage": "new",
            "to_stage": "review",
            "ai_status": "succeeded",
            "recommendation": "暂缓",
            "score": 59,
            "evaluation_id": str(retry_evidence["evaluation_id"]),
            "invocation_id": str(retry_evidence["invocation_id"]),
        }


def test_invalid_stale_callback_is_noop_after_locked_application_left_new(tmp_path):
    app = make_app(tmp_path)
    case = seed_routing_case(app, suffix="invalid-stale")
    with app.state.identity_store.sync_session() as db:
        first = route_llm_screening_terminal(
            db,
            organization_id=case.organization_id,
            item_id=case.item_id,
            actor_user_id=case.creator_id,
            score=60,
            ai_status="succeeded",
            safe_error_code=None,
            trace_id="trace-first",
            **evidence_ids(),
        )
        stale = route_llm_screening_terminal(
            db,
            organization_id=case.organization_id,
            item_id=case.item_id,
            actor_user_id=case.creator_id,
            score=999,
            ai_status="not_a_terminal_status",
            safe_error_code="candidate_alice_resume_prompt",
            trace_id="trace-stale-invalid",
        )
        assert stale.routed is False
        assert stale.stage == first.stage == "review"
        assert stale.score == first.score == 60
        assert stale.recommendation == first.recommendation == "建议评审"
        assert db.scalar(select(func.count(ApplicationStageEvent.id))) == 1
        assert db.scalar(select(func.count(ApplicationReviewTask.id))) == 1
        assert db.scalar(
            select(func.count(AuditLog.id)).where(
                AuditLog.event_type == "screening.terminal_routed"
            )
        ) == 1


def test_router_is_tenant_scoped_and_fails_closed(tmp_path):
    app = make_app(tmp_path)
    case = seed_routing_case(app, suffix="tenant")
    other_user_id = seed_user(app, "recruiter", "other-tenant@example.test")
    with app.state.identity_store.sync_session() as db:
        other_user = db.get(User, other_user_id)
        other = Organization(slug="other", name="Other", status="active")
        db.add(other)
        db.flush()
        other_user.organization_id = other.id
        db.commit()
        other_id = other.id

    with app.state.identity_store.sync_session() as db:
        with pytest.raises(ScreeningRoutingConflict, match="screening_item_unavailable"):
            route_llm_screening_terminal(
                db,
                organization_id=other_id,
                item_id=case.item_id,
                actor_user_id=other_user_id,
                score=85,
                ai_status="succeeded",
                safe_error_code=None,
                trace_id="trace-other",
            )
        assert db.get(Application, case.application_id).stage == "new"
        assert db.scalar(select(func.count(ApplicationStageEvent.id))) == 0
