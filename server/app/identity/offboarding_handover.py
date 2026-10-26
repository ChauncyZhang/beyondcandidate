"""Local responsibility transfer; account revocation and persistence belong to the caller.

Public plan fields are JSON-safe. Private context must not be serialized or accepted
from a client. Execution re-plans under locks and never commits or contacts providers.
"""

import hashlib
import json
from datetime import datetime, timezone

from sqlalchemy import inspect, select, update

from server.app.identity.models import (
    AuditLog, Job, JobCollaborator, User, UserRecruitingDepartmentScope, UserRole, UserStatus,
)
from server.app.integrations.feishu.models import FeishuInterviewSync, FeishuOnboardingConfig
from server.app.interviews.models import Interview, InterviewFeedback, InterviewParticipant
from server.app.offers.models import Offer, OfferApproval, OfferVersion, OrganizationSpecialOfferApprover
from server.app.onboarding.models import OnboardingRecord
from server.app.recruiting.models import Application, ApplicationReviewTask, Candidate
from server.app.screening.models import ScreeningRun
from server.app.talent.models import TalentPool, TalentPoolGrant, TalentPoolMembership


class HandoverPlanChanged(Exception):
    """Map to HTTP 409 offboarding_preview_changed; roll back the caller savepoint."""


LABELS = {
    "jobs": "职位", "candidates": "候选人", "applications": "招聘流程",
    "reviews": "经理评审", "offers": "Offer 审批", "talent": "人才库",
    "interviews": "面试", "onboarding": "入职", "configuration": "默认配置",
    "background": "后台初筛",
}
REASONS = {
    "eligible": "可交接当前职责，保留原始历史记录。",
    "successor_unavailable": "请选择同组织内另一位在职成员。",
    "successor_role_required": "请先通过成员配置为接任者设置对应业务角色，再重试交接。",
    "successor_scope_required": "请先通过现有权限配置授予接任者该资源的管理权限，再重试交接。",
    "collaborator_added": "将显式新增接任者的职位协作权限；不复制角色或全局范围。",
    "pool_grant_added": "将显式新增或提升接任者的人才库管理授权；保留原有联系限制。",
    "background_redirected": "后续初筛产物将使用有效接任者；原任务发起人保持不变。",
    "offer_self_approval": "接任者是当前 Offer 版本的提交人，请人工调整审批安排。",
    "offer_duplicate_approver": "接任者已参与本轮审批，请人工调整，避免重复审批。",
    "interview_participant_manual": "请通过面试排期流程替换未来参与者并同步日历。",
    "interview_feedback_manual": "原参与者的待提交反馈不能代写，请人工处理并保留原稿。",
    "external_calendar_manual": "离职成员创建的外部日程需要人工确认；不会改写日程或邀请快照。",
    "onboarding_external_manual": "请核对原发起人的飞书入职提交状态和远端实例，再通过现有流程处理；不自动重新提交。",
    "feishu_config_revalidation_required": "请修改固定审批人后重新验证飞书审批配置；当前配置不会自动改写或标记已验证。",
    "special_approver_manual": "请通过特殊 Offer 审批配置调整默认审批人，避免改变审批顺序或重复审批。",
}


def _utc(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _json_value(value):
    if isinstance(value, datetime):
        return _utc(value).isoformat()
    if isinstance(value, bytes):
        return hashlib.sha256(value).hexdigest()
    return str(value)


def _snapshot(row):
    return {column.key: getattr(row, column.key) for column in inspect(type(row)).columns}


ACTIVE_INTERVIEWS = {"draft", "scheduled", "confirmed", "rescheduled", "completed", "pending_feedback"}
# Failed/partial batches can be retried item-by-item and still need an owner.
ACTIVE_RUNS = {"queued", "parsing", "rule_scoring", "llm_scoring", "failed", "partial"}


def _reachable(rows, source_id, successor_id, run_creator_ids):
    """Select only source work and facts needed to decide its successor access."""
    chosen = {model: set() for model in rows}
    def choose(model, predicate):
        chosen[model].update(r.id for r in rows[model] if predicate(r))
    choose(Candidate, lambda r: r.owner_id == source_id and r.deleted_at is None)
    choose(Job, lambda r: r.status not in {"closed", "archived", "cancelled"} and source_id in {r.owner_id, r.hiring_owner_id, r.offer_approver_id})
    choose(JobCollaborator, lambda r: r.user_id == source_id)
    unfinished = {r.application_id for r in rows[OnboardingRecord] if r.status != "submitted"}
    unfinished.update(o.application_id for o in rows[Offer] if o.status == "accepted" and not any(r.application_id == o.application_id for r in rows[OnboardingRecord]))
    choose(Application, lambda r: (r.owner_id == source_id and (r.stage not in {"hired", "rejected", "withdrawn"} or r.id in unfinished)) or r.candidate_id in chosen[Candidate])
    choose(ApplicationReviewTask, lambda r: r.assignee_id == source_id and r.status == "open")
    choose(OfferApproval, lambda r: r.assignee_id == source_id and r.status in {"pending", "waiting"})
    choose(TalentPool, lambda r: r.owner_id == source_id)
    choose(TalentPoolGrant, lambda r: r.user_id == source_id and r.access_role == "manager")
    choose(TalentPoolMembership, lambda r: r.owner_id == source_id)
    participant_interviews = {r.interview_id for r in rows[InterviewParticipant] if r.user_id == source_id and r.task_status == "ready"}
    choose(Interview, lambda r: r.status in ACTIVE_INTERVIEWS and (source_id in {r.owner_id, r.created_by} or r.id in participant_interviews))
    source_apps = {a.id for a in rows[Application] if a.owner_id == source_id}
    choose(OnboardingRecord, lambda r: r.started_by == source_id or (r.status == "ready" and r.application_id in source_apps))
    choose(FeishuOnboardingConfig, lambda r: any(str(uid) == str(source_id) for node in r.node_approvers for uid in node.get("user_ids", [])))
    choose(OrganizationSpecialOfferApprover, lambda r: r.approver_id == source_id)
    choose(ScreeningRun, lambda r: r.created_by in run_creator_ids and r.status in ACTIVE_RUNS)
    for model in (ApplicationReviewTask, Interview, OnboardingRecord):
        chosen[Application].update(r.application_id for r in rows[model] if r.id in chosen[model])
    choose(Offer, lambda r: (r.status == "accepted" and r.application_id in chosen[Application]) or any(a.offer_id == r.id and a.id in chosen[OfferApproval] for a in rows[OfferApproval]))
    chosen[Application].update(o.application_id for o in rows[Offer] if o.id in chosen[Offer])
    choose(OfferApproval, lambda r: r.offer_id in chosen[Offer])
    choose(OfferVersion, lambda r: any(o.current_version_id == r.id and o.id in chosen[Offer] for o in rows[Offer]))
    for model in (Application, Offer, ScreeningRun, JobCollaborator):
        chosen[Job].update(r.job_id for r in rows[model] if r.id in chosen[model])
    for model in (Application, TalentPoolMembership):
        chosen[Candidate].update(r.candidate_id for r in rows[model] if r.id in chosen[model])
    for model in (TalentPoolMembership, TalentPoolGrant):
        chosen[TalentPool].update(r.pool_id for r in rows[model] if r.id in chosen[model])
    choose(JobCollaborator, lambda r: r.job_id in chosen[Job] and r.user_id in {source_id, successor_id})
    choose(TalentPoolGrant, lambda r: r.pool_id in chosen[TalentPool] and r.user_id in {source_id, successor_id})
    for model in (InterviewParticipant, InterviewFeedback, FeishuInterviewSync):
        choose(model, lambda r: r.interview_id in chosen[Interview])
    return {model: [r for r in values if r.id in chosen[model]] for model, values in rows.items()}


def build_handover_plan(db, organization_id, source_user, successor_user, *, now, lock=False):
    """Inventory current local work, without modifying it (including on GET).

    Locking uses an ordered source-reachable inventory rather than queue/run
    locks. The API holds Organization and sorted User locks before calling here.
    All mutable decision facts are fingerprinted, including unversioned tasks.
    """
    with db.no_autoflush:
        return _build(db, organization_id, source_user, successor_user, now=now, lock=lock)


def _build(db, organization_id, source_user, successor_user, *, now, lock):
    source_id = source_user.id
    successor_id = successor_user.id if successor_user is not None else None
    user_ids = [source_id] + ([successor_id] if successor_id is not None else [])
    statement = select(User).where(User.organization_id == organization_id, User.id.in_(user_ids)).order_by(User.id)
    if lock:
        statement = statement.with_for_update()
    users = {u.id: u for u in db.scalars(statement.execution_options(populate_existing=True))}
    successor = users.get(successor_id)
    available = source_id in users and successor is not None and successor_id != source_id and successor.status == UserStatus.ACTIVE

    def load(model, *conditions):
        query = select(model).where(model.organization_id == organization_id, *conditions).order_by(*inspect(model).primary_key)
        return list(db.scalars(query.execution_options(populate_existing=True)))

    from server.app.identity.offboarding_models import MemberOffboarding, handover_destination
    edges = dict(db.execute(select(MemberOffboarding.user_id, MemberOffboarding.successor_user_id).where(
        MemberOffboarding.organization_id == organization_id,
    )).all())
    ancestor_statuses = dict(db.execute(select(User.id, User.status).where(
        User.organization_id == organization_id, User.id.in_(edges),
    )).all()) if edges else {}
    run_creator_ids, frontier = {source_id}, {source_id}
    while frontier:
        frontier = {user_id for user_id, destination_id in edges.items()
                    if destination_id in frontier and user_id not in run_creator_ids
                    and ancestor_statuses.get(user_id) == UserStatus.DISABLED}
        run_creator_ids.update(frontier)

    # Match parent-before-child decision paths (notably Offer -> approval).
    models = (Candidate, Job, Application, Offer, OfferVersion, OfferApproval,
              ApplicationReviewTask, TalentPool, TalentPoolGrant, TalentPoolMembership,
              Interview, InterviewParticipant, InterviewFeedback, FeishuInterviewSync,
              OnboardingRecord, FeishuOnboardingConfig, OrganizationSpecialOfferApprover,
              JobCollaborator, ScreeningRun)
    rows = _reachable({model: load(model) for model in models}, source_id, successor_id, run_creator_ids)
    if lock:
        for model in models:
            if not rows[model] or model is ScreeningRun:
                continue  # Producers can hold Run -> User: do not invert that order.
            query = select(model).where(model.organization_id == organization_id, model.id.in_([r.id for r in rows[model]])).order_by(model.id).with_for_update()
            rows[model] = list(db.scalars(query.execution_options(populate_existing=True)))
    role_query = select(UserRole).where(UserRole.user_id == successor_id).order_by(UserRole.id)
    if lock:
        role_query = role_query.with_for_update()
    role_rows = list(db.scalars(role_query.execution_options(populate_existing=True))) if successor else []
    roles = {r.role for r in role_rows}
    scopes = load(UserRecruitingDepartmentScope, UserRecruitingDepartmentScope.user_id == successor_id) if successor else []
    department_ids = {s.department_id for s in scopes}
    jobs = {j.id: j for j in rows[Job]}
    candidates = {c.id: c for c in rows[Candidate]}
    apps = {a.id: a for a in rows[Application]}
    pools = {p.id: p for p in rows[TalentPool]}
    offers = {o.id: o for o in rows[Offer]}
    versions = {v.id: v for v in rows[OfferVersion]}
    collaborators = rows[JobCollaborator]
    grants = {(c.job_id, c.access_role) for c in collaborators if c.user_id == successor_id}
    added_grants = set()
    pool_grants = {(g.pool_id, g.access_role) for g in rows[TalentPoolGrant] if g.user_id == successor_id}
    added_pool_grants = set()
    items, actions = [], []

    def role_reason(required):
        if not available:
            return "successor_unavailable"
        return None if roles.intersection(required) else "successor_role_required"

    recruiting_reason = role_reason({"recruiter", "recruiting_admin"})
    manager_reason = role_reason({"hiring_manager", "recruiting_admin"})

    def scoped(job, *, manager=False):
        if not available or job is None:
            return False
        if "recruiting_admin" in roles:
            return True
        if successor.recruiting_scope_type == "organization":
            return True
        if successor.recruiting_scope_type == "departments" and job.department_id in department_ids:
            return True
        allowed = {"job_manager"} if manager else {"job_owner", "job_recruiter"}
        return any((job.id, grant) in grants | added_grants for grant in allowed)

    def item(category, resource_type, row, *, reason=None, title=None, changes=None, href=None):
        code = reason or "eligible"
        context_title, context_href = item_context(category, resource_type, row)
        entry = {"category": category, "resource_type": resource_type, "resource_id": str(row.id),
                 "title": context_title, "status": "blocked" if reason else "transferable",
                 "reason_code": code, "reason": REASONS[code]}
        entry["href"] = href or context_href
        items.append(entry)
        if reason is None and changes:
            actions.append(("update", row, changes))
        return entry

    def item_context(category, resource_type, row):
        # Routes mirror frontend/src/appRouter.js; no remote URLs or new routes.
        label = LABELS[category]
        if isinstance(row, Job) or isinstance(row, JobCollaborator):
            job = row if isinstance(row, Job) else jobs.get(row.job_id)
            if job:
                suffix = "/edit" if resource_type == "job_offer_approver" else ""
                return f"{label} · {job.title}", f"/jobs/{job.id}{suffix}"
        if isinstance(row, Candidate):
            return f"{label} · {row.display_name}", f"/candidates/{row.id}"
        if isinstance(row, FeishuOnboardingConfig):
            return f"飞书入职审批 · {row.approval_code}", "/settings/feishu"
        if isinstance(row, OrganizationSpecialOfferApprover):
            return f"特殊 Offer 默认审批 · 第 {row.position} 位", "/settings/offers"
        if isinstance(row, (TalentPool, TalentPoolGrant, TalentPoolMembership)):
            pool = row if isinstance(row, TalentPool) else pools.get(row.pool_id)
            if pool:
                name = pool.name
                if isinstance(row, TalentPoolMembership):
                    candidate = candidates.get(row.candidate_id)
                    name = f"{name} · {candidate.display_name if candidate else row.candidate_id}"
                return f"{label} · {name}", f"/talent/{pool.id}"
        if isinstance(row, ScreeningRun):
            job = jobs.get(row.job_id)
            return f"{label} · {job.title if job else row.job_id} · {row.id}", f"/screening/tasks/{row.id}"

        interview = row if isinstance(row, Interview) else None
        if isinstance(row, InterviewParticipant):
            interview = next((i for i in rows[Interview] if i.id == row.interview_id), None)
        if isinstance(row, OfferApproval):
            offer = offers.get(row.offer_id)
            app = apps.get(offer.application_id) if offer else None
        elif isinstance(row, Application):
            app = row
        elif interview is not None:
            app = apps.get(interview.application_id)
        else:
            app = apps.get(getattr(row, "application_id", None))
        if app:
            candidate, job = candidates.get(app.candidate_id), jobs.get(app.job_id)
            context = f"{candidate.display_name if candidate else app.candidate_id} · {job.title if job else app.job_id}"
            tab = "offer" if isinstance(row, (OfferApproval, OnboardingRecord)) else "interviews" if interview else "applications"
            target = f"/candidates/{app.candidate_id}?tab={tab}&application={app.id}&job={app.job_id}"
            if isinstance(row, OfferApproval):
                context += f" · 第 {row.round_number} 轮 / 第 {row.sequence} 步"
                target += f"&approval={row.id}"
            if interview:
                context += f" · {interview.round_name} · {_utc(interview.starts_at).isoformat()}"
                if resource_type == "interview_participant":
                    target = f"/interviews/{interview.id}/reschedule"
            return f"{label} · {context}", target
        return f"{label} · {row.id}", "/settings/organization/members"

    def add_collaborator(job, access_role):
        key = (job.id, access_role)
        if key in grants | added_grants:
            return
        added_grants.add(key)
        entry = item("jobs", "job_collaborator", job, title=job.title)
        entry["reason_code"], entry["reason"] = "collaborator_added", REASONS["collaborator_added"]
        entry["access_role"] = access_role
        actions.append(("collaborator", job, {"access_role": access_role}))
        actions.append(("update", job, {}))

    live_jobs = {j.id for j in rows[Job] if j.status not in {"closed", "archived", "cancelled"}}
    for job in rows[Job]:
        if job.id not in live_jobs:
            continue
        if job.owner_id == source_id:
            item("jobs", "job_owner", job, reason=recruiting_reason, title=job.title, changes={"owner_id": successor_id})
            if not recruiting_reason and not scoped(job):
                add_collaborator(job, "job_owner")
        if job.hiring_owner_id == source_id:
            item("jobs", "job_hiring_owner", job, reason=manager_reason, title=job.title, changes={"hiring_owner_id": successor_id})
            if not manager_reason and not scoped(job, manager=True):
                add_collaborator(job, "job_manager")
        if job.offer_approver_id == source_id:
            item("configuration", "job_offer_approver", job, reason=manager_reason, title=job.title, changes={"offer_approver_id": successor_id})
    for collaborator in collaborators:
        if collaborator.user_id != source_id or collaborator.job_id not in live_jobs:
            continue
        job = jobs[collaborator.job_id]
        reason = manager_reason if collaborator.access_role == "job_manager" else recruiting_reason
        if reason:
            item("jobs", "job_collaborator", collaborator, reason=reason, title=job.title)
        else:
            add_collaborator(job, collaborator.access_role)

    unfinished = {r.application_id for r in rows[OnboardingRecord] if r.status != "submitted"}
    recorded = {r.application_id for r in rows[OnboardingRecord]}
    unfinished.update(o.application_id for o in rows[Offer] if o.status == "accepted" and o.application_id not in recorded)
    for app in rows[Application]:
        live = app.stage not in {"hired", "rejected", "withdrawn"} or (app.stage == "hired" and app.id in unfinished)
        if app.owner_id == source_id and live:
            reason = recruiting_reason or (None if scoped(jobs.get(app.job_id)) else "successor_scope_required")
            item("applications", "application", app, reason=reason, changes={"owner_id": successor_id})
    for candidate in rows[Candidate]:
        if candidate.owner_id != source_id or candidate.deleted_at is not None:
            continue
        associated = [a for a in rows[Application] if a.candidate_id == candidate.id]
        access = not associated or any(scoped(jobs.get(a.job_id)) for a in associated)
        item("candidates", "candidate", candidate, title=candidate.display_name,
             reason=recruiting_reason or (None if access else "successor_scope_required"), changes={"owner_id": successor_id})

    for task in rows[ApplicationReviewTask]:
        if task.assignee_id == source_id and task.status == "open":
            app = apps.get(task.application_id)
            job = jobs.get(app.job_id) if app else None
            # Explicit hiring ownership is a valid review assignment too.
            access = scoped(job, manager=True) or (job is not None and job.hiring_owner_id == successor_id)
            item("reviews", "review_task", task, reason=manager_reason or (None if access else "successor_scope_required"), changes={"assignee_id": successor_id})

    rounds = {}
    for approval in rows[OfferApproval]:
        rounds[approval.offer_id] = max(rounds.get(approval.offer_id, 0), approval.round_number)
    changed_offers = set()
    for approval in rows[OfferApproval]:
        offer = offers.get(approval.offer_id)
        if not (approval.assignee_id == source_id and approval.status in {"waiting", "pending"}
                and approval.decided_at is None and offer and offer.status == "pending_approval"
                and approval.round_number == rounds[offer.id] and approval.offer_version_id == offer.current_version_id):
            continue
        version = versions.get(offer.current_version_id)
        reason = manager_reason
        if not reason and version and version.created_by == successor_id:
            reason = "offer_self_approval"
        if not reason and any(a.offer_id == offer.id and a.round_number == approval.round_number and a.assignee_id == successor_id for a in rows[OfferApproval]):
            reason = "offer_duplicate_approver"
        item("offers", "offer_approval", approval, reason=reason, changes={"assignee_id": successor_id})
        if not reason:
            changed_offers.add(offer.id)
    for offer_id in sorted(changed_offers, key=str):
        actions.append(("update", offers[offer_id], {}))  # Invalidate existing decision ETags.

    transferable_pools = set()
    for pool in rows[TalentPool]:
        if pool.owner_id == source_id:
            item("talent", "talent_pool", pool, reason=recruiting_reason, title=pool.name, changes={"owner_id": successor_id})
            if not recruiting_reason:
                transferable_pools.add(pool.id)
    for grant in rows[TalentPoolGrant]:
        if grant.user_id != source_id or grant.access_role != "manager" or (grant.pool_id, "manager") in pool_grants:
            continue
        entry = item("talent", "talent_pool_grant", grant, reason=recruiting_reason)
        if not recruiting_reason:
            entry["reason_code"], entry["reason"] = "pool_grant_added", REASONS["pool_grant_added"]
            added_pool_grants.add(grant.pool_id)
            existing = next((g for g in rows[TalentPoolGrant] if g.pool_id == grant.pool_id and g.user_id == successor_id), None)
            if existing:
                actions.append(("update", existing, {"access_role": "manager"}))
            else:
                actions.append(("pool_grant", grant, {}))
            if grant.pool_id in pools:
                actions.append(("update", pools[grant.pool_id], {}))
    for membership in rows[TalentPoolMembership]:
        if membership.owner_id != source_id:
            continue
        pool = pools.get(membership.pool_id)
        access = pool and ("recruiting_admin" in roles or pool.owner_id == successor_id
                          or pool.id in transferable_pools or pool.id in added_pool_grants or (pool.id, "manager") in pool_grants)
        # Status, source, contact prohibition and retention remain untouched.
        item("talent", "talent_membership", membership, reason=recruiting_reason or (None if access else "successor_scope_required"), changes={"owner_id": successor_id})

    active_interviews = ACTIVE_INTERVIEWS
    interviews = {i.id: i for i in rows[Interview]}
    for interview in rows[Interview]:
        if interview.status not in active_interviews:
            continue
        app = apps.get(interview.application_id)
        if interview.owner_id == source_id:
            access = app and scoped(jobs.get(app.job_id))
            item("interviews", "interview", interview, reason=recruiting_reason or (None if access else "successor_scope_required"), changes={"owner_id": successor_id})
        if interview.created_by == source_id and any(s.interview_id == interview.id and (s.external_event_id or s.sync_status in {"pending", "syncing", "failed", "pending_confirmation"}) for s in rows[FeishuInterviewSync]):
            item("interviews", "interview_calendar", interview, reason="external_calendar_manual")
    feedback = {(f.interview_id, f.author_id): f for f in rows[InterviewFeedback]}
    for participant in rows[InterviewParticipant]:
        interview = interviews.get(participant.interview_id)
        if participant.user_id != source_id or not interview or interview.status not in active_interviews or participant.task_status != "ready":
            continue
        future = _utc(interview.starts_at) > _utc(now)
        original_feedback = feedback.get((interview.id, source_id))
        if future:
            item("interviews", "interview_participant", participant, reason="interview_participant_manual")
        elif participant.required_feedback and (original_feedback is None or original_feedback.status == "draft"):
            item("interviews", "interview_feedback", participant, reason="interview_feedback_manual")

    for record in rows[OnboardingRecord]:
        app = apps.get(record.application_id)
        if record.status == "ready" and app and app.owner_id == source_id:
            reason = recruiting_reason or (None if scoped(jobs.get(record.job_id)) else "successor_scope_required")
            item("onboarding", "onboarding", record, reason=reason)
        elif record.status in {"submitting", "failed", "submitted"} and record.started_by == source_id:
            item("onboarding", "onboarding", record, reason="onboarding_external_manual")
    for config in rows[FeishuOnboardingConfig]:
        references_source = any(str(user_id) == str(source_id) for node in config.node_approvers for user_id in node.get("user_ids", []))
        if references_source:
            item("configuration", "feishu_onboarding_config", config, reason="feishu_config_revalidation_required")
    for config in rows[OrganizationSpecialOfferApprover]:
        if config.approver_id == source_id:
            item("configuration", "special_offer_approver", config, reason="special_approver_manual")

    destinations = {}
    for run in rows[ScreeningRun]:
        reason = recruiting_reason or (None if scoped(jobs.get(run.job_id)) else "successor_scope_required")
        if run.created_by not in destinations:
            destinations[run.created_by] = handover_destination(db, organization_id, run.created_by)
        destination = destinations[run.created_by]
        if destination is not None and destination.id == successor_id and not reason:
            continue
        entry = item("background", "screening_run", run, reason=reason)
        if not reason:
            entry["reason_code"], entry["reason"] = "background_redirected", REASONS["background_redirected"]

    items.sort(key=lambda i: (i["category"], i["resource_type"], i["resource_id"], i.get("access_role", "")))
    counts = []
    for category, label in LABELS.items():
        group = [i for i in items if i["category"] == category]
        if group:
            transferable = sum(i["status"] == "transferable" for i in group)
            counts.append({"category": category, "label": label, "total": len(group), "transferable": transferable, "blocked": len(group) - transferable})
    facts = {model.__tablename__: [_snapshot(row) for row in rows[model]] for model in models}
    # Only path edges carrying an unfinished inherited run affect this preview.
    relevant_ancestors = set()
    for run in rows[ScreeningRun]:
        ancestor_id = run.created_by
        while ancestor_id != source_id and ancestor_id not in relevant_ancestors and ancestor_id in edges:
            relevant_ancestors.add(ancestor_id)
            ancestor_id = edges[ancestor_id]
    facts["run_ancestors"] = [
        {"user_id": user_id, "successor_user_id": edges[user_id], "status": ancestor_statuses.get(user_id)}
        for user_id in sorted(relevant_ancestors, key=str)
    ]
    facts["successor"] = None if not successor else {
        "id": successor.id, "status": successor.status, "authorization_version": successor.authorization_version,
        "scope": successor.recruiting_scope_type, "roles": sorted(roles), "departments": sorted(map(str, department_ids)),
    }
    fingerprint = hashlib.sha256(json.dumps({"organization": organization_id, "source": source_id,
                                            "successor": successor_id, "facts": facts,
                                            "items": [i for i in items if i["category"] != "background"]},
                                           sort_keys=True, default=_json_value, ensure_ascii=True).encode()).hexdigest()
    return {"items": items, "counts": counts, "fingerprint": fingerprint,
            "_context": {"organization_id": organization_id, "source_id": source_id, "successor_id": successor_id},
            "_actions": actions}


def apply_handover(db, plan, *, actor_user_id, trace_id, now):
    """Apply an in-process plan in the caller transaction; raise on any stale fact.

    The caller must roll back on HandoverPlanChanged (normally a SAVEPOINT).
    No queue messages, emails or external calendar writes are issued here.
    """
    context = plan["_context"]
    source = db.get(User, context["source_id"])
    successor = db.get(User, context["successor_id"]) if context["successor_id"] else None
    if source is None:
        raise HandoverPlanChanged
    current = build_handover_plan(db, context["organization_id"], source, successor, now=now, lock=True)
    if current["fingerprint"] != plan["fingerprint"]:
        if current["fingerprint"] == context.get("after_fingerprint"):
            return 0
        raise HandoverPlanChanged
    merged = {}
    for operation, row, changes in current["_actions"]:
        if operation == "collaborator":
            db.add(JobCollaborator(organization_id=context["organization_id"], job_id=row.id,
                                   user_id=context["successor_id"], access_role=changes["access_role"]))
        elif operation == "pool_grant":
            db.add(TalentPoolGrant(organization_id=context["organization_id"], pool_id=row.pool_id,
                                  user_id=context["successor_id"], access_role="manager"))
        else:
            key = (type(row), row.id)
            if key not in merged:
                merged[key] = (row, {})
            merged[key][1].update(changes)
    for (model, _), (row, changes) in merged.items():
        original = _snapshot(row)
        values = dict(changes)
        if "version" in original:
            values["version"] = original["version"] + 1
        if "updated_at" in original:
            values["updated_at"] = now
        if not values:
            continue
        # Unversioned review tasks and approval rows require state CAS as well.
        conditions = [getattr(model, key).is_(None) if value is None else getattr(model, key) == value
                      for key, value in original.items() if key not in {"content", "pii_ciphertext"} and not isinstance(value, (dict, list))]
        result = db.execute(update(model).where(*conditions).values(**values).execution_options(synchronize_session=False))
        if result.rowcount != 1:
            raise HandoverPlanChanged
        # Track ORM changes too, so savepoint rollback expires the identity map.
        for field, value in values.items():
            setattr(row, field, value)
    count = sum(i["status"] == "transferable" for i in plan["items"])
    if count:
        db.add(AuditLog(organization_id=context["organization_id"], actor_user_id=actor_user_id,
                        category="identity", event_type="member.handover_applied", outcome="success",
                        resource_type="user", resource_id=context["source_id"], trace_id=trace_id, created_at=now,
                        metadata_json={"successor_user_id": str(context["successor_id"]), "transferred_count": count}))
    db.flush()
    context["after_fingerprint"] = build_handover_plan(
        db, context["organization_id"], source, successor, now=now,
    )["fingerprint"]
    return count
