import json
from datetime import date, datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from server.app.identity.models import Base, Job, JobCollaborator, Organization, User, UserRole, UserStatus
from server.app.recruiting.models import Application, ApplicationReviewTask, Candidate, CandidateNote
from server.app.offers.models import Offer, OfferApproval, OfferVersion, OrganizationSpecialOfferApprover
from server.app.onboarding.models import OnboardingRecord
from server.app.interviews.models import Interview, InterviewFeedback, InterviewParticipant
from server.app.integrations.feishu.models import FeishuInterviewSync, FeishuOnboardingConfig
from server.app.talent.models import TalentPool, TalentPoolGrant, TalentPoolMembership
from server.app.screening.models import ScreeningRun
from server.app.identity.offboarding_models import MemberOffboarding
from server.app.identity.offboarding_handover import HandoverPlanChanged, apply_handover, build_handover_plan


NOW = datetime(2026, 10, 26, tzinfo=timezone.utc)


@pytest.fixture
def world():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        org = Organization(id=uuid4(), name="Example", slug=str(uuid4()))
        source, successor = [User(id=uuid4(), organization_id=org.id, email=f"{name}@example.test",
                                  normalized_email=f"{name}@example.test", display_name=name,
                                  password_hash="not-a-real-password") for name in ("source", "successor")]
        for user in (source, successor):
            user.roles = [UserRole(role="recruiting_admin"), UserRole(role="hiring_manager")]
        job = Job(id=uuid4(), organization_id=org.id, title="Engineer", owner_id=source.id,
                  hiring_owner_id=source.id, offer_approver_id=source.id, status="open")
        candidate = Candidate(id=uuid4(), organization_id=org.id, display_name="Example", owner_id=source.id)
        app = Application(id=uuid4(), organization_id=org.id, candidate_id=candidate.id,
                          job_id=job.id, resume_id=uuid4(), owner_id=source.id, stage="review")
        db.add_all([org, source, successor, job, candidate, app])
        db.commit()
        yield db, org, source, successor, job, candidate, app
    engine.dispose()


def preview(w, **kw):
    db, org, source, successor, *_ = w
    return build_handover_plan(db, org.id, source, successor, now=NOW, **kw)


def execute(w, plan):
    return apply_handover(w[0], plan, actor_user_id=w[3].id, trace_id="handover-test", now=NOW)


def test_preview_is_read_only_and_transfers_only_operational_fields(world):
    db, org, source, successor, job, candidate, app = world
    note = CandidateNote(organization_id=org.id, candidate_id=candidate.id, actor_user_id=source.id,
                         event_type="note", payload={"text": "original"})
    db.add(note)
    db.commit()
    plan = preview(world)
    assert not db.dirty and not db.new
    assert job.owner_id == source.id
    assert plan["fingerprint"] == preview(world)["fingerprint"]
    assert all(set(("category", "resource_type", "resource_id", "title", "status", "reason_code", "reason")) <= item.keys() for item in plan["items"])
    assert sum(c["total"] for c in plan["counts"]) == len(plan["items"])
    json.dumps({"items": plan["items"], "counts": plan["counts"], "fingerprint": plan["fingerprint"]})
    assert execute(world, plan) >= 5
    assert (job.owner_id, job.hiring_owner_id, job.offer_approver_id, candidate.owner_id, app.owner_id) == (successor.id,) * 5
    assert note.actor_user_id == source.id
    assert execute(world, plan) == 0
    assert execute(world, preview(world)) == 0
    db.rollback()
    assert job.owner_id == source.id  # The domain helper never commits.


@pytest.mark.parametrize("field,value", [("stage", "rejected"), ("version", 9)])
def test_stale_plan_does_not_overwrite_changed_state(world, field, value):
    plan = preview(world)
    setattr(world[-1], field, value)
    world[0].flush()
    with pytest.raises(HandoverPlanChanged):
        execute(world, plan)
    assert world[4].owner_id == world[2].id


def test_partial_plan_does_not_copy_roles_or_scope_and_retry_is_deterministic(world):
    db, org, source, successor, job, candidate, app = world
    successor.roles = [UserRole(role="recruiter")]
    db.commit()
    plan = preview(world)
    assert any(i["status"] == "blocked" for i in plan["items"])
    assert any(i["resource_type"] == "job_collaborator" and i["status"] == "transferable" for i in plan["items"])
    execute(world, plan)
    assert job.owner_id == successor.id and app.owner_id == successor.id
    assert job.hiring_owner_id == source.id and job.offer_approver_id == source.id
    assert [r.role for r in successor.roles] == ["recruiter"]
    assert successor.recruiting_scope_type == "jobs"
    assert execute(world, preview(world)) == 0
    assert len(list(db.scalars(select(JobCollaborator).where(JobCollaborator.user_id == successor.id)))) == 1


def onboarding(w, status="ready"):
    db, org, source, successor, job, candidate, app = w
    row = OnboardingRecord(organization_id=org.id, offer_response_id=uuid4(), offer_id=uuid4(),
                           application_id=app.id, candidate_id=candidate.id, job_id=job.id,
                           department_id=uuid4(), job_title=job.title, department_name="Example",
                           expected_start_date=date(2026, 11, 1), pii_ciphertext=b"opaque", status=status)
    if status != "ready":
        row.generation, row.started_by, row.started_at = uuid4(), source.id, NOW
    if status == "submitted":
        row.submitted_at, row.feishu_instance_code = NOW, "external-instance"
    db.add(row)
    return row


@pytest.mark.parametrize("status", ["ready", "submitting", "failed", "submitted"])
def test_hired_unfinished_onboarding_and_external_identity(world, status):
    world[-1].stage = "hired"
    row = onboarding(world, status)
    world[0].commit()
    original = (row.started_by, row.generation, row.feishu_instance_code)
    plan = preview(world)
    items = [i for i in plan["items"] if i["resource_type"] == "onboarding"]
    assert len(items) == 1
    assert items[0]["status"] == ("transferable" if status == "ready" else "blocked")
    execute(world, plan)
    assert (row.started_by, row.generation, row.feishu_instance_code) == original
    assert world[-1].owner_id == (world[2].id if status == "submitted" else world[3].id)


def test_terminal_applications_and_closed_jobs_stay_historical(world):
    world[-1].stage = "rejected"
    world[4].status = "closed"
    world[0].commit()
    execute(world, preview(world))
    assert world[-1].owner_id == world[2].id
    assert world[4].owner_id == world[2].id


def test_open_review_only_and_scope_eligibility(world):
    db, org, source, successor, job, candidate, app = world
    task = ApplicationReviewTask(organization_id=org.id, application_id=app.id,
                                 assignee_id=source.id, status="open", ai_status="succeeded")
    old = ApplicationReviewTask(organization_id=org.id, application_id=app.id,
                                assignee_id=source.id, status="closed", ai_status="succeeded", closed_at=NOW)
    db.add_all([task, old])
    db.commit()
    execute(world, preview(world))
    assert task.assignee_id == successor.id and old.assignee_id == source.id
    assert old.closed_at.replace(tzinfo=timezone.utc) == NOW


def test_talent_preserves_contact_restrictions_and_history(world):
    db, org, source, successor, job, candidate, app = world
    pool = TalentPool(id=uuid4(), organization_id=org.id, name="Pool", purpose="Follow up", owner_id=source.id, visibility="private")
    member = TalentPoolMembership(organization_id=org.id, pool_id=pool.id, candidate_id=candidate.id,
                                  source_application_id=app.id, owner_id=source.id, reason="original",
                                  retention_until=NOW + timedelta(days=90), status="do_not_contact")
    db.add_all([pool, member])
    db.commit()
    execute(world, preview(world))
    assert pool.owner_id == successor.id and member.owner_id == successor.id
    assert member.status == "do_not_contact" and member.reason == "original" and member.source_application_id == app.id


def test_interview_coordination_moves_but_participant_feedback_and_calendar_do_not(world):
    db, org, source, successor, job, candidate, app = world
    row = Interview(id=uuid4(), organization_id=org.id, application_id=app.id, round_name="First",
                    method="phone", timezone="UTC", starts_at=NOW + timedelta(days=1), ends_at=NOW + timedelta(days=1, hours=1),
                    owner_id=source.id, created_by=source.id, status="scheduled")
    participant = InterviewParticipant(organization_id=org.id, interview_id=row.id, user_id=source.id)
    sync = FeishuInterviewSync(organization_id=org.id, interview_id=row.id, external_event_id="remote", sync_status="synced")
    db.add_all([row, participant, sync])
    db.commit()
    plan = preview(world)
    assert {i["reason_code"] for i in plan["items"] if i["status"] == "blocked"} >= {"interview_participant_manual", "external_calendar_manual"}
    execute(world, plan)
    assert row.owner_id == successor.id and row.created_by == source.id
    assert participant.user_id == source.id and sync.external_event_id == "remote"
    row.status, row.starts_at, row.ends_at = "pending_feedback", NOW - timedelta(hours=2), NOW - timedelta(hours=1)
    db.commit()
    assert any(i["reason_code"] == "interview_feedback_manual" for i in preview(world)["items"])


def test_feishu_fixed_approver_configuration_remains_blocked_and_validity_unchanged(world):
    db, org, source, successor, *_ = world
    config = FeishuOnboardingConfig(organization_id=org.id, approval_code="example", node_approvers=[{"node_id": "n", "user_ids": [str(source.id)]}],
                                    created_by=source.id, updated_by=source.id, validation_status="valid", enabled=True)
    db.add(config)
    db.commit()
    assert any(i["reason_code"] == "feishu_config_revalidation_required" for i in preview(world)["items"])
    execute(world, preview(world))
    assert config.validation_status == "valid" and config.updated_by == source.id


def test_successor_state_change_invalidates_plan(world):
    plan = preview(world)
    world[3].status = UserStatus.DISABLED
    world[0].flush()
    with pytest.raises(HandoverPlanChanged):
        execute(world, plan)


def test_source_revocation_does_not_invalidate_domain_plan(world):
    plan = preview(world, lock=True)
    world[2].status = UserStatus.DISABLED
    world[2].authorization_version += 1
    world[0].flush()
    assert execute(world, plan) > 0


def test_retrying_same_plan_after_savepoint_rollback_reapplies_work(world):
    db = world[0]
    plan = preview(world)
    savepoint = db.begin_nested()
    assert execute(world, plan) > 0
    savepoint.rollback()
    assert world[4].owner_id == world[2].id
    assert execute(world, plan) > 0
    assert world[4].owner_id == world[3].id


def test_ineligible_and_cross_tenant_successors_never_gain_assignments(world):
    db, org, source, successor, *_ = world
    successor.roles = [UserRole(role="system_admin")]
    db.commit()
    plan = preview(world)
    assert all(i["status"] == "blocked" for i in plan["items"])
    assert execute(world, plan) == 0
    successor.organization_id = uuid4()
    db.flush()
    plan = preview(world)
    assert all(i["reason_code"] == "successor_unavailable" for i in plan["items"])
    assert execute(world, plan) == 0


def test_unrelated_candidate_changes_do_not_stale_plan(world):
    db, org, source, successor, *_ = world
    unrelated = Candidate(organization_id=org.id, display_name="Other", owner_id=successor.id)
    db.add(unrelated)
    db.commit()
    plan = preview(world)
    unrelated.version += 1
    db.flush()
    assert preview(world)["fingerprint"] == plan["fingerprint"]
    assert execute(world, plan) > 0


def test_source_manager_pool_grant_is_explicitly_handed_over(world):
    db, org, source, successor, *_ = world
    successor.roles = [UserRole(role="recruiter")]
    pool = TalentPool(id=uuid4(), organization_id=org.id, name="Granted", purpose="Follow up", owner_id=uuid4(), visibility="granted")
    grant = TalentPoolGrant(organization_id=org.id, pool_id=pool.id, user_id=source.id, access_role="manager")
    db.add_all([pool, grant])
    db.commit()
    plan = preview(world)
    assert any(i["reason_code"] == "pool_grant_added" for i in plan["items"])
    execute(world, plan)
    assert db.scalar(select(TalentPoolGrant).where(TalentPoolGrant.pool_id == pool.id, TalentPoolGrant.user_id == successor.id)).access_role == "manager"
    assert grant.user_id == source.id
    assert execute(world, preview(world)) == 0


@pytest.mark.parametrize("status", ["queued", "failed", "partial"])
def test_background_run_requires_visible_valid_destination_without_changing_creator(world, status):
    db, org, source, successor, job, *_ = world
    run = ScreeningRun(organization_id=org.id, job_id=job.id, jd_version_id=uuid4(), rule_version_id=uuid4(),
                       source="upload", created_by=source.id, status=status, created_at=NOW,
                       started_at=NOW, finished_at=NOW if status != "queued" else None)
    db.add(run)
    db.commit()
    original = (run.id, run.created_by, run.created_at, run.started_at, run.finished_at, run.status, run.version)
    no_successor = build_handover_plan(db, org.id, source, None, now=NOW)
    assert any(i["resource_type"] == "screening_run" and i["status"] == "blocked" for i in no_successor["items"])
    plan = preview(world)
    assert any(i["resource_type"] == "screening_run" and i["status"] == "transferable" for i in plan["items"])
    source.status = UserStatus.DISABLED
    db.add(MemberOffboarding(organization_id=org.id, user_id=source.id, successor_user_id=successor.id,
                            removed_by=successor.id, removed_at=NOW, updated_at=NOW))
    db.flush()
    assert execute(world, plan) > 0
    assert run.created_by == source.id
    assert not any(i["resource_type"] == "screening_run" for i in preview(world)["items"])
    assert (run.id, run.created_by, run.created_at, run.started_at, run.finished_at, run.status, run.version) == original


@pytest.mark.parametrize("status", ["completed", "cancelled"])
def test_terminal_screening_batch_history_is_not_handover_work(world, status):
    db, org, source, successor, job, *_ = world
    db.add(ScreeningRun(organization_id=org.id, job_id=job.id, jd_version_id=uuid4(), rule_version_id=uuid4(),
                        source="upload", created_by=source.id, status=status, finished_at=NOW))
    db.commit()
    assert not any(i["resource_type"] == "screening_run" for i in preview(world)["items"])


@pytest.mark.parametrize("depth", [1, 2])
def test_inherited_upstream_run_is_visible_and_scope_blocked_for_next_successor(world, depth):
    db, org, source, successor, *_ = world
    successor.roles = [UserRole(role="recruiter")]
    ancestors = []
    destination = source.id
    for index in range(depth):
        ancestor = User(id=uuid4(), organization_id=org.id, email=f"ancestor{index}@example.test",
                        normalized_email=f"ancestor{index}@example.test", display_name=f"Ancestor {index}",
                        password_hash="x", status=UserStatus.DISABLED)
        intent = MemberOffboarding(organization_id=org.id, user_id=ancestor.id, successor_user_id=destination,
                                    removed_by=source.id, removed_at=NOW, updated_at=NOW, status="completed")
        db.add_all([ancestor, intent])
        ancestors.append((ancestor, intent))
        destination = ancestor.id
    unrelated_job = Job(id=uuid4(), organization_id=org.id, title="Inherited specialist batch", owner_id=uuid4(), status="open")
    run = ScreeningRun(organization_id=org.id, job_id=unrelated_job.id, jd_version_id=uuid4(), rule_version_id=uuid4(),
                       source="upload", created_by=ancestors[-1][0].id, status="partial", created_at=NOW)
    db.add_all([unrelated_job, run])
    db.commit()
    plan = preview(world)
    inherited = [i for i in plan["items"] if i["resource_type"] == "screening_run" and i["resource_id"] == str(run.id)]
    assert len(inherited) == 1
    assert inherited[0]["status"] == "blocked"
    assert inherited[0]["reason_code"] == "successor_scope_required"
    assert inherited[0]["href"] == f"/screening/tasks/{run.id}"
    assert "Inherited specialist batch" in inherited[0]["title"]
    execute(world, plan)
    assert run.created_by == ancestors[-1][0].id
    assert db.scalar(select(JobCollaborator).where(JobCollaborator.job_id == unrelated_job.id, JobCollaborator.user_id == successor.id)) is None
    # Administrative bookkeeping on an unrelated handover must not stale this plan.
    unrelated = MemberOffboarding(organization_id=org.id, user_id=uuid4(), successor_user_id=uuid4(), removed_by=source.id,
                                   removed_at=NOW, updated_at=NOW, status="pending")
    db.add(unrelated)
    db.flush()
    current = preview(world)
    unrelated.version += 1
    unrelated.updated_at = NOW + timedelta(hours=1)
    db.flush()
    assert preview(world)["fingerprint"] == current["fingerprint"]
    # The edge that makes the unfinished batch inherited is a concurrency fact.
    ancestors[-1][1].successor_user_id = successor.id
    db.flush()
    assert preview(world)["fingerprint"] != current["fingerprint"]
    with pytest.raises(HandoverPlanChanged):
        execute(world, current)


def test_valid_chained_destination_clears_original_and_inherited_run_items(world):
    db, org, source, successor, job, *_ = world
    ancestor = User(id=uuid4(), organization_id=org.id, email="original@example.test", normalized_email="original@example.test",
                    display_name="Original creator", password_hash="x", status=UserStatus.DISABLED)
    source.status = UserStatus.DISABLED
    original_intent = MemberOffboarding(organization_id=org.id, user_id=ancestor.id, successor_user_id=source.id,
                                        removed_by=successor.id, removed_at=NOW, updated_at=NOW, status="completed")
    current_intent = MemberOffboarding(organization_id=org.id, user_id=source.id, successor_user_id=successor.id,
                                       removed_by=successor.id, removed_at=NOW, updated_at=NOW, status="completed")
    run = ScreeningRun(organization_id=org.id, job_id=job.id, jd_version_id=uuid4(), rule_version_id=uuid4(),
                       source="upload", created_by=ancestor.id, status="queued", created_at=NOW)
    db.add_all([ancestor, original_intent, current_intent, run])
    db.commit()
    original = (run.id, run.created_by, run.created_at, run.version)
    assert not any(i["resource_type"] == "screening_run" for i in preview(world)["items"])
    original_preview = build_handover_plan(db, org.id, ancestor, successor, now=NOW)
    assert not any(i["resource_type"] == "screening_run" for i in original_preview["items"])
    # The final destination must still be eligible for the inherited run's job.
    successor.roles = [UserRole(role="recruiter")]
    job.owner_id, job.hiring_owner_id, job.offer_approver_id = uuid4(), None, None
    db.flush()
    for target in (ancestor, source):
        items = build_handover_plan(db, org.id, target, successor, now=NOW)["items"]
        assert any(i["resource_id"] == str(run.id) and i["reason_code"] == "successor_scope_required" for i in items)
    assert (run.id, run.created_by, run.created_at, run.version) == original


def test_only_current_live_approval_round_moves_and_self_duplicate_are_blocked(world):
    db, org, source, successor, job, candidate, app = world
    offer_id, version_id = uuid4(), uuid4()
    offer = Offer(id=offer_id, organization_id=org.id, application_id=app.id, job_id=job.id,
                  current_version_id=version_id, candidate_response_deadline=NOW + timedelta(days=1), status="pending_approval")
    version = OfferVersion(id=version_id, organization_id=org.id, offer_id=offer_id, version_number=1,
                           content={}, candidate_response_deadline=offer.candidate_response_deadline, is_special=False, created_by=source.id)
    def approval(round_number, sequence, status, assignee):
        return OfferApproval(organization_id=org.id, offer_id=offer_id, offer_version_id=version_id,
                             round_number=round_number, version_number=1, sequence=sequence, assignee_id=assignee, status=status)
    old = approval(1, 1, "pending", source.id)
    decided = approval(2, 1, "approved", uuid4())
    live = approval(2, 2, "pending", source.id)
    db.add_all([offer, version, old, decided, live])
    db.commit()
    execute(world, preview(world))
    assert old.assignee_id == source.id and live.assignee_id == successor.id and decided.status == "approved"
    live.assignee_id = source.id
    version.created_by = successor.id
    db.commit()
    assert any(i["reason_code"] == "offer_self_approval" for i in preview(world)["items"])
    version.created_by = source.id
    decided.assignee_id = successor.id
    db.commit()
    assert any(i["reason_code"] == "offer_duplicate_approver" for i in preview(world)["items"])


@pytest.mark.parametrize("future", [False, True])
def test_blocked_items_have_identifiable_context_and_existing_local_routes(world, future):
    db, org, source, successor, job, candidate, app = world
    successor.roles = [UserRole(role="system_admin")]
    record = onboarding(world, "failed")
    pool = TalentPool(id=uuid4(), organization_id=org.id, name="Specialist pool", purpose="Follow up", owner_id=source.id)
    membership = TalentPoolMembership(organization_id=org.id, pool_id=pool.id, candidate_id=candidate.id,
                                      owner_id=source.id, reason="original", retention_until=NOW + timedelta(days=90))
    grant = TalentPoolGrant(organization_id=org.id, pool_id=pool.id, user_id=source.id, access_role="manager")
    interview = Interview(id=uuid4(), organization_id=org.id, application_id=app.id, round_name="Technical round",
                          method="phone", timezone="UTC", starts_at=NOW + timedelta(days=1 if future else -1),
                          ends_at=NOW + timedelta(days=1 if future else -1, hours=1), owner_id=source.id,
                          created_by=source.id, status="scheduled" if future else "pending_feedback")
    participant = InterviewParticipant(organization_id=org.id, interview_id=interview.id, user_id=source.id)
    sync = FeishuInterviewSync(organization_id=org.id, interview_id=interview.id, external_event_id="remote", sync_status="synced")
    config = FeishuOnboardingConfig(organization_id=org.id, approval_code="example-approval", created_by=source.id,
                                    updated_by=source.id, node_approvers=[{"node_id": "n", "user_ids": [str(source.id)]}])
    special = OrganizationSpecialOfferApprover(organization_id=org.id, approver_id=source.id, position=1)
    task = ApplicationReviewTask(organization_id=org.id, application_id=app.id, assignee_id=source.id, ai_status="succeeded")
    offer_id, version_id = uuid4(), uuid4()
    offer = Offer(id=offer_id, organization_id=org.id, application_id=app.id, job_id=job.id,
                  current_version_id=version_id, candidate_response_deadline=NOW + timedelta(days=1), status="pending_approval")
    version = OfferVersion(id=version_id, organization_id=org.id, offer_id=offer_id, version_number=1, content={},
                           candidate_response_deadline=offer.candidate_response_deadline, is_special=False, created_by=source.id)
    approval = OfferApproval(organization_id=org.id, offer_id=offer_id, offer_version_id=version_id,
                             round_number=1, version_number=1, sequence=1, assignee_id=source.id, status="pending")
    run = ScreeningRun(organization_id=org.id, job_id=job.id, jd_version_id=uuid4(), rule_version_id=uuid4(), source="upload", created_by=source.id)
    collaborator = JobCollaborator(organization_id=org.id, job_id=job.id, user_id=source.id, access_role="job_manager")
    db.add_all([pool, membership, grant, interview, participant, sync, config, special, task, offer, version, approval, run, collaborator])
    db.commit()
    plan = preview(world)
    assert {i["category"] for i in plan["items"] if i["status"] == "blocked"} == {
        "jobs", "candidates", "applications", "reviews", "offers", "talent", "interviews", "onboarding", "configuration", "background",
    }
    app_query = f"application={app.id}&job={job.id}"
    candidate_route = f"/candidates/{candidate.id}"
    expected = {
        "job_owner": (f"/jobs/{job.id}", "Engineer"),
        "job_hiring_owner": (f"/jobs/{job.id}", "Engineer"),
        "job_offer_approver": (f"/jobs/{job.id}/edit", "Engineer"),
        "job_collaborator": (f"/jobs/{job.id}", "Engineer"),
        "candidate": (candidate_route, "Example"),
        "application": (f"{candidate_route}?tab=applications&{app_query}", "Example"),
        "review_task": (f"{candidate_route}?tab=applications&{app_query}", "Example"),
        "offer_approval": (f"{candidate_route}?tab=offer&{app_query}&approval={approval.id}", "Example"),
        "onboarding": (f"{candidate_route}?tab=offer&{app_query}", "Example"),
        "talent_pool": (f"/talent/{pool.id}", "Specialist pool"),
        "talent_pool_grant": (f"/talent/{pool.id}", "Specialist pool"),
        "talent_membership": (f"/talent/{pool.id}", "Example"),
        "interview": (f"{candidate_route}?tab=interviews&{app_query}", "Technical round"),
        "interview_calendar": (f"{candidate_route}?tab=interviews&{app_query}", "Technical round"),
        "interview_participant": (f"/interviews/{interview.id}/reschedule", "Technical round"),
        "interview_feedback": (f"{candidate_route}?tab=interviews&{app_query}", "Technical round"),
        "feishu_onboarding_config": ("/settings/feishu", "example-approval"),
        "special_offer_approver": ("/settings/offers", "1"),
        "screening_run": (f"/screening/tasks/{run.id}", "Engineer"),
    }
    for entry in plan["items"]:
        href, context = expected[entry["resource_type"]]
        assert entry.get("href") == href
        assert context in entry["title"]
        assert entry["href"].startswith("/") and not entry["href"].startswith("//")
    assert not db.dirty and not db.new
