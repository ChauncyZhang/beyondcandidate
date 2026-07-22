from enum import Enum

from sqlalchemy import and_, exists, or_

from server.app.identity.models import Job, JobCollaborator
from server.app.identity.policy import Principal
from server.app.recruiting.models import Application, Candidate


class RecruitingAction(str, Enum):
    READ = "read"
    COMMENT = "comment"
    RECOMMEND = "recommend"
    MANAGE_CANDIDATE = "manage_candidate"
    MANAGE_JOB = "manage_job"
    CREATE_VERSION = "create_version"
    TRANSITION = "transition"
    PREVIEW = "preview"
    ISSUE_TICKET = "issue_ticket"
    DOWNLOAD = "download"
    EXPORT = "export"


ROLE_ACTIONS = {
    "recruiting_admin": set(RecruitingAction),
    "recruiter": {
        RecruitingAction.READ, RecruitingAction.COMMENT,
        RecruitingAction.RECOMMEND,
        RecruitingAction.MANAGE_CANDIDATE, RecruitingAction.MANAGE_JOB,
        RecruitingAction.CREATE_VERSION, RecruitingAction.TRANSITION,
        RecruitingAction.PREVIEW, RecruitingAction.ISSUE_TICKET,
        RecruitingAction.DOWNLOAD, RecruitingAction.EXPORT,
    },
    "hiring_manager": {
        RecruitingAction.READ, RecruitingAction.COMMENT,
        RecruitingAction.RECOMMEND, RecruitingAction.PREVIEW,
    },
}


class RecruitingAuthorizationService:
    @staticmethod
    def role_allows(principal: Principal, action: RecruitingAction) -> bool:
        return principal.active and any(action in ROLE_ACTIONS.get(role, set()) for role in principal.roles)

    @staticmethod
    def is_admin(principal: Principal) -> bool:
        return principal.active and "recruiting_admin" in principal.roles

    def job_predicate(self, principal: Principal, action: RecruitingAction, job=Job):
        if not principal.active:
            return False
        if "recruiting_admin" in principal.roles:
            return True
        branches = []
        if "recruiter" in principal.roles and action in ROLE_ACTIONS["recruiter"]:
            recruiter_grants = ("job_owner",) if action == RecruitingAction.RECOMMEND else ("job_owner", "job_recruiter")
            branches.append(exists().where(
                JobCollaborator.organization_id == job.organization_id,
                JobCollaborator.job_id == job.id,
                JobCollaborator.user_id == principal.user_id,
                JobCollaborator.access_role.in_(recruiter_grants),
            ))
        if "hiring_manager" in principal.roles and action in ROLE_ACTIONS["hiring_manager"]:
            branches.append(exists().where(
                JobCollaborator.organization_id == job.organization_id,
                JobCollaborator.job_id == job.id,
                JobCollaborator.user_id == principal.user_id,
                JobCollaborator.access_role == "job_manager",
            ))
        return or_(*branches) if branches else False

    def candidate_predicate(self, principal: Principal, action: RecruitingAction, candidate=Candidate):
        if not self.role_allows(principal, action):
            return False
        if self.is_admin(principal):
            return candidate.deleted_at.is_(None)
        authorized_application = exists().where(
            Application.organization_id == candidate.organization_id,
            Application.candidate_id == candidate.id,
            exists().where(
                Job.organization_id == Application.organization_id,
                Job.id == Application.job_id,
                self.job_predicate(principal, action, Job),
            ),
        )
        unassigned_owner = and_(
            "recruiter" in principal.roles,
            action in {RecruitingAction.READ, RecruitingAction.COMMENT, RecruitingAction.MANAGE_CANDIDATE},
            candidate.owner_id == principal.user_id,
            ~exists().where(
                Application.organization_id == candidate.organization_id,
                Application.candidate_id == candidate.id,
            ),
        )
        return and_(candidate.deleted_at.is_(None), or_(unassigned_owner, authorized_application))
