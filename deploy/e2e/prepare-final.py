"""Create only synthetic, disposable prerequisites for the final browser gate."""

import os

from minio import Minio
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from server.app.identity.bootstrap import bootstrap_system_admin
from server.app.identity.models import Job, User, UserRole, UserStatus
from server.app.identity.security import PasswordService
from server.app.identity.store import IdentityStore
from server.app.recruiting.models import JobJdVersion, ScreeningRuleVersion


def required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def synthetic_email(name: str) -> str:
    value = required(name)
    if not value.casefold().endswith("@example.test"):
        raise RuntimeError(f"{name} must use the synthetic example.test domain")
    return value


def prepare_bucket() -> None:
    client = Minio(required("OBJECT_STORAGE_ENDPOINT"), access_key=required("OBJECT_STORAGE_ACCESS_KEY"), secret_key=required("OBJECT_STORAGE_SECRET_KEY"), secure=False)
    bucket = required("OBJECT_STORAGE_BUCKET")
    if not client.bucket_exists(bucket):
        client.make_bucket(bucket)


def prepare_database() -> None:
    store = IdentityStore(required("DATABASE_URL"))
    admin_id = bootstrap_system_admin(store, "final-e2e", "Final E2E Synthetic", synthetic_email("E2E_ADMIN_EMAIL"), "Final E2E Recruiting Admin", required("E2E_ADMIN_PASSWORD"))
    with store.sync_session() as db:
        admin = db.scalar(select(User).options(selectinload(User.roles)).where(User.id == admin_id))
        if admin is None:
            raise RuntimeError("synthetic admin was not persisted")
        for role in list(admin.roles):
            if role.role == "system_admin":
                db.delete(role)
        if "recruiting_admin" not in {role.role for role in admin.roles}:
            admin.roles.append(UserRole(role="recruiting_admin"))

        for email_name, password_name, display_name, role in (
            ("E2E_INTERVIEWER_EMAIL", "E2E_INTERVIEWER_PASSWORD", "Final E2E Interviewer", "interviewer"),
            ("E2E_UNASSIGNED_INTERVIEWER_EMAIL", "E2E_UNASSIGNED_INTERVIEWER_PASSWORD", "Final E2E Unassigned Interviewer", "interviewer"),
            ("E2E_RECRUITER_EMAIL", "E2E_RECRUITER_PASSWORD", "Final E2E Recruiter", "recruiter"),
        ):
            user_email = synthetic_email(email_name)
            user = db.scalar(select(User).options(selectinload(User.roles)).where(User.organization_id == admin.organization_id, User.normalized_email == user_email.casefold()))
            if user is None:
                user = User(
                    organization_id=admin.organization_id,
                    email=user_email,
                    normalized_email=user_email.casefold(),
                    display_name=display_name,
                    password_hash=PasswordService().hash(required(password_name)),
                    status=UserStatus.ACTIVE,
                )
                user.roles.append(UserRole(role=role))
                db.add(user)

        title = required("E2E_JOB_TITLE")
        job = db.scalar(select(Job).where(Job.organization_id == admin.organization_id, Job.title == title))
        if job is None:
            job = Job(organization_id=admin.organization_id, title=title, owner_id=admin.id, hiring_owner_id=admin.id, status="open")
            db.add(job)
            db.flush()
        if db.scalar(select(JobJdVersion).where(JobJdVersion.organization_id == admin.organization_id, JobJdVersion.job_id == job.id, JobJdVersion.version_number == 1)) is None:
            db.add(JobJdVersion(organization_id=admin.organization_id, job_id=job.id, version_number=1, content={"text": "Synthetic Python PostgreSQL role"}, created_by=admin.id))
        if db.scalar(select(ScreeningRuleVersion).where(ScreeningRuleVersion.organization_id == admin.organization_id, ScreeningRuleVersion.job_id == job.id, ScreeningRuleVersion.version_number == 1)) is None:
            db.add(ScreeningRuleVersion(organization_id=admin.organization_id, job_id=job.id, version_number=1, content={"required_terms": ["Python"], "bonus_terms": ["PostgreSQL"]}, created_by=admin.id))
        db.commit()


if __name__ == "__main__":
    prepare_bucket()
    prepare_database()
    print("Final E2E synthetic prerequisites are ready.")
