import hashlib
import json
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Callable

from sqlalchemy import delete, func, select, text, update

from server.app.governance.retention import (
    lock_candidate_retention_facts,
    recalculate_candidate_retention,
)
from server.app.screening.rules import RuleSnapshot,RuleSnapshotError


class InvalidStateTransition(Exception): pass
class ResourceVersionConflict(Exception): pass
class IdempotencyConflict(Exception): pass
class ActiveApplicationExists(Exception): pass
class TicketInvalid(Exception): pass
class InvalidAggregateRelationship(Exception): pass
class CandidateUnavailable(InvalidStateTransition): pass


def _job_definition_contents(command):
    try:
        jd_content={key:command[key] for key in ("description","location","process_template","llm_enabled")}
        jd_content["workflow_template_id"] = (
            str(command["workflow_template_id"])
            if command.get("workflow_template_id") is not None
            else None
        )
        rule_content={key:command[key] for key in ("must_have","nice_to_have")}
    except (KeyError,TypeError) as error:
        raise RuleSnapshotError from error
    RuleSnapshot.from_storage(jd_content,rule_content)
    return jd_content,rule_content


def lock_active_candidate(db, organization_id, candidate_id):
    candidate = lock_candidate_retention_facts(db, organization_id, candidate_id)
    if candidate is None or candidate.deleted_at is not None:
        raise CandidateUnavailable
    return candidate


class SystemClock:
    def current_time(self):
        return datetime.now(timezone.utc)


class SystemTokens:
    def new_token(self):
        return secrets.token_urlsafe(32)


class RecruitingService:
    JOB_EDGES = {"draft": {"open"}, "open": {"paused", "closed"}, "paused": {"open", "closed"}, "closed": {"archived"}, "archived": set()}
    APPLICATION_PATH = ["new", "review", "contact", "interview_pending", "interviewing", "decision", "passed", "hired"]
    TERMINAL = {"hired", "rejected", "withdrawn"}

    def __init__(self, clock=None, tokens=None):
        self.clock = clock or SystemClock()
        self.tokens = tokens or SystemTokens()
        self.idempotency = {}
        self.applications = {}
        self.download_tickets = {}

    def transition_job_state(self, source, target):
        if target not in self.JOB_EDGES.get(source, set()):
            raise InvalidStateTransition
        return target

    def transition_application_state(self, source, target, *, reason_code=None, reason_text=None):
        allowed = set()
        if source not in self.TERMINAL:
            if source in self.APPLICATION_PATH:
                index = self.APPLICATION_PATH.index(source)
                if index + 1 < len(self.APPLICATION_PATH):
                    allowed.add(self.APPLICATION_PATH[index + 1])
            allowed.update({"rejected", "withdrawn"})
        if target not in allowed or (target == "rejected" and not (reason_code or (reason_text and reason_text.strip()))):
            raise InvalidStateTransition
        return target

    def require_version(self, if_match, current):
        if if_match != f'"{current}"':
            raise ResourceVersionConflict
        return current

    def idempotent(self, organization_id, user_id, operation, key, body, action: Callable):
        identity = (organization_id, user_id, operation, key)
        fingerprint = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        previous = self.idempotency.get(identity)
        if previous:
            if previous[0] != fingerprint:
                raise IdempotencyConflict
            return previous[1]
        result = action()
        self.idempotency[identity] = (fingerprint, result)
        return result

    def create_application(self, organization_id, candidate_id, job_id, resume_id, owner_id, idempotency_key):
        def create():
            related = [a for a in self.applications.values() if a["organization_id"] == organization_id and a["candidate_id"] == candidate_id and a["job_id"] == job_id]
            if any(a["stage"] not in self.TERMINAL for a in related):
                raise ActiveApplicationExists
            source = related[-1]["id"] if related else None
            record = {"id": str(uuid.uuid4()), "organization_id": organization_id, "candidate_id": candidate_id, "job_id": job_id, "resume_id": resume_id, "owner_id": owner_id, "stage": "new", "version": 1, "source_application_id": source}
            self.applications[record["id"]] = record
            return record
        return self.idempotent(organization_id, owner_id, "application.create", idempotency_key, {"candidate_id": candidate_id, "job_id": job_id, "resume_id": resume_id}, create)

    def issue_download_ticket(self, organization_id, user_id, resume_id):
        raw = self.tokens.new_token()
        token_hash = hashlib.sha256(raw.encode()).hexdigest()
        self.download_tickets[token_hash] = {"organization_id": organization_id, "user_id": user_id, "resume_id": resume_id, "expires_at": self.clock.current_time() + timedelta(seconds=60), "consumed_at": None}
        return raw

    def consume_download_ticket(self, raw, organization_id, user_id, resume_id):
        stored = self.download_tickets.get(hashlib.sha256(raw.encode()).hexdigest())
        if not stored or stored["consumed_at"] or stored["expires_at"] <= self.clock.current_time() or any(stored[key] != value for key, value in (("organization_id", organization_id), ("user_id", user_id), ("resume_id", resume_id))):
            raise TicketInvalid
        stored["consumed_at"] = self.clock.current_time()
        return stored


def _apply_application_transition(db, application, target, *, actor_user_id, trace_id, reason_code=None, reason_text=None):
    from server.app.identity.models import AuditLog
    from server.app.recruiting.models import ApplicationStageEvent

    source = application.stage
    RecruitingService().transition_application_state(source, target, reason_code=reason_code, reason_text=reason_text)
    application.stage = target
    application.version += 1
    application.updated_at = datetime.now(timezone.utc)
    safe_payload = {"from_stage": source, "to_stage": target}
    if reason_code:
        safe_payload["reason_code"] = reason_code
    event_payload = dict(safe_payload)
    if reason_text and reason_text.strip():
        event_payload["reason_text"] = reason_text.strip()
    db.add(ApplicationStageEvent(organization_id=application.organization_id, application_id=application.id, actor_user_id=actor_user_id, event_type="application.stage_changed", payload=event_payload))
    db.add(AuditLog(organization_id=application.organization_id, actor_user_id=actor_user_id, event_type="application.stage_changed", outcome="success", trace_id=trace_id, metadata_json=safe_payload))
    db.flush()
    recalculate_candidate_retention(
        db, application.organization_id, application.candidate_id
    )
    return application


def transition_application_record(db, organization_id, application_id, target, *, expected_version, actor_user_id, trace_id, reason_code=None, reason_text=None):
    from server.app.recruiting.models import Application

    candidate_id = db.scalar(
        select(Application.candidate_id).where(
            Application.organization_id == organization_id,
            Application.id == application_id,
        )
    )
    if candidate_id is None:
        raise ResourceVersionConflict
    lock_active_candidate(db, organization_id, candidate_id)
    application = db.scalar(
        select(Application)
        .where(
            Application.organization_id == organization_id,
            Application.id == application_id,
        )
        .with_for_update()
    )
    if (
        application is None
        or application.candidate_id != candidate_id
        or application.version != expected_version
    ):
        raise ResourceVersionConflict
    return _apply_application_transition(db, application, target, actor_user_id=actor_user_id, trace_id=trace_id, reason_code=reason_code, reason_text=reason_text)


def withdraw_candidate_applications_for_deletion(
    db,
    organization_id,
    candidate_id,
    *,
    actor_user_id,
    trace_id,
):
    from server.app.recruiting.models import Application
    from server.app.recruiting.tasks import close_review_task

    lock_active_candidate(db, organization_id, candidate_id)
    applications = list(
        db.scalars(
            select(Application)
            .where(
                Application.organization_id == organization_id,
                Application.candidate_id == candidate_id,
                Application.stage.not_in(RecruitingService.TERMINAL),
            )
            .order_by(Application.id)
            .with_for_update()
        )
    )
    for application in applications:
        _apply_application_transition(
            db,
            application,
            "withdrawn",
            actor_user_id=actor_user_id,
            trace_id=trace_id,
            reason_code="candidate_deletion_approved",
        )
        close_review_task(
            db,
            organization_id=organization_id,
            application_id=application.id,
        )
    return len(applications)


APPLICATION_WORKFLOW_ACTIONS = {
    "review_approved": ("review", "interview_pending"),
    "review_rejected": ("review", "rejected"),
    "hiring_approved": ("decision", "passed"),
    "hiring_rejected": ("decision", "rejected"),
    "offer_accepted": ("passed", "hired"),
    "offer_declined": ("passed", "withdrawn"),
}


def apply_application_workflow_action_record(
    db,
    organization_id,
    application_id,
    action,
    *,
    expected_version,
    actor_user_id,
    trace_id,
    reason_text=None,
):
    from server.app.recruiting.models import Application
    from server.app.recruiting.tasks import close_review_task

    source, target = APPLICATION_WORKFLOW_ACTIONS[action]
    candidate_id = db.scalar(
        select(Application.candidate_id).where(
            Application.organization_id == organization_id,
            Application.id == application_id,
        )
    )
    if candidate_id is None:
        raise ResourceVersionConflict
    lock_active_candidate(db, organization_id, candidate_id)
    application = db.scalar(
        select(Application)
        .where(
            Application.organization_id == organization_id,
            Application.id == application_id,
        )
        .with_for_update()
    )
    if application is None or application.version != expected_version:
        raise ResourceVersionConflict
    if application.stage != source:
        raise InvalidStateTransition
    if action in {"review_rejected", "hiring_rejected", "offer_declined"} and not (reason_text and reason_text.strip()):
        raise InvalidStateTransition

    if target in RecruitingService.TERMINAL:
        application = _apply_application_transition(
            db,
            application,
            target,
            actor_user_id=actor_user_id,
            trace_id=trace_id,
            reason_text=reason_text,
        )
    else:
        target_index = RecruitingService.APPLICATION_PATH.index(target)
        while application.stage != target:
            source_index = RecruitingService.APPLICATION_PATH.index(application.stage)
            if source_index >= target_index:
                raise InvalidStateTransition
            application = _apply_application_transition(
                db,
                application,
                RecruitingService.APPLICATION_PATH[source_index + 1],
                actor_user_id=actor_user_id,
                trace_id=trace_id,
            )
    if action in {"review_approved", "review_rejected"}:
        close_review_task(
            db,
            organization_id=organization_id,
            application_id=application_id,
        )
    return application


def undo_bulk_advance_record(db, application, *, expected_version, actor_user_id, trace_id, run_id, item_id):
    from server.app.identity.models import AuditLog
    from server.app.recruiting.models import ApplicationStageEvent

    if application.stage != "review" or application.version != expected_version:
        raise ResourceVersionConflict
    application.stage = "new"
    application.version += 1
    application.updated_at = datetime.now(timezone.utc)
    payload = {
        "from_stage": "review",
        "to_stage": "new",
        "undo": True,
        "run_id": str(run_id),
        "item_id": str(item_id),
        "undone_application_version": expected_version,
    }
    db.add(ApplicationStageEvent(organization_id=application.organization_id, application_id=application.id, actor_user_id=actor_user_id, event_type="application.bulk_advance_undone", payload=payload))
    db.add(AuditLog(organization_id=application.organization_id, actor_user_id=actor_user_id, event_type="application.bulk_advance_undone", outcome="success", resource_type="application", resource_id=application.id, trace_id=trace_id, metadata_json={"application_id":str(application.id),"run_id":str(run_id),"item_id":str(item_id),"undone_application_version":expected_version}))
    db.flush()
    recalculate_candidate_retention(db, application.organization_id, application.candidate_id)
    return application


def transition_job_record(db, job_id, target, *, expected_version, actor_user_id, trace_id):
    from server.app.identity.models import AuditLog, Job

    job = db.scalar(select(Job).where(Job.id == job_id).with_for_update())
    if job is None or job.version != expected_version:
        raise ResourceVersionConflict
    source = job.status
    RecruitingService().transition_job_state(source, target)
    job.status = target
    job.version += 1
    job.updated_at = datetime.now(timezone.utc)
    db.add(AuditLog(organization_id=job.organization_id, actor_user_id=actor_user_id, event_type="job.stage_changed", outcome="success", trace_id=trace_id, metadata_json={"from_stage": source, "to_stage": target}))
    db.flush()
    return job


def patch_job_record(db, organization_id, job_id, changes, *, expected_version, actor_user_id, trace_id):
    from server.app.identity.models import AuditLog, Job

    row = db.scalar(update(Job).where(
        Job.organization_id == organization_id, Job.id == job_id, Job.version == expected_version,
    ).values(**changes, version=Job.version + 1, updated_at=datetime.now(timezone.utc)).returning(Job))
    if row is None:
        raise ResourceVersionConflict
    db.add(AuditLog(organization_id=organization_id, actor_user_id=actor_user_id, event_type="job.updated", outcome="success", trace_id=trace_id, metadata_json={"fields": sorted(changes)}))
    db.flush()
    return row


def lock_job_for_version_write(db, organization_id, job_id):
    from server.app.identity.models import Job

    return db.scalar(select(Job).where(Job.organization_id == organization_id, Job.id == job_id).with_for_update())


def create_job_definition_record(db, organization_id, actor_user_id, command, *, trace_id):
    from server.app.identity.models import AuditLog, Job, JobCollaborator
    from server.app.recruiting.models import JobJdVersion, ScreeningRuleVersion

    jd_content,rule_content=_job_definition_contents(command)
    job = Job(
        organization_id=organization_id,
        owner_id=actor_user_id,
        status="open" if command["publish"] else "draft",
        **{key: command[key] for key in ("title", "department_id", "headcount", "priority", "hiring_owner_id")},
        workflow_template_id=command["workflow_template_id"],
    )
    db.add(job)
    db.flush()
    jd = JobJdVersion(
        organization_id=organization_id,
        job_id=job.id,
        version_number=1,
        content=jd_content,
        created_by=actor_user_id,
    )
    rules = ScreeningRuleVersion(
        organization_id=organization_id,
        job_id=job.id,
        version_number=1,
        content=rule_content,
        created_by=actor_user_id,
    )
    db.add_all([
        JobCollaborator(organization_id=organization_id, job_id=job.id, user_id=actor_user_id, access_role="job_owner"),
        jd,
        rules,
    ])
    if command["hiring_owner_id"] is not None:
        db.add(JobCollaborator(organization_id=organization_id, job_id=job.id, user_id=command["hiring_owner_id"], access_role="job_manager"))
    db.flush()
    safe_metadata = {"job_id": str(job.id), "jd_version_number": 1, "rule_version_number": 1, "status": job.status}
    db.add(AuditLog(organization_id=organization_id, actor_user_id=actor_user_id, event_type="job.definition_created", outcome="success", trace_id=trace_id, metadata_json=safe_metadata))
    db.flush()
    return job, jd, rules


def replace_job_definition_record(db, organization_id, job_id, actor_user_id, command, *, expected_version, trace_id):
    from server.app.identity.models import AuditLog, Job, JobCollaborator
    from server.app.recruiting.models import JobJdVersion, ScreeningRuleVersion

    jd_content,rule_content=_job_definition_contents(command)
    job = lock_job_for_version_write(db, organization_id, job_id)
    if job is None or job.version != expected_version:
        raise ResourceVersionConflict
    if command["publish"] and job.status != "draft":
        raise InvalidStateTransition
    source_status = job.status
    for key in ("title", "department_id", "headcount", "priority", "hiring_owner_id"):
        setattr(job, key, command[key])
    job.workflow_template_id = command["workflow_template_id"]
    db.execute(delete(JobCollaborator).where(
        JobCollaborator.organization_id == organization_id,
        JobCollaborator.job_id == job_id,
        JobCollaborator.access_role == "job_manager",
    ))
    if command["hiring_owner_id"] is not None:
        db.add(JobCollaborator(organization_id=organization_id, job_id=job_id, user_id=command["hiring_owner_id"], access_role="job_manager"))
    if command["publish"]:
        job.status = "open"
    job.version += 1
    job.updated_at = datetime.now(timezone.utc)
    jd_number = (db.scalar(select(func.max(JobJdVersion.version_number)).where(JobJdVersion.organization_id == organization_id, JobJdVersion.job_id == job_id)) or 0) + 1
    rule_number = (db.scalar(select(func.max(ScreeningRuleVersion.version_number)).where(ScreeningRuleVersion.organization_id == organization_id, ScreeningRuleVersion.job_id == job_id)) or 0) + 1
    jd = JobJdVersion(organization_id=organization_id, job_id=job_id, version_number=jd_number, content=jd_content, created_by=actor_user_id)
    rules = ScreeningRuleVersion(organization_id=organization_id, job_id=job_id, version_number=rule_number, content=rule_content, created_by=actor_user_id)
    db.add_all([jd, rules])
    db.flush()
    safe_metadata = {"job_id": str(job.id), "job_version": job.version, "jd_version_number": jd_number, "rule_version_number": rule_number, "published": command["publish"]}
    db.add(AuditLog(organization_id=organization_id, actor_user_id=actor_user_id, event_type="job.definition_replaced", outcome="success", trace_id=trace_id, metadata_json=safe_metadata))
    if command["publish"]:
        db.add(AuditLog(organization_id=organization_id, actor_user_id=actor_user_id, event_type="job.published", outcome="success", trace_id=trace_id, metadata_json={"job_id": str(job.id), "from_status": source_status, "to_status": "open"}))
    db.flush()
    return job, jd, rules


def patch_candidate_record(db, organization_id, candidate_id, changes, *, expected_version, actor_user_id, trace_id):
    from server.app.identity.models import AuditLog
    from server.app.recruiting.models import Candidate, CandidateEvent

    lock_active_candidate(db, organization_id, candidate_id)
    row = db.scalar(update(Candidate).where(
        Candidate.organization_id == organization_id, Candidate.id == candidate_id, Candidate.version == expected_version,
    ).values(**changes, version=Candidate.version + 1, updated_at=datetime.now(timezone.utc)).returning(Candidate))
    if row is None:
        raise ResourceVersionConflict
    safe = {"fields": sorted(changes)}
    db.add(CandidateEvent(organization_id=organization_id, candidate_id=candidate_id, actor_user_id=actor_user_id, event_type="candidate.corrected", payload=safe))
    db.add(AuditLog(organization_id=organization_id, actor_user_id=actor_user_id, event_type="candidate.corrected", outcome="success", trace_id=trace_id, metadata_json=safe))
    db.flush()
    recalculate_candidate_retention(db, organization_id, candidate_id)
    return row


def patch_application_record(db, organization_id, application_id, changes, *, expected_version, actor_user_id, trace_id):
    from server.app.identity.models import AuditLog
    from server.app.recruiting.models import Application, CandidateEvent

    candidate_id = db.scalar(
        select(Application.candidate_id).where(
            Application.organization_id == organization_id,
            Application.id == application_id,
        )
    )
    if candidate_id is None:
        raise ResourceVersionConflict
    lock_active_candidate(db, organization_id, candidate_id)
    row = db.scalar(update(Application).where(
        Application.organization_id == organization_id, Application.id == application_id, Application.version == expected_version,
    ).values(**changes, version=Application.version + 1, updated_at=datetime.now(timezone.utc)).returning(Application))
    if row is None:
        raise ResourceVersionConflict
    safe = {"application_id": str(application_id), "fields": sorted(changes)}
    db.add(CandidateEvent(organization_id=organization_id, candidate_id=row.candidate_id, actor_user_id=actor_user_id, event_type="application.updated", payload=safe))
    db.add(AuditLog(organization_id=organization_id, actor_user_id=actor_user_id, event_type="application.updated", outcome="success", trace_id=trace_id, metadata_json=safe))
    db.flush()
    recalculate_candidate_retention(db, organization_id, row.candidate_id)
    return row


def create_application_record(db, *, organization_id, candidate_id, job_id, resume_id, owner_id, source="manual"):
    from server.app.recruiting.models import Application, Candidate, Resume

    lock_active_candidate(db, organization_id, candidate_id)
    resume = db.scalar(select(Resume).where(Resume.organization_id == organization_id, Resume.id == resume_id))
    if resume is None or resume.candidate_id != candidate_id:
        raise InvalidAggregateRelationship
    related = list(db.scalars(select(Application).where(Application.organization_id == organization_id, Application.candidate_id == candidate_id, Application.job_id == job_id).order_by(Application.created_at.desc(), Application.id.desc())))
    if any(item.stage not in RecruitingService.TERMINAL for item in related):
        raise ActiveApplicationExists
    application = Application(organization_id=organization_id, candidate_id=candidate_id, job_id=job_id, resume_id=resume_id, owner_id=owner_id, source=source, stage="new", source_application_id=related[0].id if related else None)
    db.add(application)
    db.flush()
    recalculate_candidate_retention(db, organization_id, candidate_id)
    return application


def issue_download_ticket_record(db, organization_id, user_id, resume_id, clock, tokens):
    from server.app.recruiting.models import DownloadTicket, Resume

    candidate_id = db.scalar(select(Resume.candidate_id).where(Resume.organization_id == organization_id, Resume.id == resume_id))
    if candidate_id is None:
        raise CandidateUnavailable
    lock_active_candidate(db, organization_id, candidate_id)
    raw = tokens.new_token()
    db.add(DownloadTicket(organization_id=organization_id, user_id=user_id, resume_id=resume_id, token_hash=hashlib.sha256(raw.encode()).hexdigest(), expires_at=clock.current_time() + timedelta(seconds=60)))
    db.flush()
    return raw


def consume_download_ticket_record(db, raw, organization_id, user_id, resume_id, clock):
    from server.app.recruiting.models import DownloadTicket, Resume

    ticket = db.scalar(select(DownloadTicket).where(DownloadTicket.token_hash == hashlib.sha256(raw.encode()).hexdigest()).with_for_update())
    if ticket is None or ticket.consumed_at is not None or ticket.expires_at.replace(tzinfo=ticket.expires_at.tzinfo or timezone.utc) <= clock.current_time() or ticket.organization_id != organization_id or ticket.user_id != user_id or ticket.resume_id != resume_id:
        raise TicketInvalid
    candidate_id = db.scalar(select(Resume.candidate_id).where(Resume.organization_id == organization_id, Resume.id == resume_id))
    if candidate_id is None:
        raise TicketInvalid
    try:
        lock_active_candidate(db, organization_id, candidate_id)
    except CandidateUnavailable:
        raise TicketInvalid from None
    ticket.consumed_at = clock.current_time()
    db.flush()
    return ticket


def persisted_idempotent(db, organization_id, user_id, operation, key, body, action):
    from server.app.recruiting.models import IdempotencyRecord

    request_hash = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()
    if db.get_bind().dialect.name == "postgresql":
        lock_key = f"{organization_id}:{user_id}:{operation}:{key}"
        db.execute(text("select pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"), {"lock_key": lock_key})
    record = db.scalar(select(IdempotencyRecord).where(IdempotencyRecord.organization_id == organization_id, IdempotencyRecord.user_id == user_id, IdempotencyRecord.operation == operation, IdempotencyRecord.idempotency_key == key).with_for_update())
    if record:
        expires_at = record.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at <= datetime.now(timezone.utc):
            db.delete(record)
            db.flush()
            record = None
    if record:
        if record.request_hash != request_hash:
            raise IdempotencyConflict
        return record.status_code, record.response_json
    status_code, response = action()
    db.add(IdempotencyRecord(organization_id=organization_id, user_id=user_id, operation=operation, idempotency_key=key, request_hash=request_hash, status_code=status_code, response_json=response))
    db.flush()
    return status_code, response
