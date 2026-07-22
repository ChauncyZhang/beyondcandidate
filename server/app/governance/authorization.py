from sqlalchemy import and_, exists, or_, select

from server.app.identity.models import Job
from server.app.identity.policy import Principal
from server.app.interviews.models import Interview
from server.app.llm.models import LlmProviderConfig
from server.app.recruiting.authorization import RecruitingAction, RecruitingAuthorizationService
from server.app.recruiting.models import Application, Candidate, Resume
from server.app.reports.models import ExportRecord
from server.app.screening.models import ScreeningItem, ScreeningRun
from server.app.talent.models import TalentPool, TalentPoolGrant, TalentPoolMembership


RECRUITING = RecruitingAuthorizationService()


def can_read_retention(principal: Principal) -> bool:
    return principal.active and bool(
        principal.roles & {"system_admin", "recruiting_admin", "recruiter"}
    )


def can_edit_retention(principal: Principal) -> bool:
    return principal.active and "system_admin" in principal.roles


def can_request_candidate_deletion(db, principal: Principal, candidate_id) -> bool:
    if not principal.active or not principal.roles & {"recruiter", "recruiting_admin"}:
        return False
    return db.scalar(
        select(Candidate.id).where(
            Candidate.organization_id == principal.organization_id,
            Candidate.id == candidate_id,
            RECRUITING.candidate_predicate(principal, RecruitingAction.READ, Candidate),
        )
    ) is not None


def can_read_candidate_governance(db, principal: Principal, candidate_id) -> bool:
    return db.scalar(
        select(Candidate.id).where(
            Candidate.organization_id == principal.organization_id,
            Candidate.id == candidate_id,
            RECRUITING.candidate_predicate(principal, RecruitingAction.READ, Candidate),
        )
    ) is not None


def can_manage_legal_hold(db, principal: Principal, candidate_id) -> bool:
    return (
        principal.active
        and "recruiting_admin" in principal.roles
        and can_read_candidate_governance(db, principal, candidate_id)
    )


def can_approve_deletion(principal: Principal) -> bool:
    return principal.active and "system_admin" in principal.roles


def audit_authorization_class(principal: Principal) -> str:
    capabilities = []
    if "system_admin" in principal.roles:
        capabilities.append("system")
    if "recruiting_admin" in principal.roles:
        capabilities.append("recruiting_all")
    elif "recruiter" in principal.roles:
        capabilities.append("recruiting_own")
    return "+".join(capabilities)


def audit_row_predicate(principal: Principal, audit_model):
    branches = []
    if principal.active and "system_admin" in principal.roles:
        branches.append(audit_model.category.in_(("system", "governance")))
    if principal.active and "recruiting_admin" in principal.roles:
        branches.append(audit_model.category.in_(("recruiting", "governance")))
    elif principal.active and "recruiter" in principal.roles:
        branches.append(
            and_(
                audit_model.category == "recruiting",
                audit_model.actor_user_id == principal.user_id,
            )
        )
    return or_(*branches) if branches else False


def can_view_recruiting_resource(db, principal: Principal, resource_type: str, resource_id) -> bool:
    if not principal.active:
        return False
    recruiting_admin = "recruiting_admin" in principal.roles
    recruiter = "recruiter" in principal.roles
    if resource_type == "llm_config":
        return "system_admin" in principal.roles and db.scalar(
            select(LlmProviderConfig.id).where(
                LlmProviderConfig.organization_id == principal.organization_id,
                LlmProviderConfig.id == resource_id,
            )
        ) is not None
    if resource_type in {"deletion_request", "legal_hold"}:
        from server.app.governance.deletion_models import DeletionRequest, LegalHold

        model = DeletionRequest if resource_type == "deletion_request" else LegalHold
        if "system_admin" in principal.roles:
            return db.scalar(
                select(model.id).where(
                    model.organization_id == principal.organization_id,
                    model.id == resource_id,
                )
            ) is not None
        if resource_type == "deletion_request":
            row = db.execute(
                select(DeletionRequest.candidate_id, DeletionRequest.requested_by).where(
                    DeletionRequest.organization_id == principal.organization_id,
                    DeletionRequest.id == resource_id,
                )
            ).first()
            if row is None or row.requested_by != principal.user_id:
                return False
            candidate_id = row.candidate_id
        else:
            if "recruiting_admin" not in principal.roles:
                return False
            candidate_id = db.scalar(
                select(LegalHold.candidate_id).where(
                    LegalHold.organization_id == principal.organization_id,
                    LegalHold.id == resource_id,
                )
            )
        if candidate_id is None:
            return False
        return can_read_candidate_governance(db, principal, candidate_id)
    if not (recruiting_admin or recruiter):
        return False
    job_scope = True if recruiting_admin else RECRUITING.job_predicate(
        principal, RecruitingAction.READ, Job
    )
    if resource_type == "job":
        return db.scalar(
            select(Job.id).where(
                Job.organization_id == principal.organization_id,
                Job.id == resource_id,
                job_scope,
            )
        ) is not None
    if resource_type == "candidate":
        return db.scalar(
            select(Candidate.id).where(
                Candidate.organization_id == principal.organization_id,
                Candidate.id == resource_id,
                True if recruiting_admin else RECRUITING.candidate_predicate(principal, RecruitingAction.READ, Candidate),
            )
        ) is not None
    if resource_type == "application":
        return db.scalar(
            select(Application.id)
            .join(
                Job,
                and_(
                    Job.organization_id == Application.organization_id,
                    Job.id == Application.job_id,
                ),
            )
            .where(
                Application.organization_id == principal.organization_id,
                Application.id == resource_id,
                job_scope,
            )
        ) is not None
    if resource_type == "resume":
        return db.scalar(
            select(Resume.id)
            .join(
                Application,
                and_(
                    Application.organization_id == Resume.organization_id,
                    Application.resume_id == Resume.id,
                ),
            )
            .join(
                Job,
                and_(
                    Job.organization_id == Application.organization_id,
                    Job.id == Application.job_id,
                ),
            )
            .where(
                Resume.organization_id == principal.organization_id,
                Resume.id == resource_id,
                job_scope,
            )
        ) is not None
    if resource_type == "screening_run":
        return db.scalar(
            select(ScreeningRun.id)
            .join(
                Job,
                and_(
                    Job.organization_id == ScreeningRun.organization_id,
                    Job.id == ScreeningRun.job_id,
                ),
            )
            .where(
                ScreeningRun.organization_id == principal.organization_id,
                ScreeningRun.id == resource_id,
                job_scope,
            )
        ) is not None
    if resource_type == "screening_item":
        return db.scalar(
            select(ScreeningItem.id)
            .join(
                ScreeningRun,
                and_(
                    ScreeningRun.organization_id == ScreeningItem.organization_id,
                    ScreeningRun.id == ScreeningItem.run_id,
                ),
            )
            .join(
                Job,
                and_(
                    Job.organization_id == ScreeningRun.organization_id,
                    Job.id == ScreeningRun.job_id,
                ),
            )
            .where(
                ScreeningItem.organization_id == principal.organization_id,
                ScreeningItem.id == resource_id,
                job_scope,
            )
        ) is not None
    if resource_type == "interview":
        return db.scalar(
            select(Interview.id)
            .join(
                Application,
                and_(
                    Application.organization_id == Interview.organization_id,
                    Application.id == Interview.application_id,
                ),
            )
            .join(
                Job,
                and_(
                    Job.organization_id == Application.organization_id,
                    Job.id == Application.job_id,
                ),
            )
            .where(
                Interview.organization_id == principal.organization_id,
                Interview.id == resource_id,
                job_scope,
            )
        ) is not None
    if resource_type in {"talent_pool", "talent_pool_membership"}:
        pool_scope = True if recruiting_admin else or_(
            TalentPool.owner_id == principal.user_id,
            TalentPool.visibility == "recruiting_team",
            exists().where(
                TalentPoolGrant.organization_id == TalentPool.organization_id,
                TalentPoolGrant.pool_id == TalentPool.id,
                TalentPoolGrant.user_id == principal.user_id,
                TalentPoolGrant.access_role.in_(("viewer", "manager")),
            ),
        )
        statement = select(TalentPool.id).where(
            TalentPool.organization_id == principal.organization_id,
            pool_scope,
        )
        if resource_type == "talent_pool":
            statement = statement.where(TalentPool.id == resource_id)
        else:
            statement = statement.join(
                TalentPoolMembership,
                and_(
                    TalentPoolMembership.organization_id == TalentPool.organization_id,
                    TalentPoolMembership.pool_id == TalentPool.id,
                ),
            ).where(TalentPoolMembership.id == resource_id)
        return db.scalar(statement) is not None
    if resource_type == "report_export":
        return db.scalar(
            select(ExportRecord.id).where(
                ExportRecord.organization_id == principal.organization_id,
                ExportRecord.id == resource_id,
                True if recruiting_admin else ExportRecord.requested_by == principal.user_id,
            )
        ) is not None
    return False
