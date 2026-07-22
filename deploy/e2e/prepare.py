import os

from minio import Minio
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from server.app.identity.bootstrap import bootstrap_system_admin
from server.app.identity.models import Job, User, UserRole
from server.app.identity.store import IdentityStore
from server.app.recruiting.models import JobJdVersion, ScreeningRuleVersion


def required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def prepare_bucket() -> None:
    client = Minio(
        required("OBJECT_STORAGE_ENDPOINT"),
        access_key=required("OBJECT_STORAGE_ACCESS_KEY"),
        secret_key=required("OBJECT_STORAGE_SECRET_KEY"),
        secure=False,
    )
    bucket = required("OBJECT_STORAGE_BUCKET")
    if not client.bucket_exists(bucket):
        client.make_bucket(bucket)


def prepare_database() -> None:
    store = IdentityStore(required("DATABASE_URL"))
    email = required("E2E_ADMIN_EMAIL")
    user_id = bootstrap_system_admin(
        store,
        "phase3-e2e",
        "Phase 3 E2E",
        email,
        "Phase 3 Recruiting Admin",
        required("E2E_ADMIN_PASSWORD"),
    )

    with store.sync_session() as db:
        user = db.scalar(
            select(User)
            .options(selectinload(User.roles))
            .where(User.id == user_id)
        )
        if user is None:
            raise RuntimeError("bootstrap user was not persisted")
        for role in list(user.roles):
            if role.role == "system_admin":
                db.delete(role)
        if "recruiting_admin" not in {role.role for role in user.roles}:
            user.roles.append(UserRole(role="recruiting_admin"))

        title = required("E2E_JOB_TITLE")
        job = db.scalar(
            select(Job).where(
                Job.organization_id == user.organization_id,
                Job.title == title,
            )
        )
        if job is None:
            job = Job(
                organization_id=user.organization_id,
                title=title,
                owner_id=user.id,
                hiring_owner_id=user.id,
                status="open",
            )
            db.add(job)
            db.flush()

        jd = db.scalar(
            select(JobJdVersion).where(
                JobJdVersion.organization_id == user.organization_id,
                JobJdVersion.job_id == job.id,
                JobJdVersion.version_number == 1,
            )
        )
        if jd is None:
            db.add(
                JobJdVersion(
                    organization_id=user.organization_id,
                    job_id=job.id,
                    version_number=1,
                    content={"text": "Required: Python, PostgreSQL. Bonus: Docker."},
                    created_by=user.id,
                )
            )

        rules = db.scalar(
            select(ScreeningRuleVersion).where(
                ScreeningRuleVersion.organization_id == user.organization_id,
                ScreeningRuleVersion.job_id == job.id,
                ScreeningRuleVersion.version_number == 1,
            )
        )
        if rules is None:
            db.add(
                ScreeningRuleVersion(
                    organization_id=user.organization_id,
                    job_id=job.id,
                    version_number=1,
                    content={
                        "required_terms": ["Python", "PostgreSQL"],
                        "bonus_terms": ["Docker"],
                    },
                    created_by=user.id,
                )
            )
        db.commit()


if __name__ == "__main__":
    prepare_bucket()
    prepare_database()
    print("E2E dependencies and synthetic fixtures are ready.")
