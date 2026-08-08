from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from server.app.identity.models import Base, Job, Organization, User, UserRole, UserStatus
from server.app.recruiting.schemas import JobDefinitionCommand
from server.app.recruiting.service import InvalidAggregateRelationship, create_job_definition_record, replace_job_definition_record
from server.app.recruiting.models import Application, Candidate, Resume
from server.app.offers.models import Offer, OfferApproval, OfferEvent, OfferResponse, OfferTemplate, OfferVersion, OrganizationSpecialOfferApprover
from server.app.offers.schemas import OfferCommand, OfferVersionCommand
from server.app.offers.service import (
    OfferApprovalError,
    OfferNotFound,
    OfferVersionConflict,
    create_offer,
    decide_approval,
    expire_due_offers,
    submit_offer,
    update_offer_version,
    withdraw_offer,
)


ORG = UUID(int=1)
OTHER_ORG = UUID(int=2)
OWNER = UUID(int=3)
DEFAULT_APPROVER = UUID(int=4)
SPECIAL_APPROVER = UUID(int=5)
SECOND_SPECIAL_APPROVER = UUID(int=6)
APPLICATION = UUID(int=7)
JOB = UUID(int=8)


def make_session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return Session(engine)


def seed_application(db, *, organization_id=ORG, approver_id=DEFAULT_APPROVER, template_id=None):
    organization = Organization(id=organization_id, slug=str(organization_id), name="Tenant")
    users = [
        User(id=OWNER, organization_id=organization_id, email="owner@example.test", normalized_email="owner@example.test", display_name="Owner", password_hash="x"),
        User(id=DEFAULT_APPROVER, organization_id=organization_id, email="approver@example.test", normalized_email="approver@example.test", display_name="Approver", password_hash="x"),
        User(id=SPECIAL_APPROVER, organization_id=organization_id, email="special@example.test", normalized_email="special@example.test", display_name="Special", password_hash="x"),
        User(id=SECOND_SPECIAL_APPROVER, organization_id=organization_id, email="second@example.test", normalized_email="second@example.test", display_name="Second", password_hash="x"),
    ]
    users[0].roles.append(UserRole(role="recruiting_admin"))
    for user in users[1:]:
        user.roles.append(UserRole(role="hiring_manager"))
    job = Job(id=JOB, organization_id=organization_id, title="Role", owner_id=OWNER, status="open", offer_approver_id=approver_id, offer_template_id=template_id)
    candidate = Candidate(id=UUID(int=10), organization_id=organization_id, display_name="Candidate")
    resume = Resume(id=UUID(int=11), organization_id=organization_id, candidate_id=candidate.id, file_object_id=UUID(int=12), version_number=1)
    application = Application(id=APPLICATION, organization_id=organization_id, candidate_id=candidate.id, job_id=job.id, resume_id=resume.id, owner_id=OWNER, stage="passed", source="manual")
    db.add_all([organization, *users, job, candidate, resume, application])
    db.commit()
    return application


def seed_additional_application(db, application_int):
    candidate = Candidate(id=UUID(int=application_int + 1), organization_id=ORG, display_name=f"Candidate {application_int}")
    resume = Resume(id=UUID(int=application_int + 2), organization_id=ORG, candidate_id=candidate.id, file_object_id=UUID(int=application_int + 3), version_number=1)
    application = Application(id=UUID(int=application_int), organization_id=ORG, candidate_id=candidate.id, job_id=JOB, resume_id=resume.id, owner_id=OWNER, stage="passed", source="manual")
    db.add_all([candidate, resume, application])
    db.flush()
    return application


def command(**overrides):
    values = {
        "application_id": APPLICATION,
        "candidate_response_deadline": datetime(2026, 8, 20, tzinfo=timezone.utc),
        "content": {"salary": "100"},
        "is_special": False,
        "special_reason": None,
    }
    values.update(overrides)
    return OfferCommand(**values)


def test_draft_edits_create_one_current_version_and_submission_snapshots_it_immutably():
    with make_session() as db:
        seed_application(db)
        offer = create_offer(db, ORG, OWNER, command(), trace_id="trace")
        edited = update_offer_version(db, ORG, offer.id, OWNER, OfferVersionCommand(content={"salary": "120"}), expected_version=1, trace_id="trace")
        submit_offer(db, ORG, offer.id, OWNER, expected_version=2, trace_id="trace")
        db.commit()

        versions = list(db.scalars(select(OfferVersion).where(OfferVersion.offer_id == offer.id).order_by(OfferVersion.version_number)))
        assert [(version.version_number, version.content) for version in versions] == [(1, {"salary": "100"}), (2, {"salary": "120"})]
        assert offer.current_version_id == versions[1].id
        assert edited.version == 3
        approval = db.scalar(select(OfferApproval).where(OfferApproval.offer_id == offer.id))
        assert approval.round_number == 1 and approval.version_number == 2 and approval.assignee_id == DEFAULT_APPROVER
        with pytest.raises(OfferVersionConflict):
            update_offer_version(db, ORG, offer.id, OWNER, OfferVersionCommand(content={"salary": "130"}), expected_version=3, trace_id="trace")


def test_offer_creation_requires_passed_application_and_one_active_workflow():
    with make_session() as db:
        application = seed_application(db)
        application.stage = "review"
        db.flush()
        with pytest.raises(OfferApprovalError, match="passed"):
            create_offer(db, ORG, OWNER, command(), trace_id="trace")

        application.stage = "passed"
        first = create_offer(db, ORG, OWNER, command(), trace_id="trace")
        with pytest.raises(OfferApprovalError, match="active workflow"):
            create_offer(db, ORG, OWNER, command(), trace_id="trace")
        withdraw_offer(db, ORG, first.id, OWNER, expected_version=1, trace_id="trace")
        assert create_offer(db, ORG, OWNER, command(), trace_id="trace").status == "draft"


def test_special_approval_chain_appends_ordered_org_approvers_and_deduplicates_first_occurrence():
    with make_session() as db:
        seed_application(db)
        db.add_all([
            OrganizationSpecialOfferApprover(organization_id=ORG, approver_id=DEFAULT_APPROVER, position=1),
            OrganizationSpecialOfferApprover(organization_id=ORG, approver_id=SPECIAL_APPROVER, position=2),
            OrganizationSpecialOfferApprover(organization_id=ORG, approver_id=SECOND_SPECIAL_APPROVER, position=3),
        ])
        offer = create_offer(db, ORG, OWNER, command(is_special=True, special_reason="Compensation exception"), trace_id="trace")
        submit_offer(db, ORG, offer.id, OWNER, expected_version=1, trace_id="trace")
        db.commit()
        approvals = list(db.scalars(select(OfferApproval).where(OfferApproval.offer_id == offer.id).order_by(OfferApproval.sequence)))
        assert [(approval.sequence, approval.assignee_id, approval.status) for approval in approvals] == [
            (1, DEFAULT_APPROVER, "pending"),
            (2, SPECIAL_APPROVER, "waiting"),
            (3, SECOND_SPECIAL_APPROVER, "waiting"),
        ]


def test_special_submission_requires_an_eligible_special_approver_after_default_dedup():
    with make_session() as db:
        seed_application(db)
        offer = create_offer(db, ORG, OWNER, command(is_special=True, special_reason="Exception"), trace_id="trace")
        with pytest.raises(OfferApprovalError, match="special approver"):
            submit_offer(db, ORG, offer.id, OWNER, expected_version=1, trace_id="trace")

        db.add(OrganizationSpecialOfferApprover(organization_id=ORG, approver_id=DEFAULT_APPROVER, position=1))
        db.flush()
        with pytest.raises(OfferApprovalError, match="special approver"):
            submit_offer(db, ORG, offer.id, OWNER, expected_version=1, trace_id="trace")


def test_submission_revalidates_active_eligible_default_special_approvers_and_template():
    with make_session() as db:
        seed_application(db)
        template = OfferTemplate(organization_id=ORG, name="Standard", content={})
        db.add(template)
        db.flush()
        offer = create_offer(db, ORG, OWNER, command(template_id=template.id), trace_id="trace")
        db.get(User, DEFAULT_APPROVER).status = UserStatus.DISABLED
        with pytest.raises(OfferApprovalError, match="default approver"):
            submit_offer(db, ORG, offer.id, OWNER, expected_version=1, trace_id="trace")

        db.get(User, DEFAULT_APPROVER).status = UserStatus.ACTIVE
        template.status = "inactive"
        with pytest.raises(OfferApprovalError, match="template"):
            submit_offer(db, ORG, offer.id, OWNER, expected_version=1, trace_id="trace")

        template.status = "active"
        withdraw_offer(db, ORG, offer.id, OWNER, expected_version=1, trace_id="trace")
        special = create_offer(db, ORG, OWNER, command(is_special=True, special_reason="Exception"), trace_id="trace")
        db.add(OrganizationSpecialOfferApprover(organization_id=ORG, approver_id=SPECIAL_APPROVER, position=1))
        db.get(User, SPECIAL_APPROVER).status = UserStatus.DISABLED
        db.flush()
        with pytest.raises(OfferApprovalError, match="special approver"):
            submit_offer(db, ORG, special.id, OWNER, expected_version=1, trace_id="trace")


def test_sequential_approval_rejection_requires_reason_and_changes_requested_without_application_stage_change():
    with make_session() as db:
        application = seed_application(db)
        offer = create_offer(db, ORG, OWNER, command(), trace_id="trace")
        submit_offer(db, ORG, offer.id, OWNER, expected_version=1, trace_id="trace")
        with pytest.raises(OfferApprovalError):
            decide_approval(db, ORG, offer.id, DEFAULT_APPROVER, "rejected", expected_version=2, reason="   ", trace_id="trace")
        rejected = decide_approval(db, ORG, offer.id, DEFAULT_APPROVER, "rejected", expected_version=2, reason="Need revised compensation", trace_id="trace")
        assert rejected.status == "changes_requested"
        update_offer_version(db, ORG, offer.id, OWNER, OfferVersionCommand(content={"salary": "125"}), expected_version=3, trace_id="trace")
        submit_offer(db, ORG, offer.id, OWNER, expected_version=4, trace_id="trace")
        db.commit()
        assert db.get(Application, application.id).stage == "passed"
        assert db.scalar(select(OfferEvent).where(OfferEvent.offer_id == offer.id, OfferEvent.event_type == "offer.approval_rejected")).payload["reason"] == "Need revised compensation"
        assert [(item.round_number, item.version_number) for item in db.scalars(select(OfferApproval).where(OfferApproval.offer_id == offer.id).order_by(OfferApproval.round_number))] == [(1, 1), (2, 2)]


def test_changes_requested_cannot_resubmit_the_rejected_version():
    with make_session() as db:
        seed_application(db)
        offer = create_offer(db, ORG, OWNER, command(), trace_id="trace")
        submit_offer(db, ORG, offer.id, OWNER, expected_version=1, trace_id="trace")
        decide_approval(db, ORG, offer.id, DEFAULT_APPROVER, "rejected", expected_version=2, reason="Revise", trace_id="trace")
        with pytest.raises(OfferApprovalError, match="new version"):
            submit_offer(db, ORG, offer.id, OWNER, expected_version=3, trace_id="trace")


def test_approval_completion_requires_each_assignee_in_order_and_only_moves_to_ready_to_send():
    with make_session() as db:
        seed_application(db)
        db.add(OrganizationSpecialOfferApprover(organization_id=ORG, approver_id=SPECIAL_APPROVER, position=1))
        offer = create_offer(db, ORG, OWNER, command(is_special=True, special_reason="Exception"), trace_id="trace")
        submit_offer(db, ORG, offer.id, OWNER, expected_version=1, trace_id="trace")
        with pytest.raises(OfferApprovalError):
            decide_approval(db, ORG, offer.id, SPECIAL_APPROVER, "approved", expected_version=2, trace_id="trace")
        assert decide_approval(db, ORG, offer.id, DEFAULT_APPROVER, "approved", expected_version=2, trace_id="trace").status == "pending_approval"
        assert decide_approval(db, ORG, offer.id, SPECIAL_APPROVER, "approved", expected_version=3, trace_id="trace").status == "ready_to_send"


def test_submission_blocks_missing_job_default_approver_and_stale_offer_versions():
    with make_session() as db:
        seed_application(db, approver_id=None)
        offer = create_offer(db, ORG, OWNER, command(), trace_id="trace")
        with pytest.raises(OfferApprovalError, match="default approver"):
            submit_offer(db, ORG, offer.id, OWNER, expected_version=1, trace_id="trace")
        with pytest.raises(OfferVersionConflict):
            submit_offer(db, ORG, offer.id, OWNER, expected_version=0, trace_id="trace")


def test_withdrawal_leaves_application_stage_unchanged_and_expiry_only_transitions_due_sent_unanswered_offers():
    with make_session() as db:
        application = seed_application(db)
        withdrawn = create_offer(db, ORG, OWNER, command(), trace_id="trace")
        withdraw_offer(db, ORG, withdrawn.id, OWNER, expected_version=1, trace_id="trace")
        due = create_offer(db, ORG, OWNER, command(candidate_response_deadline=datetime(2026, 8, 1, tzinfo=timezone.utc)), trace_id="trace")
        future_application = seed_additional_application(db, 50)
        future = create_offer(db, ORG, OWNER, command(application_id=future_application.id, candidate_response_deadline=datetime(2026, 9, 1, tzinfo=timezone.utc)), trace_id="trace")
        due.status = future.status = "sent"
        db.commit()
        assert expire_due_offers(db, now=datetime(2026, 8, 8, tzinfo=timezone.utc), trace_id="sweep") == 1
        db.commit()
        assert db.get(Offer, withdrawn.id).status == "withdrawn"
        assert db.get(Offer, due.id).status == "expired"
        assert db.get(Offer, future.id).status == "sent"
        assert db.get(Application, application.id).stage == "passed"


def test_cross_tenant_operations_are_rejected():
    with make_session() as db:
        seed_application(db)
        offer = create_offer(db, ORG, OWNER, command(), trace_id="trace")
        with pytest.raises(OfferNotFound):
            submit_offer(db, OTHER_ORG, offer.id, OWNER, expected_version=1, trace_id="trace")
        withdraw_offer(db, ORG, offer.id, OWNER, expected_version=1, trace_id="trace")
        db.add(OfferTemplate(id=UUID(int=30), organization_id=OTHER_ORG, name="Other", content={}))
        with pytest.raises(OfferNotFound):
            create_offer(db, ORG, OWNER, command(template_id=UUID(int=30)), trace_id="trace")


def test_offer_current_version_is_a_required_pointer_and_approval_snapshot_is_offer_bound():
    offer_constraints = [constraint for constraint in Offer.__table__.foreign_key_constraints if "current_version_id" in constraint.column_keys]
    approval_constraints = [constraint for constraint in OfferApproval.__table__.foreign_key_constraints if "offer_version_id" in constraint.column_keys]
    assert Offer.__table__.c.current_version_id.nullable is False
    assert any(tuple(constraint.column_keys) == ("organization_id", "current_version_id", "id") for constraint in offer_constraints)
    assert any(tuple(constraint.column_keys) == ("organization_id", "offer_id", "offer_version_id", "version_number") for constraint in approval_constraints)
    assert "is_current" not in OfferVersion.__table__.c


def test_submission_copies_template_deadline_and_special_metadata_into_immutable_version_snapshot():
    with make_session() as db:
        template = OfferTemplate(organization_id=ORG, name="Standard", content={"body": "v1"})
        db.add(template)
        db.flush()
        seed_application(db, template_id=template.id)
        db.add(OrganizationSpecialOfferApprover(organization_id=ORG, approver_id=SPECIAL_APPROVER, position=1))
        deadline = datetime(2026, 8, 20, tzinfo=timezone.utc)
        offer = create_offer(db, ORG, OWNER, command(candidate_response_deadline=deadline, is_special=True, special_reason="Exception"), trace_id="trace")
        submit_offer(db, ORG, offer.id, OWNER, expected_version=1, trace_id="trace")
        version = db.get(OfferVersion, offer.current_version_id)
        assert (offer.template_id, version.template_id, version.candidate_response_deadline.replace(tzinfo=timezone.utc), version.is_special, version.special_reason) == (template.id, template.id, deadline, True, "Exception")
        with pytest.raises(OfferVersionConflict):
            update_offer_version(db, ORG, offer.id, OWNER, OfferVersionCommand(content={"salary": "101"}), expected_version=2, trace_id="trace")


@pytest.mark.parametrize("source_status", ["draft", "changes_requested", "ready_to_send", "sent"])
def test_revision_versions_every_offer_snapshot_field_and_resets_approval(source_status):
    with make_session() as db:
        seed_application(db)
        old_template = OfferTemplate(organization_id=ORG, name="Old", content={})
        new_template = OfferTemplate(organization_id=ORG, name="New", content={})
        db.add_all([old_template, new_template])
        db.flush()
        offer = create_offer(db, ORG, OWNER, command(template_id=old_template.id), trace_id="trace")
        old_version = db.get(OfferVersion, offer.current_version_id)
        if source_status != "draft":
            submit_offer(db, ORG, offer.id, OWNER, expected_version=1, trace_id="trace")
            if source_status == "changes_requested":
                decide_approval(db, ORG, offer.id, DEFAULT_APPROVER, "rejected", expected_version=2, reason="Revise", trace_id="trace")
            elif source_status in {"ready_to_send", "sent"}:
                decide_approval(db, ORG, offer.id, DEFAULT_APPROVER, "approved", expected_version=2, trace_id="trace")
                if source_status == "sent":
                    offer.status = "sent"
        expected_version = offer.version
        deadline = datetime(2026, 9, 1, tzinfo=timezone.utc)
        revised = update_offer_version(
            db, ORG, offer.id, OWNER,
            OfferVersionCommand(
                content={"body": "revised", "compensation": {"salary": "130"}},
                candidate_response_deadline=deadline,
                template_id=new_template.id,
                is_special=True,
                special_reason="Executive exception",
            ),
            expected_version=expected_version,
            trace_id="trace",
        )
        current = db.get(OfferVersion, revised.current_version_id)
        assert current.id != old_version.id
        assert (current.content, current.template_id, current.is_special, current.special_reason) == (
            {"body": "revised", "compensation": {"salary": "130"}}, new_template.id, True, "Executive exception"
        )
        assert current.candidate_response_deadline.replace(tzinfo=timezone.utc) == deadline
        assert (revised.template_id, revised.is_special, revised.special_reason, revised.status) == (new_template.id, True, "Executive exception", "draft")
        assert revised.candidate_response_deadline.replace(tzinfo=timezone.utc) == deadline
        assert old_version.content == {"salary": "100"}
        if source_status != "draft":
            assert old_version.submitted_at is not None


def test_revision_preserves_omitted_fields_allows_explicit_template_clear_and_rejects_noop():
    with make_session() as db:
        seed_application(db)
        template = OfferTemplate(organization_id=ORG, name="Standard", content={})
        db.add(template)
        db.flush()
        offer = create_offer(db, ORG, OWNER, command(template_id=template.id), trace_id="trace")
        with pytest.raises(OfferVersionConflict, match="no changes"):
            update_offer_version(db, ORG, offer.id, OWNER, OfferVersionCommand(), expected_version=1, trace_id="trace")
        update_offer_version(db, ORG, offer.id, OWNER, OfferVersionCommand(template_id=None), expected_version=1, trace_id="trace")
        current = db.get(OfferVersion, offer.current_version_id)
        assert current.template_id is None
        assert current.content == {"salary": "100"}


def test_user_transitions_require_current_version_and_expiry_never_repeats_or_expires_answered_offer():
    with make_session() as db:
        seed_application(db)
        offer = create_offer(db, ORG, OWNER, command(candidate_response_deadline=datetime(2026, 8, 1, tzinfo=timezone.utc)), trace_id="trace")
        offer.status = "sent"
        db.flush()
        assert db.scalar(select(OfferResponse.id).where(OfferResponse.offer_id == offer.id)) is None
        db.add(OfferResponse(organization_id=ORG, offer_id=offer.id, status="accepted", responded_at=datetime.now(timezone.utc)))
        db.flush()
        assert expire_due_offers(db, now=datetime(2026, 8, 8, tzinfo=timezone.utc), trace_id="sweep") == 0
        with pytest.raises(TypeError):
            withdraw_offer(db, ORG, offer.id, OWNER, trace_id="trace")
        with pytest.raises(OfferVersionConflict):
            withdraw_offer(db, ORG, offer.id, OWNER, expected_version=0, trace_id="trace")


def test_expiry_locks_transition_state_so_repeated_sweeps_write_one_event():
    with make_session() as db:
        seed_application(db)
        offer = create_offer(db, ORG, OWNER, command(candidate_response_deadline=datetime(2026, 8, 1, tzinfo=timezone.utc)), trace_id="trace")
        offer.status = "sent"
        assert expire_due_offers(db, now=datetime(2026, 8, 8, tzinfo=timezone.utc), trace_id="sweep") == 1
        assert expire_due_offers(db, now=datetime(2026, 8, 8, tzinfo=timezone.utc), trace_id="sweep") == 0
        assert db.scalar(select(func.count()).select_from(OfferEvent).where(OfferEvent.offer_id == offer.id, OfferEvent.event_type == "offer.expired")) == 1


def test_rejection_reason_is_constrained_and_excluded_from_global_audit_metadata():
    with make_session() as db:
        seed_application(db)
        offer = create_offer(db, ORG, OWNER, command(), trace_id="trace")
        submit_offer(db, ORG, offer.id, OWNER, expected_version=1, trace_id="trace")
        decide_approval(db, ORG, offer.id, DEFAULT_APPROVER, "rejected", expected_version=2, reason="Compensation requires revision", trace_id="trace")
        event = db.scalar(select(OfferEvent).where(OfferEvent.offer_id == offer.id, OfferEvent.event_type == "offer.approval_rejected"))
        from server.app.identity.models import AuditLog
        audit = db.scalar(select(AuditLog).where(AuditLog.resource_id == offer.id, AuditLog.event_type == "offer.approval_rejected"))
        checks = {constraint.name for constraint in OfferApproval.__table__.constraints if hasattr(constraint, "sqltext")}
        assert event.payload["reason"] == "Compensation requires revision"
        assert "reason" not in audit.metadata_json
        assert "ck_offer_approvals_rejection_reason" in checks


def test_offer_migration_uses_deferred_exact_current_pointer_and_returns_new_from_trigger():
    migration = Path("server/migrations/versions/0033_offer_workflow.py").read_text(encoding="utf-8")
    assert '"fk_offers_current_version"' in migration
    assert 'deferrable=True, initially="DEFERRED"' in migration
    assert "RETURN NEW;" in migration
    assert "uq_offer_versions_current" not in migration


def test_offer_models_bind_application_job_and_final_response_contract():
    application_uniques = {
        tuple(column.name for column in constraint.columns)
        for constraint in Application.__table__.constraints
        if constraint.__class__.__name__ == "UniqueConstraint"
    }
    offer_foreign_keys = {tuple(constraint.column_keys) for constraint in Offer.__table__.foreign_key_constraints}
    response_checks = {constraint.name for constraint in OfferResponse.__table__.constraints if hasattr(constraint, "sqltext")}
    assert ("organization_id", "id", "job_id") in application_uniques
    assert ("organization_id", "application_id", "job_id") in offer_foreign_keys
    assert "ck_offer_responses_status" in response_checks
    assert "pending" not in str(next(constraint.sqltext for constraint in OfferResponse.__table__.constraints if getattr(constraint, "name", None) == "ck_offer_responses_status"))


def test_job_definition_persists_tenant_scoped_offer_defaults_and_rejects_cross_tenant_references():
    with make_session() as db:
        seed_application(db)
        organization = Organization(id=OTHER_ORG, slug="other", name="Other")
        other_user = User(id=UUID(int=20), organization_id=OTHER_ORG, email="other@example.test", normalized_email="other@example.test", display_name="Other", password_hash="x")
        other_template = OfferTemplate(id=UUID(int=21), organization_id=OTHER_ORG, name="Other template", content={})
        db.add_all([organization, other_user, other_template])
        db.commit()
        payload = {
            "title": "New role", "department_id": None, "headcount": 1, "priority": "normal", "recruiting_owner_id": None,
            "hiring_owner_id": OWNER, "hiring_manager_ids": [OWNER], "description": "Description", "location": "", "process_template": "standard",
            "workflow_template_id": None, "llm_enabled": False, "must_have": [], "nice_to_have": [], "publish": False,
            "offer_approver_id": DEFAULT_APPROVER, "offer_template_id": None,
        }
        command = JobDefinitionCommand(**payload).model_dump()
        job, _, _ = create_job_definition_record(db, ORG, OWNER, command, trace_id="trace")
        assert job.offer_approver_id == DEFAULT_APPROVER
        local_template = OfferTemplate(organization_id=ORG, name="Local template", content={})
        db.add(local_template)
        db.flush()
        payload["offer_template_id"] = local_template.id
        replaced, _, _ = replace_job_definition_record(db, ORG, job.id, OWNER, JobDefinitionCommand(**payload).model_dump(), expected_version=1, trace_id="trace")
        assert replaced.offer_template_id == local_template.id
        payload["offer_template_id"] = other_template.id
        with pytest.raises(InvalidAggregateRelationship):
            create_job_definition_record(db, ORG, OWNER, JobDefinitionCommand(**payload).model_dump(), trace_id="trace")
        payload["offer_template_id"] = None
        payload["offer_approver_id"] = other_user.id
        with pytest.raises(InvalidAggregateRelationship):
            create_job_definition_record(db, ORG, OWNER, JobDefinitionCommand(**payload).model_dump(), trace_id="trace")


def test_job_definition_rejects_inactive_roleless_approvers_and_inactive_templates():
    with make_session() as db:
        seed_application(db)
        roleless = User(
            id=UUID(int=40), organization_id=ORG, email="roleless@example.test",
            normalized_email="roleless@example.test", display_name="Roleless", password_hash="x",
        )
        inactive = OfferTemplate(organization_id=ORG, name="Inactive", content={}, status="inactive")
        db.add_all([roleless, inactive])
        db.flush()
        payload = {
            "title": "New role", "department_id": None, "headcount": 1, "priority": "normal", "recruiting_owner_id": None,
            "hiring_owner_id": OWNER, "hiring_manager_ids": [OWNER], "description": "Description", "location": "", "process_template": "standard",
            "workflow_template_id": None, "llm_enabled": False, "must_have": [], "nice_to_have": [], "publish": False,
            "offer_approver_id": roleless.id, "offer_template_id": None,
        }
        with pytest.raises(InvalidAggregateRelationship):
            create_job_definition_record(db, ORG, OWNER, JobDefinitionCommand(**payload).model_dump(), trace_id="trace")
        roleless.roles.append(UserRole(role="hiring_manager"))
        roleless.status = UserStatus.INVITED
        with pytest.raises(InvalidAggregateRelationship):
            create_job_definition_record(db, ORG, OWNER, JobDefinitionCommand(**payload).model_dump(), trace_id="trace")
        payload["offer_approver_id"] = DEFAULT_APPROVER
        payload["offer_template_id"] = inactive.id
        with pytest.raises(InvalidAggregateRelationship):
            create_job_definition_record(db, ORG, OWNER, JobDefinitionCommand(**payload).model_dump(), trace_id="trace")
